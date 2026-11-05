"""YAML-only runtime configuration; other public protocol documents stay JSON."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from research_loop.cli import main
from research_loop.runtime.config_yaml import load_config


class RuntimeConfigYAMLTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_yaml_extensions_and_yaml12_scalars(self):
        for extension in (".yaml", ".yml"):
            with self.subTest(extension=extension):
                path = self.base / ("config" + extension)
                path.write_text(
                    "# Synthetic runtime configuration\n"
                    "goal: Synthetic question\n"
                    "runtime:\n  reasoning: off\n  planning: false\n"
                    "permissions: {}\n"
                )
                self.assertEqual(
                    load_config(path),
                    {
                        "goal": "Synthetic question",
                        "runtime": {"reasoning": "off", "planning": False},
                        "permissions": {},
                    },
                )

    def test_rejects_duplicate_keys_unsafe_tags_and_non_mapping_roots(self):
        invalid_documents = (
            "goal: first\ngoal: second\n",
            "runtime:\n  worker_slots: 1\n  worker_slots: 2\n",
            "runtime:\n  roles:\n    - reasoning: off\n      reasoning: high\n",
            "base: &base {cap: 1}\nbudget: {<<: *base, cap: 2, cap: 3}\n",
            "%YAML 1.1\n---\nreasoning: off\n",
            "%YAML 1.3\n---\nreasoning: off\n",
            "!!python/object/apply:builtins.dict []\n",
            "runtime: !!python/object/apply:builtins.dict []\n",
            "- goal: Synthetic question\n",
            "Synthetic scalar\n",
            "null\n",
            "",
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                path = self.base / "config.yaml"
                path.write_text(document)
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_init_and_migrate_prepare_reject_other_extensions_before_work(self):
        # Valid YAML/JSON content must not admit a retired config filename.
        for extension in (".json", ".txt", ""):
            for command in ("init", "migrate"):
                with self.subTest(extension=extension, command=command):
                    path = self.base / ("config" + extension)
                    path.write_text("{}")
                    root = self.base / "study"
                    arguments = ["runtime", command]
                    if command == "migrate":
                        arguments.append("prepare")
                    arguments.extend(
                        [
                            str(root),
                            "--config",
                            str(path),
                            "--application",
                            str(self.base / "missing.py"),
                        ]
                    )
                    if command == "migrate":
                        arguments.extend(["--import-id", "synthetic"])
                        for name in ("inventory", "translation", "fence", "deployment"):
                            arguments.extend(
                                [
                                    "--" + name,
                                    str(self.base / (name + ".json")),
                                ]
                            )
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        self.assertEqual(main(arguments), 1)
                    self.assertIn(".yaml", errors.getvalue())
                    self.assertIn(".yml", errors.getvalue())
                    self.assertEqual(output.getvalue(), "")
                    self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
