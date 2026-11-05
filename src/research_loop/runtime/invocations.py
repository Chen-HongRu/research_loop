"""Durable local invocation journals; no coordinator or database capability.

A claimed token is never launched twice. Missing worker evidence remains unknown;
only a fenced retirement or an exited callback produces sender-retirement proof.
"""

from contextlib import contextmanager
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import uuid

from .files import (
    _sync_directory,
    publish_artifact,
    publish_json,
    validate_artifact_ref,
)


def _json(value):
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is None or type(item) in (str, int, bool):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            raise ValueError("Expected finite JSON")

    check(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _read(path):
    return json.loads(Path(path).read_text())


def _module_identity(path):
    metadata = Path(path).stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Invocation module must be an existing local file")
    return dict(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def _check_module(path, identity):
    if _module_identity(path) != identity:
        raise ValueError("Frozen invocation module identity changed")


def _load_module(path, identity):
    """Import the selected source, checking metadata on both sides of import.

    Compiling that source directly avoids a timestamp-rounded bytecode cache
    selecting an older module. Dependencies remain the trusted module's concern.
    """
    _check_module(path, identity)
    specification = importlib.util.spec_from_file_location(
        "runtime_application_" + uuid.uuid4().hex, path
    )
    if specification is None or specification.loader is None:
        raise ValueError("Cannot load invocation module")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    try:
        source = Path(path).read_bytes()
        _check_module(path, identity)
        exec(compile(source, str(path), "exec"), module.__dict__)
        _check_module(path, identity)
    except BaseException:
        sys.modules.pop(specification.name, None)
        raise
    return module


def _identity(root, reference):
    stat = validate_artifact_ref(root, reference).stat()
    return dict(
        device=stat.st_dev,
        inode=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _check_input(root, claim):
    if _identity(root, claim["binding"]["input_ref"]) != claim["input_identity"]:
        raise ValueError("Immutable invocation input changed")


def _folder(root, token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
        raise ValueError("Invalid launch token")
    return Path(root) / "invocations" / token


def _ensure_folder(root, token):
    folder = _folder(root, token)
    folder.parent.mkdir(parents=True, exist_ok=True)
    _sync_directory(Path(root))
    folder.mkdir(exist_ok=True)
    _sync_directory(folder.parent)
    return folder


@contextmanager
def _locked(folder, *, blocking=False):
    descriptor = os.open(folder / "lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _binding(kind, input_ref, deadline):
    if not isinstance(kind, str) or kind not in {"agent", "backend"}:
        raise ValueError("Invocation kind must be agent or backend")
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("Invocation deadline must be a finite UTC timestamp")
    return dict(kind=kind, input_ref=input_ref, deadline=deadline)


def _publish_completion(
    root, folder, claim, status, *, result=None, error=None, exit_code=None
):
    pointer = folder / "completion.json"
    if pointer.exists():
        return _read(pointer)["completion_ref"]
    binding = claim["binding"]
    reference = publish_artifact(
        root,
        dict(
            version=1,
            launch_token=claim["launch_token"],
            kind=binding["kind"],
            input_ref=binding["input_ref"],
            adapter=claim["adapter"],
            status=status,
            result=result,
            error=error,
            sender_retired=True,
            exit_code=exit_code,
        ),
    )
    publish_json(pointer, {"completion_ref": reference})
    return reference


class LocalProcessInvoker:
    def __init__(
        self,
        root,
        module_path,
        identity,
        config=None,
        python=sys.executable,
        *,
        module_identity=None,
    ):
        self.root = Path(root).resolve()
        self.module_path = Path(module_path).resolve()
        if (
            not isinstance(identity, dict)
            or set(identity) != {"id", "version"}
            or any(
                not isinstance(value, str) or not value.strip()
                for value in identity.values()
            )
        ):
            raise ValueError("Adapter identity requires nonblank id and version")
        if config is not None and not isinstance(config, dict):
            raise ValueError("Invocation config must be an object")
        module_identity = (
            _module_identity(self.module_path)
            if module_identity is None
            else module_identity
        )
        if (
            not isinstance(module_identity, dict)
            or set(module_identity) != {"device", "inode", "size", "mtime_ns"}
            or any(type(value) is not int for value in module_identity.values())
        ):
            raise ValueError("Module identity requires exact file metadata")
        self._configuration = _json(
            dict(
                adapter=identity,
                module_path=str(self.module_path),
                module_identity=module_identity,
                config={} if config is None else config,
                python=str(python),
            )
        )

    @property
    def identity(self):
        return json.loads(self._configuration)["adapter"]

    def _claim(self, token, binding):
        return dict(
            **json.loads(self._configuration),
            launch_token=token,
            binding=binding,
            input_identity=_identity(self.root, binding["input_ref"]),
        )

    def _check_configuration(self, claim):
        # Source replacement fences new effects, not access to an existing
        # sender's completion/cancellation/retirement evidence.
        keys = ("adapter", "module_path", "config", "python")
        actual = {key: claim[key] for key in keys}
        expected = json.loads(self._configuration)
        if _json(actual) != _json({key: expected[key] for key in keys}):
            raise ValueError("Frozen invocation adapter configuration differs")

    def _check_binding(self, claim, binding):
        self._check_configuration(claim)
        if _json(claim["binding"]) != _json(binding):
            raise ValueError("Launch token reused with a different binding")

    def inspect(self, launch_token):
        folder = _folder(self.root, launch_token)
        empty = dict(
            status="unknown", completion_ref=None, proof_ref=None, worker_identity=None
        )
        if not folder.exists():
            return empty
        claim_path = folder / "claimed.json"
        retired_path = folder / "retired.json"
        claim = (
            _read(claim_path)
            if claim_path.exists()
            else _read(retired_path)
            if retired_path.exists()
            else None
        )
        if claim is not None:
            self._check_configuration(claim)
        started = folder / "started.json"
        worker = _read(started) if started.exists() else None
        pointer = folder / "completion.json"
        if pointer.exists():
            reference = _read(pointer)["completion_ref"]
            result = _read(validate_artifact_ref(self.root, reference))
            if (
                claim is None
                or result["launch_token"] != launch_token
                or result["adapter"] != claim["adapter"]
                or result["kind"] != claim["binding"]["kind"]
                or result["input_ref"] != claim["binding"]["input_ref"]
                or result["sender_retired"] is not True
            ):
                raise ValueError("Completion differs from invocation binding")
            return dict(
                status=result["status"],
                completion_ref=reference,
                proof_ref=reference,
                worker_identity=worker,
            )
        with _locked(folder) as acquired:
            running = worker is not None and not acquired
        return empty | dict(
            status="running" if running else "unknown", worker_identity=worker
        )

    def _inspect_bound(self, launch_token, binding):
        observation = self.inspect(launch_token)
        folder = _folder(self.root, launch_token)
        prior = next(
            (
                folder / name
                for name in ("claimed.json", "retired.json")
                if (folder / name).exists()
            ),
            None,
        )
        if prior is None:
            return dict(
                status="unknown",
                completion_ref=None,
                proof_ref=None,
                worker_identity=None,
            )
        # inspect binds a completion to this immutable journal record. Recheck
        # its requested binding even when another writer held the lifetime lock.
        self._check_binding(_read(prior), binding)
        return observation

    def launch(self, launch_token, kind, input_ref, deadline):
        binding = _binding(kind, input_ref, deadline)
        folder = _ensure_folder(self.root, launch_token)
        with _locked(folder) as acquired:
            prior = next(
                (
                    folder / name
                    for name in ("claimed.json", "retired.json")
                    if (folder / name).exists()
                ),
                None,
            )
            if prior is not None:
                self._check_binding(_read(prior), binding)
            elif acquired:
                claim = self._claim(launch_token, binding)
                _check_module(claim["module_path"], claim["module_identity"])
                publish_json(folder / "claimed.json", claim)
                with (folder / "supervisor.log").open("ab") as log:
                    try:
                        process = subprocess.Popen(
                            [
                                claim["python"],
                                "-m",
                                "research_loop.runtime.worker_process",
                                "--supervise",
                                str(self.root),
                                launch_token,
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                            close_fds=True,
                        )
                        # Reap only our supervisor; journal/lock evidence alone
                        # determines status, including in a new invoker instance.
                        threading.Thread(target=process.wait, daemon=True).start()
                    except OSError:
                        # A durable claim never turns back into permission to send.
                        pass
        return self._inspect_bound(launch_token, binding)

    def cancel(self, launch_token, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Cancellation requires a reason")
        folder = _ensure_folder(self.root, launch_token)
        try:
            publish_json(folder / "cancel.json", {"reason": reason[:1000]})
        except FileExistsError:
            pass
        return self.inspect(launch_token)

    def retire_unlaunched(self, launch_token, binding):
        if not isinstance(binding, dict) or set(binding) != {
            "kind",
            "input_ref",
            "deadline",
        }:
            raise ValueError("Retirement requires the exact invocation binding")
        binding = _binding(**binding)
        folder = _ensure_folder(self.root, launch_token)
        with _locked(folder) as acquired:
            claimed = folder / "claimed.json"
            retired = folder / "retired.json"
            prior = (
                claimed if claimed.exists() else retired if retired.exists() else None
            )
            claim = _read(prior) if prior is not None else None
            if claim is not None:
                self._check_binding(claim, binding)
            if acquired and not claimed.exists():
                if claim is None:
                    claim = self._claim(launch_token, binding)
                    publish_json(retired, claim)
                _publish_completion(self.root, folder, claim, "not_started")
        return self._inspect_bound(launch_token, binding)
