import asyncio
import contextlib
import sqlite3
import sys
import threading
import urllib.parse
from pathlib import Path

if __package__ in {None, ""}:
    _ROOT = Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    __package__ = "src"

from .catalog import DownloadCancelled, IconCatalog, PackageInfo, fetch_package_info, format_bytes  # noqa: E402
from .icon_svg import IconRecord, download_icon_svg, icon_to_svg, progress_icon_svg  # noqa: E402
from .query_text import parse_query  # noqa: E402
from wox_plugin import (  # noqa: E402
    ActionContext,
    Context,
    CopyParams,
    CopyType,
    LogLevel,
    Plugin,
    PluginInitParams,
    PublicAPI,
    Query,
    QueryGridLayout,
    QueryLayout,
    QueryRefinement,
    QueryRefinementOption,
    QueryRefinementType,
    QueryResponse,
    RefreshQueryParam,
    Result,
    ResultAction,
    UpdatableResult,
    WoxImage,
    WoxImageType,
)

STATUS_RESULT_ID = "iconify-catalog-status"
STATUS_ACTION_ID = "iconify-download"
RESULT_LIMIT = 100

EXECUTE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
    'stroke="var(--wox-theme-icon-color)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M13 2 4.5 13.5h5.5L9 22l10-13h-6z"/></svg>'
)
COPY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
    'stroke="var(--wox-theme-icon-color)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="8" y="8" width="12" height="12" rx="2"/>'
    '<path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>'
)


