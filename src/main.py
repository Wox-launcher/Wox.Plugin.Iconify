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
        await self.api.log(ctx, LogLevel.INFO, actionContext.context_data.get("search_term", ""))
        await self.api.notify(ctx, "Action executed!")

    async def query(self, ctx: Context, query: Query) -> list[Result]:
        results: list[Result] = []
        search_term = query.search.lower() if query.search else ""

        results.append(
            Result(
                title=f"you typed {search_term}",
                sub_title="this is subsitle",
                icon=WoxImage(
                    image_type=WoxImageType.RELATIVE,
                    image_data="image/app.png",
                ),
                actions=[
                    ResultAction(
                        name="My Action",
                        prevent_hide_after_action=True,
                        context_data={"search_term": search_term},
                        action=self.action,
                    )
                ],
            )
        )

        return results


plugin = MyPlugin()
