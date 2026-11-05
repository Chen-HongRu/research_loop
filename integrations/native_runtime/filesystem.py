"""Host file isolation for Pi. No native tool is wrapped or intercepted.

The installation and OS runtime are trusted code inputs. Study, library and
credential directories never become runtime roots. Unsupported hosts fail closed.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from research_loop.harness import HarnessError

NATIVE_TOOLS = frozenset({"ls", "find", "grep", "read", "edit", "write"})


def native_binary(name):
    configured = (
        Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
        / "bin"
        / name
    )
    for candidate in (
        configured,
        Path.home() / ".pi/agent/bin" / name,
        Path(shutil.which(name) or "/nonexistent"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def capabilities(value, tools):
    value = {} if value is None else value
    if not isinstance(value, dict) or value.keys() - {
        "read_roots",
        "write_root",
        "mounts",
    }:
        raise ValueError("Invalid Pi filesystem capabilities")
    roots = []
    supplied_roots = value.get("read_roots", [])
    if not isinstance(supplied_roots, (list, tuple)):
        raise ValueError("Pi read roots must be a list of absolute directories")
    for item in supplied_roots:
        if not isinstance(item, str):
            raise ValueError("Pi read roots must contain path strings")
        path = Path(item)
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Pi read roots must be existing absolute directories")
        roots.append(path.resolve())
    writable = value.get("write_root")
    if writable is not None:
        writable = Path(writable)
        if not writable.is_absolute() or not writable.is_dir():
            raise ValueError("Pi draft root must be an existing absolute directory")
        writable = writable.resolve()
        if any(
            writable.is_relative_to(root) or root.is_relative_to(writable)
            for root in roots
        ):
            raise ValueError("Pi draft and library roots must be disjoint")
    if {"edit", "write"} & set(tools) and writable is None:
        raise ValueError("Pi edit/write require a private draft root")
    mounts = value.get("mounts", {})
    if not isinstance(mounts, dict):
        raise ValueError("Pi mounts must map aliases to live library directories")
    for alias, item in mounts.items():
        if not isinstance(alias, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", alias
        ):
            raise ValueError("Invalid live library alias")
        path = Path(item)
        if (
            not path.is_absolute()
            or not path.is_dir()
            or not any(path.resolve().is_relative_to(root) for root in roots)
        ):
            raise ValueError("Pi library mount must be within an authorized read root")
    return roots, writable, mounts


def _pi_install(executable):
    executable = Path(shutil.which(executable) or executable).resolve()
    if not executable.is_file():
        raise HarnessError("Pi executable is unavailable")
    # The managed launcher is a shell script; resolve the actual immutable npm
    # installation without granting access to its neighboring auth/config files.
    if (
        executable.name == "pi"
        and (executable.parent.parent / "install/current-version").is_file()
    ):
        install = executable.parent.parent / "install"
        version = (install / "current-version").read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
            raise HarnessError("Invalid managed Pi version")
        executable = (install / "releases" / version / "node_modules/.bin/pi").resolve()
    modules = next(
        (parent for parent in executable.parents if parent.name == "node_modules"), None
    )
    if (
        modules is None
        or not (modules / "@earendil-works/pi-coding-agent/package.json").is_file()
    ):
        raise HarnessError(
            "Isolation requires an installed Pi npm entry point or managed launcher"
        )
    node = shutil.which("node")
    if node is None:
        raise HarnessError("Pi isolation requires Node.js on PATH")
    return [str(Path(node).resolve()), str(executable)], modules.resolve()


def _runtime_files(command, modules):
    """Resolve dynamic libraries by loader metadata, without scanning contents."""
    roots = []
    pending_packages = [modules / "@earendil-works/pi-coding-agent"]
    seen = set()
    while pending_packages:
        package = pending_packages.pop().resolve()
        if not package.is_relative_to(modules):
            raise HarnessError(
                "Pi dependencies must remain inside the installed npm tree"
            )
        if package in seen:
            continue
        seen.add(package)
        roots.append(package)
        metadata = json.loads((package / "package.json").read_text())
        dependencies = set(metadata.get("dependencies", {})) | set(
            metadata.get("optionalDependencies", {})
        )
        for name in dependencies:
            if not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", name):
                raise HarnessError("Invalid Pi runtime dependency name")
            for parent in [package, *package.parents]:
                candidate = parent / "node_modules" / name
                if (candidate / "package.json").is_file():
                    pending_packages.append(candidate)
                    break
            else:
                if name not in metadata.get("optionalDependencies", {}):
                    raise HarnessError("Pi runtime dependency is not installed")
    files = {Path(command[0]).resolve()}
    for name in ("rg", "fd"):
        path = native_binary(name)
        if path:
            files.add(Path(path).resolve())
    if sys.platform == "darwin":
        pending = list(files)
        while pending:
            path = pending.pop()
            listing = subprocess.run(
                ["/usr/bin/otool", "-L", str(path)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            dependencies = re.findall(r"^\s+(/\S+) \(", listing, re.MULTILINE)
            # Homebrew's Node dylib uses an rpath next to the executable.
            dependencies += [
                str(path.parent.parent / "lib" / name)
                for name in re.findall(r"^\s+@rpath/(\S+) \(", listing, re.MULTILINE)
            ]
            dependencies += [
                str(path.parent / name)
                for name in re.findall(
                    r"^\s+@loader_path/(\S+) \(", listing, re.MULTILINE
                )
            ]
            for dependency in dependencies:
                target = Path(dependency).resolve()
                if (
                    str(target).startswith(("/usr/lib/", "/System/Library/"))
                    or target in files
                ):
                    continue
                if not target.is_file():
                    raise HarnessError("Cannot resolve Pi runtime dynamic library")
                files.add(target)
                pending.append(target)
    return roots, sorted(files)


def command(executable, workspace, control, state, bridge_socket, roots, writable):
    cli, modules = _pi_install(executable)
    runtime_roots, runtime_files = _runtime_files(cli, modules)
    read_roots = [*runtime_roots, *roots, workspace, control, state]
    write_roots = [state / "tmp"]
    if writable is not None:
        read_roots.append(writable)
        write_roots.append(writable)
    if sys.platform == "darwin":
        sandbox = shutil.which("sandbox-exec")
        if sandbox is None:
            raise HarnessError("macOS sandbox-exec is required; refusing unisolated Pi")
        # Directory traversal metadata is allowed; file contents and directory
        # listings are default-denied outside these exact runtime/capability roots.
        system = [
            "/usr/bin",
            "/usr/lib",
            "/usr/share",
            "/bin",
            "/sbin",
            "/System/Library",
            "/Library/Apple/System/Library",
        ]
        literals = [
            "/",
            "/dev/null",
            "/dev/random",
            "/dev/urandom",
            "/private/etc/hosts",
            "/private/etc/resolv.conf",
            "/private/etc/localtime",
            "/private/etc/services",
            "/private/etc/ssl/cert.pem",
            bridge_socket,
        ]
        exclusions = [
            f"(require-not (subpath {json.dumps(str(p))}))"
            for p in [*system, *read_roots]
        ]
        exclusions += [
            f"(require-not (literal {json.dumps(str(p))}))"
            for p in [*literals, *runtime_files]
        ]
        profile = (
            "(version 1)(allow default)(deny file-read-data (require-all "
            + " ".join(exclusions)
            + "))"
        )
        exclusions = [
            f"(require-not (subpath {json.dumps(str(p))}))" for p in write_roots
        ]
        # Pi creates these empty synchronization directories during bootstrap.
        # The host seals the state directory before the first provider request.
        exclusions += [
            f"(require-not (subpath {json.dumps(str(state / name))}))"
            for name in ("auth.json.lock", "trust.json.lock", "settings.json.lock")
        ]
        exclusions += [
            '(require-not (literal "/dev/null"))',
            f"(require-not (literal {json.dumps(bridge_socket)}))",
        ]
        profile += "(deny file-write* (require-all " + " ".join(exclusions) + "))"
        # Prevent accessing unrelated credentials through Keychain services.
        profile += '(deny mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.securityd.xpc"))'
        return [sandbox, "-p", profile, *cli]
    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            raise HarnessError("Linux bubblewrap is required; refusing unisolated Pi")
        result = [
            bwrap,
            "--die-with-parent",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--cap-drop",
            "ALL",
            "--tmpfs",
            "/",
            "--dev",
            "/dev",
        ]
        # No /proc: native reads must not reach host process environments/fds.
        system = [
            Path(p)
            for p in (
                "/usr/bin",
                "/usr/lib",
                "/usr/lib64",
                "/usr/share",
                "/lib",
                "/lib64",
                "/bin",
                "/sbin",
            )
            if Path(p).exists()
        ]
        for path in [*system, *read_roots, *runtime_files]:
            result.extend(["--ro-bind", str(path), str(path)])
        for path in write_roots:
            result.extend(["--bind", str(path), str(path)])
        # Bubblewrap cannot grant mkdir for two names within an otherwise
        # read-only directory. Bootstrap gets this isolated directory; the host
        # removes write permission before admitting the first model request.
        # Existing configuration files remain read-only even during bootstrap.
        result.extend(["--bind", str(state), str(state)])
        for name in ("settings.json", "models.json", "auth.json", "trust.json"):
            path = state / name
            if path.exists():
                result.extend(["--ro-bind", str(path), str(path)])
        result.extend(
            [
                "--ro-bind",
                str(Path(bridge_socket).parent),
                str(Path(bridge_socket).parent),
            ]
        )
        for path in (
            "/etc/hosts",
            "/etc/resolv.conf",
            "/etc/nsswitch.conf",
            "/etc/ssl/certs",
        ):
            if Path(path).exists():
                result.extend(["--ro-bind", path, path])
        return [*result, "--chdir", str(workspace), "--", *cli]
    raise HarnessError("No supported host file isolation; refusing unisolated Pi")


def prepare_agent(directory, provider):
    """Copy selected non-secret configuration; put credential values in env only.

    Native read can see every file readable by this process. Never copy auth.json
    or literal API keys into its filesystem. OAuth refresh requires a separate
    credential broker and is intentionally rejected rather than weakening this.
    """
    source = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
    directory.mkdir()
    environment = os.environ.copy()
    environment["PI_CODING_AGENT_DIR"] = str(directory)
    environment["HOME"] = str(directory)
    environment["TMPDIR"] = str(directory / "tmp")
    (directory / "tmp").mkdir()
    # These contain no host credentials or trust decisions. Creating them here
    # avoids granting Pi file creation in the profile root during bootstrap.
    (directory / "auth.json").write_text("{}")
    (directory / "trust.json").write_text("{}")
    environment["JITI_CACHE_DIR"] = str(directory / "tmp/jiti")
    environment["XDG_CACHE_HOME"] = str(directory / "tmp/cache")
    environment["OPENSSL_CONF"] = "/dev/null"
    environment["PI_OFFLINE"] = "1"
    environment.pop("PI_PACKAGE_DIR", None)
    models_path = source / "models.json"
    models = json.loads(models_path.read_text()) if models_path.is_file() else {}
    selected = models.get("providers", {}).get(provider)
    if selected is not None and selected.get("oauth") is not None:
        raise HarnessError("Isolated Pi does not support file-backed OAuth profiles")
    auth_path = source / "auth.json"
    auth = json.loads(auth_path.read_text()) if auth_path.is_file() else {}
    credential = auth.get(provider)
    if credential and credential.get("type") != "api_key":
        raise HarnessError(
            "Isolated Pi requires an environment/API-key profile; OAuth credential files require a broker"
        )
    serial = 0

    def protect(value, credential_environment=None):
        nonlocal serial
        if not isinstance(value, str):
            raise HarnessError("Pi credential values must be strings")
        if value.startswith("!"):
            raise HarnessError("Isolated Pi does not execute credential commands")
        serial += 1
        name = f"RL_PI_SECRET_{serial}"

        # Preserve Pi's explicit $VAR interpolation, but no command execution.
        def replace(match):
            token = match.group(0)
            if token == "$$":
                return "$"
            if token == "$!":
                return "!"
            variable = match.group(1) or match.group(2)
            resolved = (credential_environment or {}).get(
                variable, os.environ.get(variable)
            )
            if not isinstance(resolved, str) or not resolved:
                raise HarnessError("Pi credential environment variable is missing")
            return resolved

        environment[name] = re.sub(
            r"\$\$|\$!|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
            replace,
            value,
        )
        return "$" + name

    if selected is not None:
        if "apiKey" in selected:
            selected["apiKey"] = protect(selected["apiKey"])
        if "headers" in selected:
            selected["headers"] = {
                key: protect(value) for key, value in selected["headers"].items()
            }
        for model in [
            *selected.get("models", []),
            *selected.get("modelOverrides", {}).values(),
        ]:
            if "headers" in model:
                model["headers"] = {
                    key: protect(value) for key, value in model["headers"].items()
                }
    if credential:
        selected = selected or {}
        selected["apiKey"] = protect(credential["key"], credential.get("env"))
    if selected is not None:
        # Use Pi's own defaults, model overrides and protocol composition.
        (directory / "models.json").write_text(
            json.dumps({"providers": {provider: selected}})
        )
    (directory / "settings.json").write_text(
        json.dumps(
            {
                "retry": {"enabled": False, "provider": {"maxRetries": 0}},
                "compaction": {"enabled": False},
                "transport": "sse",
            }
        )
    )
    # Establish immutable file revisions before Pi caches auth/settings. A
    # post-start chmod would change ctime and force a lock-taking auth reload.
    for path in directory.iterdir():
        if path.is_file():
            path.chmod(0o400)
    return environment
