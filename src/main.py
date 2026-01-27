import json
import subprocess
import sys
import urllib.parse
import urllib.request

from wox_plugin import (
    ActionContext,
    Context,
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


class MyPlugin(Plugin):
    api: PublicAPI

    async def init(self, ctx: Context, init_params: PluginInitParams) -> None:
        self.api = init_params.api

    async def action(self, ctx: Context, actionContext: ActionContext):
        data = actionContext.context_data
        action_name = data.get("action")

        if action_name == "copy_name":
            name = data.get("name", "")
            if self._copy_to_clipboard(name):
                await self.api.notify(ctx, f"Copied: {name}")
            else:
                await self.api.notify(ctx, "Failed to copy to clipboard")

        elif action_name == "copy_url":
            url = data.get("url", "")
            if self._copy_to_clipboard(url):
                await self.api.notify(ctx, "Copied URL to clipboard")
            else:
                await self.api.notify(ctx, "Failed to copy URL")

        elif action_name == "copy_svg":
            url = data.get("url", "")
            try:
                with urllib.request.urlopen(url) as response:
                    svg_content = response.read().decode("utf-8")
                    if self._copy_to_clipboard(svg_content):
                        await self.api.notify(ctx, "Copied SVG content to clipboard")
                    else:
                        await self.api.notify(ctx, "Failed to copy SVG content")
            except Exception as e:
                await self.api.log(ctx, LogLevel.ERROR, f"Error fetching SVG: {e}")
                await self.api.notify(ctx, f"Error fetching SVG: {e}")

    async def query(self, ctx: Context, query: Query) -> list[Result]:
        search_term = query.search
        if not search_term:
            return []

        results: list[Result] = []
        try:
            # Iconify search API
            params = urllib.parse.urlencode({"query": search_term, "limit": 50})
            api_url = f"https://api.iconify.design/search?{params}"

            with urllib.request.urlopen(api_url) as response:
                data = json.loads(response.read().decode("utf-8"))

            icons = data.get("icons", [])
            for icon_name in icons:
                # Format: prefix:name
                parts = icon_name.split(":")
                if len(parts) == 2:
                    prefix, name = parts
                    # Icon URL for display and downloading
                    icon_url = f"https://api.iconify.design/{prefix}/{name}.svg"

                    results.append(
                        Result(
                            title=icon_name,
                            sub_title="Copy name, URL or SVG content",
                            icon=WoxImage(
                                image_type=WoxImageType.URL,
                                image_data=icon_url,
                            ),
                            actions=[
                                ResultAction(
                                    name="Copy Name",
                                    prevent_hide_after_action=False,
                                    context_data={"action": "copy_name", "name": icon_name},
                                    action=self.action,
                                ),
                                ResultAction(
                                    name="Copy URL",
                                    prevent_hide_after_action=False,
                                    context_data={"action": "copy_url", "url": icon_url},
                                    action=self.action,
                                ),
                                ResultAction(
                                    name="Copy SVG Content",
                                    prevent_hide_after_action=False,
                                    context_data={"action": "copy_svg", "url": icon_url},
                                    action=self.action,
                                ),
                            ],
                        )
                    )

        except Exception as e:
            await self.api.log(ctx, LogLevel.ERROR, f"Search failed: {e}")
            results.append(
                Result(
                    title="Error searching icons",
                    sub_title=str(e),
                    icon=WoxImage(
                        image_type=WoxImageType.RELATIVE,
                        image_data="image/app.png",
                    ),
                )
            )

        return results

    def _copy_to_clipboard(self, text: str) -> bool:
        try:
            if sys.platform == "darwin":  # macOS
                process = subprocess.Popen(["pbcopy"], env={"LANG": "en_US.UTF-8"}, stdin=subprocess.PIPE)
                process.communicate(text.encode("utf-8"))
                return process.returncode == 0
            elif sys.platform == "win32":  # Windows
                # Use clip.exe (UTF-16 little endian usually works best for clip)
                process = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
                # Windows clip expects ANSI or similar, but complex unicode might fail with simple clip.
                # However, for simple usage this often works. Better might be powershell.
                # Let's try simple encoding first.
                process.communicate(text.encode("cp1252", errors="ignore"))
                # Re-thinking: Windows 'clip' is notoriously bad with encoding.
                # But for standard usage without deps, it's the option.
                # Just using 'mbcs' or default system locale encoding.
                return process.returncode == 0
            elif sys.platform.startswith("linux"):  # Linux
                # Try xclip or xsel
                for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
                    try:
                        process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                        process.communicate(text.encode("utf-8"))
                        if process.returncode == 0:
                            return True
                    except FileNotFoundError:
                        continue
                return False
        except Exception:
            return False
        return False


plugin = MyPlugin()
