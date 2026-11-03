"""Real TensorBoard metadata listing is bounded before HTML construction."""

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from integrations.protok import tensorboard_plugins
from integrations.protok.tensorboard_scope import Scope


@unittest.skipIf(
    importlib.util.find_spec("tensorboard") is None, "install dashboard extra"
)
class TaskPluginTests(unittest.TestCase):
    def setUp(self):
        from integrations.protok import tensorboard as mirror
        from research_loop import telemetry
        from tensorboard.backend.application import TensorBoardWSGI
        from tensorboard.backend.event_processing.data_provider import (
            MultiplexerDataProvider,
        )
        from tensorboard.backend.event_processing.plugin_event_multiplexer import (
            EventMultiplexer,
        )
        from tensorboard.plugins.base_plugin import TBContext
        from werkzeug.test import Client

        self.telemetry = telemetry
        self.Client = Client
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run"
        self.study = self.root / "study"
        self.logdir = Path(temporary.name) / "derived"
        self.source = {
            "kind": "fixed-short",
            "experiment_id": "0001",
            "source_identity": "synthetic",
            "execution_identity": "job-1",
        }
        telemetry.register(self.study, "selected", self.source)
        telemetry.register(self.study, "other", self.source | {"experiment_id": "0002"})
        self.emit("selected", {f"metric-{index}": index for index in range(34)})
        for start in range(0, 1001, 250):
            self.emit(
                "other",
                {
                    f"metric-{index}": index
                    for index in range(start, min(start + 250, 1001))
                },
            )
        self.exporter = mirror.Exporter(self.root, self.logdir)
        self.assertEqual(self.exporter.poll()["errors"], [])
        self.multiplexer = EventMultiplexer(size_guidance={"tensors": 0})
        self.provider = MultiplexerDataProvider(
            self.multiplexer, str(self.logdir / "events-v2")
        )
        self.context = TBContext(data_provider=self.provider)
        plugins = tensorboard_plugins.task_plugins({"active"})
        self.plugin_classes = {
            plugin.plugin_name: plugin
            for plugin in plugins
            if isinstance(plugin, type)
            and getattr(plugin, "plugin_name", None)
            in {"scalars", "timeseries", "text"}
        }
        self.assertEqual(set(self.plugin_classes), {"scalars", "timeseries", "text"})
        self.plugins = [plugin(self.context) for plugin in self.plugin_classes.values()]
        self.app = TensorBoardWSGI(
            self.plugins, path_prefix="/tensorboard", data_provider=self.provider
        )
        self.client = Client(self.app)
        self.reload()

    def emit(self, stream, value):
        self.telemetry.emit(
            "train/loss",
            value,
            step=10,
            unit="loss",
            context={"seed": 7},
            path=self.study / "telemetry" / stream / "events.jsonl",
        )

    def reload(self):
        self.multiplexer.AddRunsFromDirectory(str(self.logdir / "events-v2"))
        self.multiplexer.Reload()

    def get(self, plugin, experiment="0001", client=None):
        return (client or self.client).get(
            f"/tensorboard/data/plugin/{plugin}/tags",
            query_string={
                "dashboard_task": json.dumps(
                    {"source": "active", "experiment": experiment}
                )
            },
        )

    def test_real_metadata_filters_provider_before_listing_and_refreshes_runs(self):
        with patch.object(
            self.provider, "list_scalars", wraps=self.provider.list_scalars
        ) as scalars:
            response = self.get("scalars")
            self.assertEqual(response.status_code, 200)
            tags = response.get_json()
            self.assertEqual(list(tags), ["active/#0001"])
            self.assertEqual(len(tags["active/#0001"]), 34)
            self.assertTrue(
                all("seed=7; loss; step" in tag for tag in tags["active/#0001"])
            )
            self.assertEqual(
                scalars.call_args.kwargs["run_tag_filter"].runs, {"active/#0001"}
            )
        with (
            patch.object(
                self.provider, "list_scalars", wraps=self.provider.list_scalars
            ) as scalars,
            patch.object(
                self.provider, "list_tensors", wraps=self.provider.list_tensors
            ) as tensors,
            patch.object(
                self.provider,
                "list_blob_sequences",
                wraps=self.provider.list_blob_sequences,
            ) as blobs,
            patch.object(
                self.provider, "list_runs", wraps=self.provider.list_runs
            ) as runs,
        ):
            response = self.get("timeseries")
            self.assertEqual(response.status_code, 200)
            metadata = response.get_json()["scalars"]
            self.assertEqual(
                metadata["runTagInfo"], {"active/#0001": sorted(tags["active/#0001"])}
            )
            self.assertEqual(
                set(metadata["tagDescriptions"]), set(tags["active/#0001"])
            )
            for listing in (scalars, tensors, blobs):
                self.assertEqual(
                    listing.call_args.kwargs["run_tag_filter"].runs, {"active/#0001"}
                )
            self.assertEqual(runs.call_count, 1)
        with patch.object(
            self.provider, "list_tensors", wraps=self.provider.list_tensors
        ) as listing:
            response = self.get("text")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(response.get_json()), ["active/#0001"])
            self.assertEqual(
                listing.call_args.kwargs["run_tag_filter"].runs, {"active/#0001"}
            )
        self.telemetry.register(
            self.study, "retry", self.source | {"execution_identity": "job-2"}
        )
        self.emit("retry", {"metric-0": 0.5})
        self.exporter.poll()
        self.reload()
        self.assertEqual(
            set(self.get("scalars").get_json()),
            {"active/#0001", "active/#0001 (attempt 2)"},
        )
        self.assertIs(self.context.data_provider, self.provider)
        self.assertTrue(
            all(plugin._data_provider is self.provider for plugin in self.plugins)
        )

    def test_no_scope_unchanged_invalid_scope_rejected_and_concurrent_scopes_isolated(
        self,
    ):
        with (
            patch.object(
                self.provider, "list_scalars", wraps=self.provider.list_scalars
            ) as scalars,
            patch.object(
                self.provider, "list_runs", wraps=self.provider.list_runs
            ) as runs,
        ):
            global_tags = self.client.get(
                "/tensorboard/data/plugin/scalars/tags"
            ).get_json()
            self.assertEqual(len(global_tags["active/#0001"]), 34)
            self.assertEqual(len(global_tags["active/#0002"]), 1001)
            self.assertNotIn("run_tag_filter", scalars.call_args.kwargs)
            self.assertEqual(runs.call_count, 0)
            response = self.client.get(
                "/tensorboard/data/plugin/scalars/tags?dashboard_task=invalid"
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(scalars.call_count, 1)
        original = self.provider.list_scalars
        barrier = Barrier(2)
        filters = []

        def concurrent_listing(*args, **kwargs):
            filters.append(kwargs["run_tag_filter"].runs)
            barrier.wait(timeout=5)
            return original(*args, **kwargs)

        def request(experiment):
            response = self.get("scalars", experiment, self.Client(self.app))
            self.assertEqual(response.status_code, 200)
            return response.get_json()

        with (
            patch.object(self.provider, "list_scalars", side_effect=concurrent_listing),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            first, second = list(executor.map(request, ("0001", "0002")))
        self.assertEqual(set(first), {"active/#0001"})
        self.assertEqual(set(second), {"active/#0002"})
        self.assertEqual(
            set(filters), {frozenset({"active/#0001"}), frozenset({"active/#0002"})}
        )

    def test_provider_intersects_existing_tags_and_runs_and_delegates_reads(self):
        from tensorboard.data.provider import Run, RunTagFilter

        underlying = Mock()
        underlying.list_runs.return_value = [
            Run("opaque-id", "active/#0001", None),
            Run("other-id", "active/#0002", None),
        ]
        provider = tensorboard_plugins._ScopedProvider(
            underlying, Scope("active", experiment="0001")
        )
        original_filter = RunTagFilter(
            runs=["active/#0001", "active/#0002"], tags=["exact condition tag"]
        )
        for method in ("list_scalars", "list_tensors", "list_blob_sequences"):
            getattr(provider, method)(
                "context",
                experiment_id="",
                plugin_name="plugin",
                run_tag_filter=original_filter,
            )
            call = getattr(underlying, method).call_args
            self.assertEqual(call.args, ("context",))
            self.assertEqual(call.kwargs["run_tag_filter"].runs, {"active/#0001"})
            self.assertEqual(
                call.kwargs["run_tag_filter"].tags, {"exact condition tag"}
            )
        self.assertEqual(underlying.list_runs.call_count, 1)
        provider.list_scalars(
            experiment_id="",
            plugin_name="scalars",
            run_tag_filter=RunTagFilter(runs=["active/#0002"]),
        )
        self.assertEqual(
            underlying.list_scalars.call_args.kwargs["run_tag_filter"].runs, set()
        )
        self.assertEqual(original_filter.runs, {"active/#0001", "active/#0002"})
        self.assertIs(provider.read_scalars, underlying.read_scalars)


if __name__ == "__main__":
    unittest.main()
