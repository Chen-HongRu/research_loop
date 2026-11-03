"""Synthetic checks of task identity and TensorBoard JSON response boundaries."""

import copy
import json
import unittest
from urllib.parse import quote, urlencode

from integrations.protok import tensorboard_scope as scope_api


class TensorBoardScopeTests(unittest.TestCase):
    sources = {"active", "history-1", "history-2"}

    def request(self, value, path="/tensorboard/data/runs", extra=""):
        return scope_api.parse_request(
            path + "?" + urlencode({"dashboard_task": json.dumps(value)}) + extra,
            self.sources,
        )

    def scope(self, **identity):
        return self.request({"source": "active", **identity})[1]

    def test_experiment_boundaries_attempts_and_overflow_stream(self):
        scope = self.scope(experiment="0026", run=None, task=None)
        for run in (
            "active/#0026",
            "active/#0026 (attempt 2)",
            "active/#0026/step-out-of-int64-event-order",
            "active/#0026 (attempt 12)/step-out-of-int64-event-order",
        ):
            self.assertTrue(scope.matches(run), run)
        for run in (
            "history-1/#0026",
            "active/#00260",
            "active/#0026-other",
            "active/#0026 (attempt 0)",
            "active/#0026 (attempt 02)",
            "active/#0026/other",
            "active/long/0026",
            None,
        ):
            self.assertFalse(scope.matches(run), run)

    def test_long_and_manual_identities_do_not_overlap(self):
        run_id = "L0123456789abcdef"
        long = self.scope(run=run_id)
        self.assertTrue(long.matches("active/long/" + run_id + " (attempt 3)"))
        self.assertFalse(long.matches("active/manual/" + run_id + "/#0001"))
        self.assertFalse(long.matches("history-1/long/" + run_id))
        manual = self.scope(task="task_a-1")
        for run in (
            "active/manual/task_a-1/#0001",
            "active/manual/task_a-1/#00021 (attempt 2)/step-out-of-int64-event-order",
        ):
            self.assertTrue(manual.matches(run), run)
        for run in (
            "active/manual/task_a-10/#0001",
            "active/manual/task_a-1/#001",
            "history-1/manual/task_a-1/#0001",
            "active/manual/task_a-1/#0001/extra",
            "active/long/task_a-1",
        ):
            self.assertFalse(manual.matches(run), run)
        history = self.request({"source": "history-2", "experiment": "0026"})[1]
        self.assertTrue(history.matches("history-2/#0026"))
        self.assertFalse(history.matches("history-1/#0026"))

    def test_invalid_scopes_cannot_fall_back_to_global(self):
        invalid = (
            None,
            [],
            "active",
            {},
            {"source": "active"},
            {"source": "active", "experiment": "0026", "task": "manual"},
            {"source": "active", "experiment": "0026", "regex": ".*"},
            {"source": "history-3", "experiment": "0026"},
            {"source": "history-0", "experiment": "0026"},
            {"source": "active/other", "experiment": "0026"},
            {"source": "active", "experiment": 26},
            {"source": "active", "experiment": "26"},
            {"source": "active", "experiment": ".*"},
            {"source": "active", "run": "L0123456789abcdeF"},
            {"source": "active", "task": "../task"},
            {"source": "active", "task": "任务"},
            {"source": "active", "task": "_task"},
            {"source": "active", "task": "a" * 65},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.request(value)
        for raw in (
            "",
            "{",
            '{"source":"active","source":"active","task":"one"}',
            "x" * 1025,
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                scope_api.parse_request(
                    "/data/runs?dashboard_task=" + quote(raw), self.sources
                )
        with self.assertRaises(ValueError):
            self.request(
                {"source": "active", "task": "one"}, extra="&dashboard_task=null"
            )
        self.assertIsNotNone(self.scope(task="a" * 64))

    def test_upstream_query_preservation_and_pre_forward_run_rejection(self):
        query = "&run=" + quote("active/#0026") + "&tag=metric%20a%2Bb&tag=x%2fy&blank="
        upstream, scope = self.request(
            {"source": "active", "experiment": "0026"}, extra=query
        )
        self.assertEqual(upstream, "/tensorboard/data/runs?" + query[1:])
        self.assertTrue(scope.matches("active/#0026"))
        for run in ("history-1/#0026", "active/#00260", ""):
            with self.subTest(run=run), self.assertRaises(ValueError):
                self.request(
                    {"source": "active", "experiment": "0026"},
                    extra="&run=" + quote(run),
                )
        with self.assertRaises(ValueError):
            self.request(
                {"source": "active", "experiment": "0026"},
                extra="&run=active%2F%230026&run=active%2F%230027",
            )
        path = "/tensorboard/data/runs?run=any%2Frun&blank=&tag=one&tag=two"
        self.assertEqual(scope_api.parse_request(path, self.sources), (path, None))

    def test_optional_experiment_route_and_mount(self):
        for path in (
            "/tensorboard/data/runs?x=1",
            "/data/runs",
            "/tensorboard/experiment/default/data/runs",
            "/experiment/default/data/runs",
            "/tensorboard/experiment//data/runs",
            "/experiment//data/runs",
        ):
            self.assertEqual(scope_api.route(path), "/data/runs")
        self.assertEqual(scope_api.route("/tensorboard"), "/")
        self.assertEqual(
            scope_api.route("/tensorboard-other/data/runs"),
            "/tensorboard-other/data/runs",
        )

    def test_legacy_maps_and_multirun_preserve_selected_conditions(self):
        scope = self.scope(experiment="0026")
        selected = "active/#0026"
        attempt = selected + " (attempt 2)"
        unrelated = "history-1/#0026"
        runs = [unrelated, selected, "active/#00260", attempt]
        self.assertEqual(
            scope_api.filter_response("/data/runs", runs, scope), [selected, attempt]
        )
        conditions = {"loss [seed=1; dataset=synthetic-a]": {"displayName": "loss"}}
        payload = {unrelated: {"other": {}}, selected: conditions, attempt: conditions}
        for endpoint in ("scalars/tags", "text/tags", "scalars/scalars_multirun"):
            filtered = scope_api.filter_response(
                "/data/plugin/" + endpoint, payload, scope
            )
            self.assertEqual(filtered, {selected: conditions, attempt: conditions})
            self.assertIs(filtered[selected], conditions)

    def test_timeseries_tags_reduce_1000_unrelated_tags_to_34(self):
        scope = self.scope(task="selected")
        selected = "active/manual/selected/#0001"
        other = "active/manual/selected-other/#0001"
        tags = [
            f"metric/{index} [seed={index % 2}; dataset=synthetic]"
            for index in range(34)
        ]
        unrelated = [f"other/{index}" for index in range(1000)]
        payload = {
            "scalars": {
                "runTagInfo": {other: unrelated, selected: tags},
                "tagDescriptions": {
                    tag: "combined provenance" for tag in tags + unrelated
                },
            },
            "histograms": {
                "runTagInfo": {other: unrelated, selected: tags},
                "tagDescriptions": {},
            },
            "images": {
                "tagRunSampledInfo": {
                    **{tag: {other: {"maxSamplesPerStep": 2}} for tag in unrelated},
                    **{
                        tag: {
                            selected: {"maxSamplesPerStep": 1},
                            other: {"maxSamplesPerStep": 2},
                        }
                        for tag in tags
                    },
                },
                "tagDescriptions": {
                    tag: "combined provenance" for tag in tags + unrelated
                },
            },
        }
        original = copy.deepcopy(payload)
        filtered = scope_api.filter_response(
            "/data/plugin/timeseries/tags", payload, scope
        )
        self.assertEqual(filtered["scalars"]["runTagInfo"], {selected: tags})
        self.assertEqual(filtered["histograms"]["runTagInfo"], {selected: tags})
        self.assertEqual(list(filtered["images"]["tagRunSampledInfo"]), tags)
        self.assertEqual(len(filtered["images"]["tagRunSampledInfo"]), 34)
        for runs in filtered["images"]["tagRunSampledInfo"].values():
            self.assertEqual(runs, {selected: {"maxSamplesPerStep": 1}})
        self.assertTrue(
            all(not metadata["tagDescriptions"] for metadata in filtered.values())
        )
        self.assertEqual(payload, original)

    def test_timeseries_series_order_errors_and_multirun(self):
        scope = self.scope(experiment="0026")
        selected = "active/#0026"
        other = "active/#0027"
        payload = [
            {
                "plugin": "scalars",
                "tag": "loss",
                "runToSeries": {other: [2], selected: [1]},
            },
            {
                "plugin": "histograms",
                "tag": "weights",
                "run": other,
                "runToSeries": {other: [3]},
            },
            {
                "plugin": "images",
                "tag": "sample",
                "run": selected,
                "sample": 0,
                "runToSeries": {selected: [4]},
            },
            {"plugin": "invalid", "tag": "loss", "error": "Invalid plugin"},
            {"plugin": "scalars", "tag": "absent", "runToSeries": {}},
        ]
        original = copy.deepcopy(payload)
        filtered = scope_api.filter_response(
            "/data/plugin/timeseries/timeSeries", payload, scope
        )
        self.assertEqual(
            filtered,
            [
                {**payload[0], "runToSeries": {selected: [1]}},
                {**payload[1], "runToSeries": {}},
                *payload[2:],
            ],
        )
        self.assertEqual(payload, original)

    def test_no_scope_and_unrecognized_endpoints_are_unchanged(self):
        payload = {"error": "example", "run": "outside"}
        for route in scope_api.JSON_ROUTES:
            self.assertIs(scope_api.filter_response(route, payload, None), payload)
        self.assertIs(
            scope_api.filter_response(
                "/data/environment", payload, self.scope(task="one")
            ),
            payload,
        )


if __name__ == "__main__":
    unittest.main()