class MyPlugin(Plugin):
    api: PublicAPI
    catalog: IconCatalog | None
    _cancel: threading.Event
    _status_ids: set[str]
    _status_ctx: Context | None
    _last_search: str
    _task: asyncio.Task[None] | None
    _offer_task: asyncio.Task[None] | None
    _offer: PackageInfo | None
    _init_error: str
    _closed: bool
    _translations: dict[str, str]

    def __init__(self) -> None:
        self.catalog = None
        self._cancel = threading.Event()
        self._status_ids = {STATUS_RESULT_ID}
        self._status_ctx = None
        self._last_search = ""
        self._task = None
        self._offer_task = None
        self._offer = None
        self._init_error = ""
        self._closed = False
        self._translations = {}

    async def init(self, ctx: Context, init_params: PluginInitParams) -> None:
        self.api = init_params.api
        self._status_ctx = ctx
        get_cache_folder = getattr(self.api, "get_cache_folder", None)
        if get_cache_folder is None:
            self._init_error = "This Wox build has no plugin cache folder API"
            await self.api.log(ctx, LogLevel.ERROR, self._init_error)
            return
        try:
            folder = await get_cache_folder(ctx)
        except Exception as exc:
            self._init_error = str(exc)
            await self.api.log(ctx, LogLevel.ERROR, f"Iconify cache folder unavailable: {exc}")
            return
        if not folder:
            self._init_error = "Wox did not return a cache folder"
            await self.api.log(ctx, LogLevel.ERROR, self._init_error)
            return

        self.catalog = IconCatalog(Path(folder))
        await self.api.on_unload(ctx, self._on_unload)
        if self.catalog.is_ready():
            await self.api.log(ctx, LogLevel.INFO, f"Iconify catalog ready, {self.catalog.icon_count} icons")
            asyncio.create_task(asyncio.to_thread(self.catalog.ensure_palette))
            return
        self._offer_task = asyncio.create_task(self._prefetch_offer(ctx))

    async def query(self, ctx: Context, query: Query) -> QueryResponse:
        self._last_search = query.search or ""
        self._status_ctx = ctx
        if self.catalog is None:
            return _list_response([await self._unavailable_result(ctx)])
        if not self.catalog.is_ready():
            self._remember_status(ctx, STATUS_RESULT_ID)
            return _list_response([await self._status_result(ctx)])

        term, color = parse_query(_search_text(query))
        palette = _palette_filter(query.refinements.get("palette", "all"))
        refinement = await self._palette_refinement(ctx)
        if not term:
            return _grid_response([], [refinement])
        if not self.catalog.palette_ready:
            await self.api.log(ctx, LogLevel.INFO, "Classifying icons as color or monochrome")
        try:
            records = await asyncio.to_thread(self.catalog.search, term, RESULT_LIMIT, palette)
        except sqlite3.Error as exc:
            await self.api.log(ctx, LogLevel.ERROR, f"Local icon search failed: {exc}")
            return _list_response(
                [
                    Result(
                        title="i18n:error_search_title",
                        sub_title=str(exc),
                        icon=WoxImage(image_type=WoxImageType.RELATIVE, image_data="image/app.png"),
                    )
                ]
            )

        subtitle = await self._result_subtitle(ctx, color)
        results: list[Result] = []
        for index, record in enumerate(records):
            icon_name = f"{record.prefix}:{record.name}"
            svg = icon_to_svg(record, color)
            results.append(
                Result(
                    id=icon_name,
                    title=icon_name,
                    sub_title=subtitle,
                    score=float(RESULT_LIMIT - index),
                    icon=WoxImage(image_type=WoxImageType.SVG, image_data=svg),
                    actions=self._icon_actions(record, color),
                )
            )
        return _grid_response(results, [refinement])

    async def _on_download(self, ctx: Context, action_context: ActionContext) -> None:
        self._remember_status(ctx, action_context.result_id or STATUS_RESULT_ID)
        if self.catalog is None:
            return
        if self.catalog.is_ready():
            await self.api.refresh_query(ctx, RefreshQueryParam(preserve_selected_index=False))
            return
        if self._task is None or self._task.done():
            self.catalog.progress.update(phase="downloading", done=0, total=0, detail="", error="")
            self._task = asyncio.create_task(self._run_download())
        await self._publish_status(ctx)

    async def _run_download(self) -> None:
        ctx = self._status_ctx
        pump = asyncio.create_task(self._pump_progress())
        try:
            await self._ensure_offer()
            offer = self._offer
            catalog = self.catalog
            if catalog is None or offer is None:
                raise RuntimeError(self._init_error or "Iconify package metadata is unavailable")
            await asyncio.to_thread(catalog.download_and_index, offer, self._cancel)
        except DownloadCancelled:
            return
        except Exception as exc:
            if ctx is not None and not self._closed:
                await self.api.log(ctx, LogLevel.ERROR, f"Iconify download failed: {exc}")
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

        if self._closed or self.catalog is None:
            return
        ctx = self._status_ctx or ctx
        if ctx is None:
            return
        await self._publish_status(ctx)
        if self.catalog.is_ready():
            await self.api.log(ctx, LogLevel.INFO, f"Iconify catalog ready, {self.catalog.icon_count} icons")
            await self.api.notify(ctx, "i18n:download_ready_title")
            if self._last_search.strip():
                await self.api.refresh_query(ctx, RefreshQueryParam(preserve_selected_index=False))
            return
        if self.catalog.progress.snapshot()[0] == "error":
            await self.api.notify(ctx, "i18n:download_failed_title")

    async def _pump_progress(self) -> None:
        last: tuple[str, int, int, str, str] | None = None
        while not self._closed:
            if self.catalog is None:
                return
            snapshot = self.catalog.progress.snapshot()
            ctx = self._status_ctx
            if snapshot != last and ctx is not None:
                last = snapshot
                await self._publish_status(ctx)
            await asyncio.sleep(0.4)

    async def _prefetch_offer(self, ctx: Context) -> None:
        try:
            self._offer = await asyncio.to_thread(fetch_package_info)
        except Exception as exc:
            await self.api.log(ctx, LogLevel.ERROR, f"Iconify package lookup failed: {exc}")
            return
        if self._closed or self.catalog is None or self.catalog.is_ready():
            return
        if self._task is not None and not self._task.done():
            return
        await self._publish_status(self._status_ctx or ctx)

    async def _ensure_offer(self) -> None:
        if self._offer is not None:
            return
        if self._offer_task is not None:
            with contextlib.suppress(Exception):
                await self._offer_task
        if self._offer is None:
            self._offer = await asyncio.to_thread(fetch_package_info)

    async def _on_unload(self, ctx: Context) -> None:
        del ctx
        self._closed = True
        self._cancel.set()
        if self.catalog is not None:
            self.catalog.close()

    async def _on_copy(self, ctx: Context, action_context: ActionContext) -> None:
        data = action_context.context_data
        action_name = data.get("action", "")
        if action_name == "copy_name":
            name = data.get("name", "")
            await self.api.copy(ctx, CopyParams(type=CopyType.TEXT, text=name))
            await self.api.notify(ctx, await self._tr(ctx, "notify_name_copied", name=name))
            return
        if action_name == "copy_url":
            await self.api.copy(ctx, CopyParams(type=CopyType.TEXT, text=data.get("url", "")))
            await self.api.notify(ctx, "i18n:notify_url_copied")
            return
        if action_name != "copy_svg" or self.catalog is None:
            return
        record = self.catalog.get(data.get("prefix", ""), data.get("name", ""))
        if record is None:
            await self.api.notify(ctx, await self._tr(ctx, "notify_svg_fetch_error", error="icon is not in the local catalog"))
            return
        color = data.get("color") or None
        await self.api.copy(ctx, CopyParams(type=CopyType.TEXT, text=icon_to_svg(record, color)))
        await self.api.notify(ctx, "i18n:notify_svg_copied")

    async def _publish_status(self, ctx: Context) -> None:
        if self.catalog is None or self._closed:
            return
        status = await self._status_result(ctx)
        for result_id in list(self._status_ids):
            await self.api.update_result(
                ctx,
                UpdatableResult(
                    id=result_id,
                    title=status.title,
                    sub_title=status.sub_title,
                    icon=status.icon,
                    actions=status.actions,
                ),
            )

    async def _status_result(self, ctx: Context) -> Result:
        title, subtitle, action_name, icon = await self._status_view(ctx)
        return Result(
            id=STATUS_RESULT_ID,
            title=title,
            sub_title=subtitle,
            icon=icon,
            score=1000,
            actions=[
                ResultAction(
                    id=STATUS_ACTION_ID,
                    name=action_name,
                    icon=WoxImage(image_type=WoxImageType.SVG, image_data=EXECUTE_SVG),
                    is_default=True,
                    prevent_hide_after_action=True,
                    action=self._on_download,
                )
            ],
        )

    async def _status_view(self, ctx: Context) -> tuple[str, str, str, WoxImage]:
        phase, done, total, error, detail = ("missing", 0, 0, self._init_error, "")
        if self.catalog is not None:
            phase, done, total, error, detail = self.catalog.progress.snapshot()
        if phase == "missing" and self._task is not None and not self._task.done():
            phase = "downloading"

        if phase == "downloading":
            return await self._byte_progress(ctx, done, total)
        if phase == "verifying":
            return (
                await self._tr(ctx, "progress_verifying_title"),
                await self._tr(ctx, "progress_verifying_subtitle"),
                await self._tr(ctx, "progress_verifying_action"),
                WoxImage(image_type=WoxImageType.SVG, image_data=progress_icon_svg(None)),
            )
        if phase == "indexing":
            detail_suffix = ""
            if detail:
                detail_suffix = await self._tr(ctx, "progress_indexing_detail_suffix", detail=detail)
            subtitle = await self._tr(ctx, "progress_indexing_subtitle", done=str(done), total=str(total), detail=detail_suffix)
            action_name = await self._tr(ctx, "progress_indexing_action", done=str(done), total=str(total))
            fraction = (done / total) if total else None
            return (
                await self._tr(ctx, "progress_indexing_title"),
                subtitle,
                action_name,
                WoxImage(image_type=WoxImageType.SVG, image_data=progress_icon_svg(fraction)),
            )
        if phase == "error":
            message = _short_error(error) or await self._tr(ctx, "download_failed_title")
            return (
                await self._tr(ctx, "download_failed_title"),
                await self._tr(ctx, "download_failed_subtitle", error=message),
                await self._tr(ctx, "download_retry_action"),
                WoxImage(image_type=WoxImageType.SVG, image_data=download_icon_svg()),
            )
        if phase == "ready":
            return (
                await self._tr(ctx, "download_ready_title"),
                await self._tr(ctx, "download_ready_subtitle"),
                await self._tr(ctx, "download_ready_action"),
                WoxImage(image_type=WoxImageType.SVG, image_data=download_icon_svg()),
            )

        size_suffix = await self._size_suffix(ctx)
        return (
            await self._tr(ctx, "download_title"),
            await self._tr(ctx, "download_subtitle", size=size_suffix),
            await self._tr(ctx, "download_action", size=size_suffix),
            WoxImage(image_type=WoxImageType.SVG, image_data=download_icon_svg()),
        )

    async def _byte_progress(self, ctx: Context, done: int, total: int) -> tuple[str, str, str, WoxImage]:
        if total <= 0 and done <= 0:
            return (
                await self._tr(ctx, "progress_starting_title"),
                await self._tr(ctx, "progress_starting_subtitle"),
                await self._tr(ctx, "progress_starting_action"),
                WoxImage(image_type=WoxImageType.SVG, image_data=progress_icon_svg(None)),
            )
        done_text = format_bytes(done)
        if total <= 0:
            return (
                await self._tr(ctx, "progress_downloading_title"),
                await self._tr(ctx, "progress_downloading_unknown_subtitle", done=done_text),
                await self._tr(ctx, "progress_downloading_unknown_action", done=done_text),
                WoxImage(image_type=WoxImageType.SVG, image_data=progress_icon_svg(None)),
            )
        percent = min(100, int(done * 100 / total))
        total_text = format_bytes(total)
        return (
            await self._tr(ctx, "progress_downloading_title"),
            await self._tr(ctx, "progress_downloading_subtitle", done=done_text, total=total_text, percent=str(percent)),
            await self._tr(ctx, "progress_downloading_action", done=done_text, total=total_text, percent=str(percent)),
            WoxImage(image_type=WoxImageType.SVG, image_data=progress_icon_svg(done / total)),
        )

    async def _unavailable_result(self, ctx: Context) -> Result:
        return Result(
            id=STATUS_RESULT_ID,
            title=await self._tr(ctx, "cache_unavailable_title"),
            sub_title=await self._tr(ctx, "cache_unavailable_subtitle", error=_short_error(self._init_error)),
            icon=WoxImage(image_type=WoxImageType.RELATIVE, image_data="image/app.png"),
        )

    def _icon_actions(self, record: IconRecord, color: str | None) -> list[ResultAction]:
        icon_name = f"{record.prefix}:{record.name}"
        url = "https://api.iconify.design/" f"{urllib.parse.quote(record.prefix)}/{urllib.parse.quote(record.name)}.svg"
        if color:
            url += f"?color={urllib.parse.quote(color)}"
        copy_icon = WoxImage(image_type=WoxImageType.SVG, image_data=COPY_SVG)
        identity = {"prefix": record.prefix, "name": record.name, "color": color or ""}
        return [
            ResultAction(
                name="i18n:action_copy_svg",
                icon=copy_icon,
                is_default=True,
                context_data={"action": "copy_svg", **identity},
                action=self._on_copy,
            ),
            ResultAction(
                name="i18n:action_copy_url",
                icon=copy_icon,
                context_data={"action": "copy_url", "url": url, **identity},
                action=self._on_copy,
            ),
            ResultAction(
                name="i18n:action_copy_name",
                icon=copy_icon,
                context_data={"action": "copy_name", **identity, "name": icon_name},
                action=self._on_copy,
            ),
        ]

    def _remember_status(self, ctx: Context, result_id: str) -> None:
        self._status_ctx = ctx
        self._status_ids.add(STATUS_RESULT_ID)
        if result_id:
            self._status_ids.add(result_id)

    async def _palette_refinement(self, ctx: Context) -> QueryRefinement:
        return QueryRefinement(
            id="palette",
            title=await self._tr(ctx, "refinement_palette"),
            type=QueryRefinementType.SINGLE_SELECT,
            hotkey=_primary_hotkey("t"),
            default_value=["all"],
            persist=True,
            options=[
                QueryRefinementOption(value="all", title=await self._tr(ctx, "refinement_palette_all")),
                QueryRefinementOption(value="color", title=await self._tr(ctx, "refinement_palette_color")),
                QueryRefinementOption(value="mono", title=await self._tr(ctx, "refinement_palette_mono")),
            ],
        )

    async def _size_suffix(self, ctx: Context) -> str:
        offer = self._offer
        download_size = getattr(offer, "download_size", 0)
        if not isinstance(download_size, int) or download_size <= 0:
            return ""
        return await self._tr(ctx, "download_size_suffix", size=format_bytes(download_size))

    async def _result_subtitle(self, ctx: Context, color: str | None) -> str:
        suffix = ""
        if color:
            suffix = await self._tr(ctx, "result_subtitle_color_suffix", color=color)
        return await self._tr(ctx, "result_subtitle", suffix=suffix)

    async def _tr(self, ctx: Context, key: str, **kwargs: str) -> str:
        raw = self._translations.get(key)
        if raw is None:
            raw = await self.api.get_translation(ctx, key)
            self._translations[key] = raw
        return raw.format(**kwargs) if kwargs else raw


def _search_text(query: Query) -> str:
    hint = query.query_hint
    if hint is not None:
        for element in hint.elements:
            if element.id == "search" and element.kind == "argument":
                return element.value or ""
    return query.search or ""


def _palette_filter(value: str) -> str:
    selected = value.split(",", 1)[0].strip()
    if selected in {"color", "mono"}:
        return selected
    return "all"


def _primary_hotkey(key: str) -> str:
    modifier = "cmd" if sys.platform == "darwin" else "ctrl"
    return f"{modifier}+{key}"


def _list_response(results: list[Result]) -> QueryResponse:
    # No GridLayout: the host merges this over plugin metadata, and a missing
    # grid stays a list only when plugin.json does not declare gridLayout.
    return QueryResponse(results=results)


def _grid_response(results: list[Result], refinements: list[QueryRefinement] | None = None) -> QueryResponse:
    return QueryResponse(
        results=results,
        refinements=refinements or [],
        layout=QueryLayout(grid_layout=QueryGridLayout(columns=10, item_padding=12, item_margin=6)),
    )


def _short_error(error: str) -> str:
    text = " ".join(error.split())
    if len(text) > 240:
        return text[:237] + "..."
    return text


plugin = MyPlugin()
