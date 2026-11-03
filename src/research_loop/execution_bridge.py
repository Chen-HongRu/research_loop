"""Unified Agent/submission bridge for trusted, separately deployed execution adapters."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time

from .diagnostics import diagnostic_secrets, diagnostic_view
from .harness import HarnessError, publish_completed_response
from .protocol import AuthorReport, validate_response
from .schema import OutputSchema
from .submission import (
    INTERFACE_FILES,
    LOCK,
    RESULT,
    claim_request,
    status as submission_status,
    _locked,
)


REGISTRATION = "execution-backend.json"
HOOKS = {
    "commit_iteration",
    "call_allowance",
    "validate_plan",
    "response_schema",
    "diagnostic_context",
}


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    )
    temporary.replace(path)


def _git(workspace, *args):
    result = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True)
    if result.returncode:
        raise HarnessError("Cannot verify inherited submission interfaces")
    return result.stdout


def initialize_workspace(study_root, workspace, iteration):
    """Retire only unchanged Git-inherited transport files, once per workspace."""
    workspace = Path(workspace).resolve()
    marker = Path(study_root) / "iterations" / iteration / "submission-workspace.json"
    with _locked(workspace):
        if marker.exists():
            record = _read(marker)
            if record.get("workspace") != str(workspace):
                raise HarnessError(
                    "Submission workspace initialization identity changed"
                )
            if record.get("status") == "ready":
                return
            if record.get("status") != "initializing":
                raise HarnessError("Invalid submission workspace initialization")
        else:
            tracked = (
                _git(workspace, "ls-files", "--", *sorted(INTERFACE_FILES - {LOCK}))
                .decode()
                .splitlines()
            )
            record = {
                "workspace": str(workspace),
                "head": _git(workspace, "rev-parse", "HEAD").decode().strip(),
                "inherited": tracked,
                "status": "initializing",
            }
        if not isinstance(record.get("inherited"), list) or any(
            name not in INTERFACE_FILES - {LOCK} for name in record["inherited"]
        ):
            raise HarnessError("Invalid inherited submission interface manifest")
        if record["head"] != _git(workspace, "rev-parse", "HEAD").decode().strip():
            raise HarnessError(
                "Submission workspace HEAD changed during initialization"
            )
        for name in INTERFACE_FILES - {LOCK}:
            path = workspace / name
            if not os.path.lexists(path):
                continue
            if (
                name not in record["inherited"]
                or path.is_symlink()
                or not stat.S_ISREG(path.stat().st_mode)
                or path.stat().st_size > 1024 * 1024
                or path.read_bytes() != _git(workspace, "show", f"HEAD:{name}")
            ):
                raise HarnessError("Unreconciled submission interface: " + name)
        if not marker.exists():
            _write(marker, record)
        for name in record["inherited"]:
            (workspace / name).unlink(missing_ok=True)
        _write(marker, {**record, "status": "ready"})


def load_execution_harness(study_root, delegate, *, backend=None, backend_config=None):
    """Load an explicit adapter, or the Study's previously registered adapter."""
    study_root = Path(study_root).resolve()
    registered = study_root / REGISTRATION
    if (backend is None) != (backend_config is None):
        raise HarnessError("--backend and --backend-config must be supplied together")
    if backend is None:
        if not registered.exists():
            return delegate
        previous = _read(registered)
        backend, backend_config = previous["adapter"], previous["config"]
    adapter, config_path = Path(backend), Path(backend_config)
    if not adapter.is_absolute() or not config_path.is_absolute():
        raise HarnessError("Execution adapter and configuration paths must be absolute")
    adapter, config_path = adapter.resolve(), config_path.resolve()
    config = _read(config_path)
    if not isinstance(config, dict):
        raise HarnessError("Execution backend configuration must be a JSON object")
    binding = {
        "version": 1,
        "adapter": str(adapter),
        "config": str(config_path),
        "supports_qualitative_execution": True,
        "config_fingerprint": hashlib.sha256(
            json.dumps(config, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
    }
    if registered.exists() and _read(registered) != binding:
        raise HarnessError("Registered execution backend or configuration changed")
    name = (
        "research_loop_adapter_"
        + hashlib.sha256(str(adapter).encode()).hexdigest()[:16]
    )
    spec = importlib.util.spec_from_file_location(name, adapter)
    if spec is None or spec.loader is None:
        raise HarnessError("Cannot load execution backend module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "create_backend", None)):
        raise HarnessError("Execution adapter must define create_backend")
    return ExecutionHarness(study_root, delegate, module, config, binding)


class ExecutionHarness:
    """Keep raw Agent reports separate from controller-certified measurements."""

    supports_qualitative_execution = True

    def __init__(self, study_root, delegate, module, config, binding):
        self.root, self.delegate, self.module = Path(study_root), delegate, module
        self.config, self.binding = config, binding
        self._owns_run = False
        factory = getattr(module, "create_controller", None)
        self.controller = factory(config, study_root=self.root) if factory else None
        self.diagnostic_secrets = diagnostic_secrets(
            (
                *(getattr(delegate, "diagnostic_secrets", ()) or ()),
                *(getattr(self.controller, "diagnostic_secrets", ()) or ()),
            )
        )
        if hasattr(delegate, "diagnostic_secrets"):
            delegate.diagnostic_secrets = self.diagnostic_secrets

    def __getattr__(self, name):
        if name in HOOKS and self.controller is not None:
            return getattr(self.controller, name)
        raise AttributeError(name)

    def admit_iteration(self, iteration):
        # Core invokes admission only after acquiring this Study's controller lock.
        registration = self.root / REGISTRATION
        if registration.exists():
            if _read(registration) != self.binding:
                raise HarnessError("Registered execution backend changed")
        else:
            _write(registration, self.binding)
        self._owns_run = True
        admit = getattr(self.controller, "admit_iteration", None)
        return admit(iteration) if admit else None

    def close(self):
        if not self._owns_run:
            return
        self._owns_run = False
        close = getattr(self.controller, "close", None)
        if close:
            close()

    def _backend(self, workspace, run_dir, context, stop, deadline):
        return self.module.create_backend(
            self.config,
            study_root=self.root,
            workspace=Path(workspace),
            run_dir=Path(run_dir),
            context=context,
            stop=stop,
            deadline=deadline,
        )

    def _deadline(self, run_dir, timeout):
        budget = Path(run_dir) / "parameter-budget.json"
        deadline = (
            _read(budget)["deadline"] if budget.exists() else time.time() + timeout
        )
        return min(deadline, time.time() + timeout)

    def _inspect(self, backend):
        result = backend.inspect()
        validate_response("execute", result)
        return result

    def validate_completed(self, result, *, workspace, run_dir):
        run_dir = Path(run_dir)
        context = _read(run_dir / "context.json")
        binding = _read(run_dir / "execution-backend.json")
        if binding != self.binding:
            raise HarnessError("Execute backend binding changed")
        execution = context["plan"].get("execution")
        validate_response("execute", result)
        if execution is None:
            if result["artifacts"] or result["measurements"]:
                raise HarnessError(
                    "Qualitative Execute cannot produce artifacts or measurements"
                )
        else:
            stop = threading.Event()
            stop.set()
            backend = self._backend(
                workspace, run_dir, context, stop, self._deadline(run_dir, 1)
            )
            trusted = self._inspect(backend)
            terminal = "completed" if trusted["outcome"] == "completed" else "failed"
            interface = submission_status(workspace)
            if (
                interface["status"] != terminal
                or interface.get("result") != {"status": terminal, "response": trusted}
                or trusted != result
                or _read(run_dir / "backend-response.json") != trusted
            ):
                raise HarnessError(
                    "Execute differs from its retained trusted backend result"
                )
        validate = getattr(self.controller, "validate_completed", None)
        if (
            validate
            and validate(result, workspace=workspace, run_dir=run_dir) != result
        ):
            raise HarnessError(
                "Controller validation changed the trusted Execute response"
            )
        return result

    def _delegate(
        self, kwargs, context, *, writable, report_only=False, qualitative=False
    ):
        outer = Path(kwargs["run_dir"])
        agent = outer / "agent"
        agent.mkdir(exist_ok=True)
        _write(agent / "context.json", context)
        forwarded = {**kwargs, "run_dir": agent, "writable": writable}
        if report_only:
            forwarded["schema"] = OutputSchema(AuthorReport)
            submission = Path(__file__).with_name("submission.py")
            forwarded["prompt"] += (
                "\n\nThe controller owns backend execution and all structured measurements. "
                "Author only allowed source under the accepted Plan. Submit once using "
                f"`{sys.executable} {submission} submit --workspace . --action research`; "
                f"poll `{sys.executable} {submission} status --workspace .` quietly until terminal. "
                "Never launch compute, network commands, or hand-write/delete transport files. "
                "parameter_error permits correcting only authorized source/request parameters "
                "within the same original deadline. accepted means controller claim, not backend "
                "submission. Unknown effects require reconciliation. Return only outcome and "
                "markdown: do not invent artifacts, scores, or measurements."
            )
        elif qualitative:
            forwarded["prompt"] += (
                "\n\nThis is a qualitative, read-only Execute. Inspect existing evidence only; "
                "no computation, submission, artifacts or measurements are authorized. "
                "Return artifacts=[] and measurements=[]."
            )
        return self.delegate.run(**forwarded)

    def _watch(self, workspace, run_dir, context, stop, deadline):
        backend = None
        prepared = {}

        def preflight(request):
            if request != {"action": "research"}:
                from .correction import ParameterError

                raise ParameterError("Script execution requires action=research")
            if stop.is_set() or time.time() >= deadline:
                raise HarnessError("Execute ended before submission claim")
            prepared["value"] = backend.prepare()
            if stop.is_set() or time.time() >= deadline:
                raise HarnessError("Execute ended during backend preparation")

        try:
            backend = self._backend(workspace, run_dir, context, stop, deadline)
            while not stop.wait(0.05):
                if time.time() >= deadline:
                    raise HarnessError(
                        "Execute deadline expired without a settled backend result"
                    )
                request = claim_request(workspace, preflight=preflight)
                if request is None:
                    continue
                if stop.is_set() or time.time() >= deadline:
                    raise HarnessError(
                        "Execute ended after claim; retained effects require inspection"
                    )
                started = backend.start(prepared.pop("value"))
                validate_response("execute", started)
                trusted = self._inspect(backend)
                if started != trusted:
                    raise HarnessError("Backend start and inspection disagree")
                _write(Path(run_dir) / "backend-response.json", trusted)
                _write(
                    Path(workspace) / RESULT,
                    {
                        "status": "completed"
                        if trusted["outcome"] == "completed"
                        else "failed",
                        "response": trusted,
                    },
                )
                return trusted
            return None
        except BaseException as error:
            diagnostics = diagnostic_view(
                error,
                owner="pipeline",
                stage="execution_backend",
                secrets=self.diagnostic_secrets,
            )
            _write(Path(run_dir) / "backend-failure.json", diagnostics)
            _write(
                Path(workspace) / RESULT,
                {
                    "status": "error",
                    "retry": False,
                    "error": diagnostics["cause_chain"][0]["message"],
                },
            )
            raise

    def run(self, **kwargs):
        outer = Path(kwargs["run_dir"])
        context = _read(outer / "context.json")
        stage = re.search(r"^Stage: (\w+)$", kwargs["prompt"], re.MULTILINE)
        if stage is None:
            raise HarnessError("Missing research stage")
        phase = stage[1]
        if phase != "execute":
            result = self._delegate(kwargs, context, writable=False)
            publish_completed_response(outer, result)
            return result
        _write(outer / "execution-backend.json", self.binding)
        execution = context["plan"].get("execution")
        if execution is None:
            result = self._delegate(kwargs, context, writable=False, qualitative=True)
            result = self.validate_completed(
                result, workspace=kwargs["workspace"], run_dir=outer
            )
            publish_completed_response(outer, result)
            return result
        if execution.get("mode") != "script" or not kwargs["writable"]:
            raise HarnessError(
                "Unified execution bridge supports authorized writable script Plans only"
            )
        workspace = Path(kwargs["workspace"])
        initialize_workspace(self.root, workspace, context["experiment"]["id"])
        if submission_status(workspace)["status"] != "not_submitted":
            raise HarnessError(
                "Retained submission requires reconciliation; no action was repeated"
            )
        deadline = self._deadline(outer, kwargs["timeout_seconds"])
        stop = threading.Event()
        pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="research-execution"
        )
        future = pool.submit(self._watch, workspace, outer, context, stop, deadline)
        agent_error = None
        try:
            try:
                report = self._delegate(
                    kwargs, context, writable=True, report_only=True
                )
                OutputSchema(AuthorReport).validate(report)
            except BaseException as error:
                agent_error = error
            finally:
                stop.set()
            try:
                result = future.result(timeout=max(0, deadline - time.time()) + 5)
            except TimeoutError as error:
                if isinstance(agent_error, (KeyboardInterrupt, SystemExit)):
                    raise agent_error from error
                raise HarnessError(
                    "Backend did not settle within its original deadline; effects remain unknown"
                ) from error
            except BaseException as error:
                if isinstance(agent_error, (KeyboardInterrupt, SystemExit)):
                    raise agent_error from error
                raise
            if isinstance(agent_error, (KeyboardInterrupt, SystemExit)):
                raise agent_error
            if result is None:
                if agent_error:
                    raise agent_error
                raise HarnessError("Execute ended without a trusted backend result")
            result = self.validate_completed(result, workspace=workspace, run_dir=outer)
            publish_completed_response(outer, result)
            return result
        finally:
            stop.set()
            pool.shutdown(wait=False, cancel_futures=True)
