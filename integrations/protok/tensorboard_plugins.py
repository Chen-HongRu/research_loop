"""Request-local task metadata filtering for the optional TensorBoard service."""

from copy import copy

from integrations.protok.tensorboard_scope import parse_request


class _ScopedProvider:
    def __init__(self, provider, scope):
        self._provider = provider
        self._scope = scope
        self._runs = {}

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def _list(self, method, ctx, experiment_id, plugin_name, run_tag_filter):
        from tensorboard.data.provider import RunTagFilter

        if experiment_id not in self._runs:
            self._runs[experiment_id] = {
                run.run_name
                for run in self._provider.list_runs(ctx, experiment_id=experiment_id)
                if self._scope.matches(run.run_name)
            }
        runs = self._runs[experiment_id]
        tags = None
        if run_tag_filter is not None:
            if run_tag_filter.runs is not None:
                runs = runs.intersection(run_tag_filter.runs)
            tags = run_tag_filter.tags
        return getattr(self._provider, method)(
            ctx,
            experiment_id=experiment_id,
            plugin_name=plugin_name,
            run_tag_filter=RunTagFilter(runs=runs, tags=tags),
        )

    def list_scalars(
        self, ctx=None, *, experiment_id, plugin_name, run_tag_filter=None
    ):
        return self._list(
            "list_scalars", ctx, experiment_id, plugin_name, run_tag_filter
        )

    def list_tensors(
        self, ctx=None, *, experiment_id, plugin_name, run_tag_filter=None
    ):
        return self._list(
            "list_tensors", ctx, experiment_id, plugin_name, run_tag_filter
        )

    def list_blob_sequences(
        self, ctx=None, *, experiment_id, plugin_name, run_tag_filter=None
    ):
        return self._list(
            "list_blob_sequences", ctx, experiment_id, plugin_name, run_tag_filter
        )


def task_plugins(allowed_sources):
    """Keep TensorBoard's default plugins, scoping metadata before it is built."""
    from tensorboard import default
    from tensorboard.backend import http_util
    from tensorboard.plugins.metrics.metrics_plugin import MetricsPlugin
    from tensorboard.plugins.scalar.scalars_plugin import ScalarsPlugin
    from tensorboard.plugins.text.text_plugin import TextPlugin
    from werkzeug.wrappers import Request

    sources = frozenset(allowed_sources)

    def scoped(base):
        class TaskPlugin(base):
            def __init__(self, context):
                super().__init__(context)
                self._task_context = context

            def get_plugin_apps(self):
                apps = super().get_plugin_apps()
                original = apps["/tags"]

                def tags(environ, start_response):
                    path = (
                        environ.get("PATH_INFO", "")
                        + "?"
                        + environ.get("QUERY_STRING", "")
                    )
                    try:
                        _, scope = parse_request(path, sources)
                    except ValueError as error:
                        response = http_util.Respond(
                            Request(environ), str(error), "text/plain", code=400
                        )
                        return response(environ, start_response)
                    if scope is None:
                        return original(environ, start_response)
                    context = copy(self._task_context)
                    context.data_provider = _ScopedProvider(
                        context.data_provider, scope
                    )
                    handler = base(context).get_plugin_apps()["/tags"]
                    return handler(environ, start_response)

                apps["/tags"] = tags
                return apps

        return TaskPlugin

    replacements = {
        base: scoped(base) for base in (ScalarsPlugin, MetricsPlugin, TextPlugin)
    }
    return [replacements.get(plugin, plugin) for plugin in default.get_plugins()]
