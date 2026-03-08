import json
import re
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from wox_plugin import (
    ActionContext,
    Context,
    CopyParams,
    CopyType,
    LogLevel,
    Plugin,
    PluginInitParams,
    PublicAPI,
    Query,
    Result,
    ResultAction,
    WoxImage,
    WoxImageType,
)

CopyIconSvg = """<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80"><g fill="none"><path fill="#f2c94c" fill-rule="evenodd" d="M30 55V39.57a4 4 0 0 1 1.158-2.816l10.47-10.568a4 4 0 0 1 2.84-1.185H50v-8a4 4 0 0 0-4-4h-8.19L22 13a4 4 0 0 0-4 4v34a4 4 0 0 0 4 4z" clip-rule="evenodd"/><path fill="#f2994a" fill-rule="evenodd" d="M41.73 26.087a4 4 0 0 1 2.739-1.085L57.999 25A4 4 0 0 1 62 29v34a4 4 0 0 1-4 4H34a4 4 0 0 1-4-4V39.57a4 4 0 0 1 1.08-2.734a.667.667 0 0 0 .52 1.086h7.2a4 4 0 0 0 4-4v-7.308a.665.665 0 0 0-1.07-.527" clip-rule="evenodd"/><path fill="#f2f2f2" d="m31.126 36.785l10.537-10.64a.665.665 0 0 1 1.137.468v7.308a4 4 0 0 1-4 4h-7.2a.667.667 0 0 1-.474-1.136"/></g></svg>"""


class MyPlugin(Plugin):
    api: PublicAPI

    async def init(self, ctx: Context, init_params: PluginInitParams) -> None:
        self.api = init_params.api

    async def action(self, ctx: Context, actionContext: ActionContext):
        data = actionContext.context_data
        action_name = data.get("action")

        if action_name == "copy_name":
            name = data.get("name", "")
            await self.api.copy(ctx, CopyParams(type=CopyType.TEXT, text=name))
            await self.api.notify(ctx, await self._tr(ctx, "notify_name_copied", name=name))

        elif action_name == "copy_url":
            url = data.get("url", "")
            await self.api.copy(ctx, CopyParams(type=CopyType.TEXT, text=url))
            await self.api.notify(ctx, "i18n:notify_url_copied")

        elif action_name == "copy_svg":
            url = data.get("url", "")
            try:
                svg_content = self._fetch_svg(url)
                await self.api.copy(ctx, CopyParams(type=CopyType.TEXT, text=svg_content))
                await self.api.notify(ctx, "i18n:notify_svg_copied")
            except Exception as e:
                await self.api.log(ctx, LogLevel.ERROR, f"Error fetching SVG: {e}")
                await self.api.notify(ctx, await self._tr(ctx, "notify_svg_fetch_error", error=str(e)))

    async def query(self, ctx: Context, query: Query) -> list[Result]:
        raw_search = query.search
        if not raw_search:
            return []

        # Parse color from query (e.g. "home #f00" or "user icon blue")
        # Simple heuristic: last word starting with # is hex color,
        # or we could try to detect standard CSS color names, but hex is safer for now.
        search_term, color = self._parse_query(raw_search)

        if not search_term:
            return []

        results: list[Result] = []
        try:
            params = urllib.parse.urlencode({"query": search_term, "limit": 100})
            api_url = f"https://api.iconify.design/search?{params}"

            req = urllib.request.Request(api_url, headers={"User-Agent": "Wox.Plugin.Iconify"})

            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))

            icons = data.get("icons", [])
            for icon_name in icons:
                # Format: prefix:name
                parts = icon_name.split(":")
                if len(parts) == 2:
                    prefix, name = parts

                    icon_url = self._build_icon_url(prefix, name, color)

                    results.append(
                        Result(
                            title=icon_name,
                            sub_title=await self._result_subtitle(ctx, color),
                            icon=WoxImage(
                                image_type=WoxImageType.URL,
                                image_data=icon_url,
                            ),
                            actions=[
                                ResultAction(
                                    name="i18n:action_copy_svg",
                                    icon=WoxImage(
                                        image_type=WoxImageType.SVG,
                                        image_data=CopyIconSvg,
                                    ),
                                    is_default=True,
                                    prevent_hide_after_action=False,
                                    context_data={"action": "copy_svg", "url": icon_url},
                                    action=self.action,
                                ),
                                ResultAction(
                                    name="i18n:action_copy_url",
                                    icon=WoxImage(
                                        image_type=WoxImageType.SVG,
                                        image_data=CopyIconSvg,
                                    ),
                                    prevent_hide_after_action=False,
                                    context_data={"action": "copy_url", "url": icon_url},
                                    action=self.action,
                                ),
                                ResultAction(
                                    name="i18n:action_copy_name",
                                    icon=WoxImage(
                                        image_type=WoxImageType.SVG,
                                        image_data=CopyIconSvg,
                                    ),
                                    prevent_hide_after_action=False,
                                    context_data={"action": "copy_name", "name": icon_name},
                                    action=self.action,
                                ),
                            ],
                        )
                    )

        except Exception as e:
            await self.api.log(ctx, LogLevel.ERROR, f"Search failed: {e}")
            results.append(
                Result(
                    title="i18n:error_search_title",
                    sub_title=str(e),
                    icon=WoxImage(
                        image_type=WoxImageType.RELATIVE,
                        image_data="image/app.png",
                    ),
                )
            )

        return results

    def _parse_query(self, query_str: str) -> Tuple[str, Optional[str]]:
        """Extracts search term and optional color from query string."""
        # Simple heuristic: last word starting with # is hex color,
        # or we could try to detect standard CSS color names, but hex is safer for now.
        match = re.search(r"\s+(#[0-9a-fA-F]{3,6})$", query_str)
        if match:
            color = match.group(1)
            term = query_str[: match.start()].strip()
            return term, color

        common_colors = {"red", "green", "blue", "black", "white", "yellow", "orange", "purple", "gray", "grey"}
        parts = query_str.split()
        if len(parts) > 1 and parts[-1].lower() in common_colors:
            color = parts[-1]
            term = " ".join(parts[:-1])
            return term, color

        return query_str, None

    def _build_icon_url(self, prefix: str, name: str, color: Optional[str]) -> str:
        icon_url = f"https://api.iconify.design/{prefix}/{name}.svg"
        if color:
            icon_url += f"?color={urllib.parse.quote(color)}"
        return icon_url

    def _fetch_svg(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "Wox.Plugin.Iconify/0.0.6"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read().decode("utf-8")

    async def _result_subtitle(self, ctx: Context, color: Optional[str]) -> str:
        suffix = ""
        if color:
            suffix = await self._tr(ctx, "result_subtitle_color_suffix", color=color)

        return await self._tr(ctx, "result_subtitle", suffix=suffix)

    async def _tr(self, ctx: Context, key: str, **kwargs: str) -> str:
        raw = await self.api.get_translation(ctx, key)
        return raw.format(**kwargs) if kwargs else raw


plugin = MyPlugin()
