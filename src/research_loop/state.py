"""Small filesystem records, a single writer and whole-tree publication."""

import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def now():
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@contextmanager
def writer(root: Path):
    with (root / "writer.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This run already has a controller writer") from None
        yield


class Journal:
    def __init__(self, path: Path):
        self.stream = path.open("a", encoding="utf-8")
        path.chmod(0o600)

    def emit(self, event, **data):
        self.stream.write(json.dumps({"at": now(), "event": event, **data}) + "\n")
        self.stream.flush()

    def close(self):
        self.stream.close()


class PublicationStopped(RuntimeError):
    """STOP was observed before the staged knowledge pointer was activated."""


class Knowledge:
    def __init__(self, root: Path):
        self.root = root / "knowledge"
        self.current = self.root / "current"
        self.draft = self.root / "draft"

    def prepare(self):
        versions = self.root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        if not self.current.exists():
            initial = versions / "initial"
            initial.mkdir(exist_ok=False)
            (initial / "index.md").write_text(
                "# Research knowledge\n\nNo reviewed findings have been published.\n"
            )
            self.current.symlink_to(
                Path("versions") / "initial", target_is_directory=True
            )
        if not self.draft.exists():
            shutil.copytree(self.current.resolve(), self.draft)
        return str(self.current.resolve())

    def published_state(self):
        if not self.current.exists():
            return None
        version = self.current.resolve().name
        path = self.root / "publications" / (version + ".json")
        if not path.exists():
            return None
        state = json.loads(path.read_text())
        if state.get("knowledge_version") != version:
            raise ValueError("Publication metadata does not match active knowledge")
        return state

    def publish(self, baseline: str, publication=None):
        with (self.root / "publish.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            if str(self.current.resolve()) != baseline:
                raise RuntimeError("Published knowledge changed since this draft began")
            if self.draft.is_symlink() or not self.draft.is_dir():
                raise ValueError("Knowledge draft must be a real directory")
            # Follow no links or special files into canonical data or secrets.
            for parent, dirs, files in os.walk(self.draft, followlinks=False):
                for name in dirs + files:
                    path = Path(parent) / name
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        raise ValueError(
                            "Knowledge draft must contain regular files/directories"
                        )
            version = uuid4().hex
            destination = self.root / "versions" / version
            pointer = self.root / (".current-" + version)
            metadata = self.root / "publications" / (version + ".json")
            try:
                # New inodes prevent an old draft FD from changing the published tree.
                shutil.copytree(self.draft, destination)
                if publication is not None:
                    metadata.parent.mkdir(exist_ok=True)
                    write_json(metadata, {**publication, "knowledge_version": version})
                pointer.symlink_to(Path("versions") / version, target_is_directory=True)
                if (self.root.parent / "STOP").exists():
                    raise PublicationStopped(
                        "STOP recorded before knowledge activation"
                    )
                pointer.replace(self.current)
            except BaseException:
                pointer.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                shutil.rmtree(destination, ignore_errors=True)
                raise
            return version
