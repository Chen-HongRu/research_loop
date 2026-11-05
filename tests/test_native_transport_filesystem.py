"""Focused host-isolation boundaries; no model or network is involved."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from integrations.native_runtime import filesystem
from research_loop.harness import HarnessError


class NativeFilesystemTests(unittest.TestCase):
    def test_capabilities_reject_escape_overlap_and_unbound_writes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "library"
            library.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (library / "escape").symlink_to(outside, target_is_directory=True)
            for value, tools in (
                ({"read_roots": str(library)}, []),
                (
                    {
                        "read_roots": [str(library)],
                        "mounts": {"../escape": str(library)},
                    },
                    [],
                ),
                (
                    {
                        "read_roots": [str(library)],
                        "mounts": {"escape": str(library / "escape")},
                    },
                    [],
                ),
                ({"read_roots": [str(library)], "write_root": str(root)}, ["write"]),
                ({}, ["write"]),
            ):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    filesystem.capabilities(value, tools)

    def test_profile_contains_references_and_preserves_native_model_overrides(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            provider = {
                "apiKey": "prefix-$$-$!-${SYNTHETIC_TOKEN}",
                "headers": {"X-Test": "$SYNTHETIC_TOKEN"},
                "models": [{"id": "synthetic"}],
                "modelOverrides": {
                    "synthetic": {
                        "maxTokens": 321,
                        "headers": {"X-Override": "$SYNTHETIC_TOKEN"},
                    }
                },
            }
            (source / "models.json").write_text(
                json.dumps({"providers": {"fixture": provider}})
            )
            (source / "auth.json").write_text(
                json.dumps(
                    {
                        "other-provider": {
                            "type": "oauth",
                            "access": "synthetic-unrelated-secret",
                        }
                    }
                )
            )
            target = root / "isolated"
            with patch.dict(
                os.environ,
                {
                    "PI_CODING_AGENT_DIR": str(source),
                    "SYNTHETIC_TOKEN": "synthetic-value",
                },
            ):
                environment = filesystem.prepare_agent(target, "fixture")
            rendered = (target / "models.json").read_text()
            self.assertNotIn("synthetic-value", rendered)
            self.assertNotIn("synthetic-unrelated-secret", rendered)
            self.assertEqual((target / "auth.json").read_text(), "{}")
            selected = json.loads(rendered)["providers"]["fixture"]
            self.assertEqual(selected["models"], [{"id": "synthetic"}])
            self.assertEqual(selected["modelOverrides"]["synthetic"]["maxTokens"], 321)
            self.assertEqual(
                environment[selected["apiKey"][1:]], "prefix-$-!-synthetic-value"
            )
            for path in target.iterdir():
                if path.is_file():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o400)

    def test_oauth_and_credential_commands_fail_closed(self):
        for credential in (
            {"type": "oauth", "access": "synthetic"},
            {"type": "api_key", "key": "!echo forbidden"},
        ):
            with self.subTest(kind=credential["type"]), TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                source.mkdir()
                (source / "auth.json").write_text(json.dumps({"fixture": credential}))
                with (
                    patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(source)}),
                    self.assertRaises(HarnessError),
                ):
                    filesystem.prepare_agent(root / "isolated", "fixture")

    def test_linux_binds_only_the_stable_authorized_root(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized = root / "published-library"
            (authorized / "current").mkdir(parents=True)
            workspace, control, state = (
                root / name for name in ("workspace", "control", "state")
            )
            for path in (workspace, control, state, state / "tmp"):
                path.mkdir()
            with (
                patch.object(
                    filesystem,
                    "_pi_install",
                    return_value=(["/usr/bin/node", "/runtime/pi.js"], root),
                ),
                patch.object(filesystem, "_runtime_files", return_value=([], [])),
                patch.object(filesystem.sys, "platform", "linux"),
                patch.object(filesystem.shutil, "which", return_value="/usr/bin/bwrap"),
            ):
                arguments = filesystem.command(
                    "pi",
                    workspace,
                    control,
                    state,
                    str(root / "bridge/socket"),
                    [authorized],
                    None,
                )
            bindings = [
                arguments[index + 1 : index + 3]
                for index, item in enumerate(arguments)
                if item == "--ro-bind"
            ]
            self.assertIn([str(authorized), str(authorized)], bindings)
            self.assertNotIn(
                [str(authorized / "current"), str(authorized / "current")], bindings
            )
            self.assertNotIn([str(root), str(root)], bindings)
            self.assertNotIn("--proc", arguments)

    def test_missing_linux_bubblewrap_fails_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(
                    filesystem,
                    "_pi_install",
                    return_value=(["/usr/bin/node", "/runtime/pi.js"], root),
                ),
                patch.object(filesystem, "_runtime_files", return_value=([], [])),
                patch.object(filesystem.sys, "platform", "linux"),
                patch.object(filesystem.shutil, "which", return_value=None),
                self.assertRaisesRegex(HarnessError, "refusing unisolated"),
            ):
                filesystem.command(
                    "pi", root, root, root, str(root / "socket"), [], None
                )
