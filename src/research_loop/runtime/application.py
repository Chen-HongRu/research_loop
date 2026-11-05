"""Load an explicitly trusted task application, without invoking its callbacks.

An application exports APPLICATION_IDENTITY, invoke(kind, payload, config), and
validate(role, output, context, config). Import must have no external effects.
Local children must remain in the callback process group; detached submitters
cannot be retired by the local invocation supervisor. This is a trust contract,
not an OS sandbox or a fingerprint of imported dependencies or the environment.
"""

from dataclasses import dataclass
import json
from pathlib import Path

from .invocations import (
    LocalProcessInvoker,
    _check_module,
    _json,
    _load_module,
    _module_identity,
)


ROLES = ("scout", "advocate", "judge", "plan", "execute", "review")


def _adapter(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "version"}
        or any(not isinstance(item, str) or not item.strip() for item in value.values())
    ):
        raise ValueError("Application identity requires nonblank id and version")
    return json.loads(_json(value))


def _application(module_path, identity, adapter):
    module = _load_module(module_path, identity)
    if _adapter(getattr(module, "APPLICATION_IDENTITY", None)) != adapter:
        raise ValueError("Application identity differs from the frozen worker adapter")
    if not callable(getattr(module, "invoke", None)):
        raise ValueError("Application must define invoke(kind, payload, config)")
    if not callable(getattr(module, "validate", None)):
        raise ValueError(
            "Application must define validate(role, output, context, config)"
        )
    return module


def prepare_application(module_path, application_config, adapter):
    """Validate a trusted module and detach its JSON configuration for init.

    Module import reads its Python source, with O(1) metadata checks and no
    hashes. Neither callback is called. The result belongs in runtime.application.
    """
    adapter = _adapter(adapter)
    if not isinstance(application_config, dict):
        raise ValueError("Application config must be an object")
    configuration = _json(application_config)
    path = Path(module_path).resolve()
    identity = _module_identity(path)
    _application(path, identity, adapter)
    return dict(
        module=str(path), module_identity=identity, config=json.loads(configuration)
    )


@dataclass(frozen=True)
class Application:
    invoker: LocalProcessInvoker
    validators: dict


def load_application(owner):
    """Load only the immutable application selected in the SQLite config."""
    runtime = owner.snapshot()["config"].get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("Runtime configuration is required")
    adapter = _adapter(runtime.get("worker_adapter"))
    spec = runtime.get("application")
    if not isinstance(spec, dict) or set(spec) != {
        "module",
        "module_identity",
        "config",
    }:
        raise ValueError("Runtime application requires a prepared module spec")
    if not isinstance(spec["module"], str) or not Path(spec["module"]).is_absolute():
        raise ValueError("Application module must be an absolute path")
    if not isinstance(spec["config"], dict):
        raise ValueError("Application config must be an object")
    configuration = _json(spec["config"])
    invoker = LocalProcessInvoker(
        owner.store.root,
        spec["module"],
        adapter,
        spec["config"],
        module_identity=spec["module_identity"],
    )
    module = _application(spec["module"], spec["module_identity"], adapter)

    def validator(role):
        def validate(output, context):
            _check_module(spec["module"], spec["module_identity"])
            normalized = module.validate(
                role, output, context, json.loads(configuration)
            )
            _check_module(spec["module"], spec["module_identity"])
            if not isinstance(normalized, dict):
                raise ValueError("Application validator must return an object")
            return json.loads(_json(normalized))

        return validate

    return Application(invoker, {role: validator(role) for role in ROLES})
