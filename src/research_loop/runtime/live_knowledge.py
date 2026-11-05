"""One whole-library native directory; private SQLite retains immutable history."""

from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .atomic_directory import exchange
from .files import _sync_directory
from .knowledge import KnowledgeBase, LivePublicationError, _json, _markdown
from .role_access import RoleAccess
from .source_scope import directory_source_scope


def _schema(library):
    library._ensure()
    with library._connection(library.root, readonly=True) as conn:
        if (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('kb_mount_bindings','kb_live_state')"
            ).fetchone()[0]
            == 2
        ):
            return
    with library._connection(library.root) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kb_mount_bindings(alias TEXT NOT NULL,path TEXT NOT NULL,version INTEGER NOT NULL,library_id TEXT NOT NULL,PRIMARY KEY(alias,path,version))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kb_live_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),dirty INTEGER NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO kb_live_state VALUES(1,1)")


def _directory(path):
    if any(parent.is_symlink() for parent in [path, *path.parents]):
        raise ValueError("Published directories must not traverse symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


@contextmanager
def _publication_lock(library):
    _directory(library.folder)
    handle = os.open(
        library.folder / ".publication.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _bind(library, bindings):
    for mount in library._mounts:
        if mount["alias"] not in {item["alias"] for item in bindings}:
            continue
        values = (mount["alias"], mount["binding_path"], mount["version"])
        with library._connection(library.root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT library_id FROM kb_mount_bindings WHERE alias=? AND path=? AND version=?",
                values,
            ).fetchone()
            if prior is not None and prior["library_id"] != mount["library_id"]:
                raise PermissionError("Mounted knowledge library identity changed")
            if library._library(mount["path"])["id"] != mount["library_id"]:
                raise PermissionError("Mounted knowledge library identity changed")
            conn.execute(
                "INSERT OR IGNORE INTO kb_mount_bindings VALUES(?,?,?,?)",
                (*values, mount["library_id"]),
            )
            conn.commit()


def _view(library, item):
    if item["alias"] == "study":
        return library.folder / "published"
    return (
        library.folder
        / "mounted"
        / f"{item['library_id']}-{item['version']}"
        / "published"
    )


def _read_authority(root):
    return json.loads((root / "current" / "authority.json").read_text())


def _authorize(library, dependencies, role, aliases, bindings, *, shared=False):
    from .handoff_scope import authorize_publication

    if shared:
        from .role_access import directory_grants

        aliases = directory_grants(library._config)[role]
        bindings = library.directory_bindings(aliases)
        library = KnowledgeBase(library.root, config=library._config)

    normalized = []
    for reference in dependencies:
        item = next(
            (
                item
                for item in bindings
                if item["library_id"] == reference["library_id"]
            ),
            None,
        )
        if (
            reference["kind"] == "library"
            and reference["revision"] == 0
            and item is not None
            and item["alias"] != "study"
        ):
            reference = dict(reference, revision=item["version"])
        normalized.append(reference)
    authorize_publication(library, normalized, {role: aliases})


def open_live(library, aliases, *, role):
    allowed = RoleAccess.resolve(library._config, role).rule.get("knowledge_bases")
    if allowed is not None and not set(aliases) <= set(allowed):
        raise PermissionError("Knowledge aliases exceed the role's access")
    _schema(library)
    bindings = library.directory_bindings(aliases)
    _bind(library, bindings)
    metadata = library.directory_bindings(list(dict.fromkeys(["study", *aliases])))
    grants = directory_source_scope(library, library._config, role, metadata)
    entries, references = [], []
    with _publication_lock(library):
        for item in bindings:
            root = _view(library, item)
            with library._connection(library.root, readonly=True) as conn:
                dirty = conn.execute("SELECT dirty FROM kb_live_state").fetchone()[0]
            if not (root / "current").is_dir() or item["alias"] == "study" and dirty:
                try:
                    _publish(
                        library,
                        item,
                        root,
                        role=role,
                        aliases=aliases,
                        bindings=metadata,
                    )
                except OSError as error:
                    if (
                        isinstance(error, PermissionError)
                        or not (root / "current").is_dir()
                    ):
                        raise
                    # Keep the previous complete tree available during an
                    # activation failure; its authority is still checked below.
            _authorize(
                library,
                _read_authority(root),
                role,
                aliases,
                metadata,
                shared=item["alias"] == "study",
            )
            entries.append(
                dict(
                    alias=item["alias"],
                    library_id=item["library_id"],
                    path=str(root / "current"),
                    isolation_root=str(root),
                    description=item["description"],
                )
            )
            references.append(
                dict(
                    library_id=item["library_id"],
                    kind="library",
                    id="library",
                    revision=0 if item["alias"] == "study" else item["version"],
                )
            )
    return dict(entries=entries, references=references, source_scope=grants)


def _snapshot(library, item):
    with library._connection(item["root"], readonly=True) as conn:
        conn.execute("BEGIN")
        library._check_library_identity(conn, item["library_id"])
        version = (
            conn.execute("SELECT version FROM kb_library").fetchone()[0]
            if item["version"] is None
            else item["version"]
        )
        row = conn.execute(
            "SELECT * FROM kb_snapshots WHERE version=?", (version,)
        ).fetchone()
    if row is None:
        raise PermissionError("Unknown published library version")
    return dict(row), dict(
        item,
        version=version,
        evidence_through=row["evidence_through"],
        legacy_through=row["legacy_through"],
    )


def _copy_file(source, target):
    _directory(target.parent)
    if source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o222:
        raise ValueError("Published source must be an immutable regular file")
    try:
        os.link(source, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        shutil.copyfile(source, target)
        os.chmod(target, 0o444)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())


def _publish(library, item, root, *, role=None, aliases=None, bindings=None):
    snapshot, frozen = _snapshot(library, item)
    catalog = library.file_catalog(frozen, snapshot)
    manifest = [frozen]
    if snapshot.get("files_json") is None:
        authority = library.disclosure(manifest, list(catalog.values()))
    else:
        authority = json.loads(snapshot["files_json"])["disclosure"]
    if role is not None:
        _authorize(
            library, authority, role, aliases, bindings, shared=item["alias"] == "study"
        )
    current = root / "current"
    if current.is_symlink():
        raise ValueError("Published current must be a real directory")
    if (
        current.is_dir()
        and json.loads((current / ".version").read_text()) == frozen["version"]
    ):
        _sync_directory(root)
        if item["alias"] == "study":
            _clear_dirty(library, frozen["version"])
        return dict(state="ready", activated=False)
    staging = library.folder / ".publication-stage" / uuid.uuid4().hex
    _directory(staging / "files")
    activated = False
    try:
        scoped = library._manifest(manifest)
        citations = {}
        for path, reference in catalog.items():
            row, _ = library._record(scoped, reference)
            source = scoped[reference["library_id"]]["root"] / "knowledge" / row["path"]
            _copy_file(source, staging / "files" / path)
            from .model_requests import format_decision_reference

            citations[path] = format_decision_reference(reference)
        _markdown(staging / "revisions.json", _json(citations))
        _markdown(staging / "authority.json", _json(authority))
        _markdown(staging / ".version", _json(frozen["version"]))
        for folder, _, _ in os.walk(staging, topdown=False):
            _sync_directory(Path(folder))
        _directory(root)
        ready = staging.stat().st_ino
        recheck = False
        try:
            if current.exists():
                exchange(staging, current)
            else:
                os.rename(staging, current)
            activated = True
        except OSError:
            activated = current.is_dir() and current.stat().st_ino == ready
            if not activated:
                raise
            recheck = True
        try:
            _sync_directory(root)
            _sync_directory(staging.parent)
            shutil.rmtree(staging) if staging.exists() else None
        except OSError:
            recheck = True
        if not recheck and item["alias"] == "study":
            _clear_dirty(library, frozen["version"])
        return dict(state="ready", activated=True, recheck=recheck)
    finally:
        if not activated:
            shutil.rmtree(staging, ignore_errors=True)


def _clear_dirty(library, version):
    with library._connection(library.root, readonly=True) as conn:
        row = conn.execute("SELECT dirty FROM kb_live_state").fetchone()
    if not row or not row[0]:
        return
    with library._connection(library.root) as conn:
        conn.execute("PRAGMA busy_timeout=0")
        try:
            conn.execute(
                "UPDATE kb_live_state SET dirty=0 WHERE EXISTS(SELECT 1 FROM kb_library WHERE version=?)",
                (version,),
            )
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise


def refresh_live(library):
    _schema(library)
    root = library.folder / "published"
    if not root.exists():
        return {"state": "ready"}
    try:
        with _publication_lock(library):
            item = library.directory_bindings(["study"])[0]
            return _publish(library, item, root)
    except (OSError, ValueError, sqlite3.Error) as error:
        raise LivePublicationError(
            "Knowledge SQL is committed; native publication needs replay"
        ) from error


def live_status(library):
    if not library.path.exists():
        return dict(
            sql_version=None,
            live_version=None,
            publication_pending=False,
        )
    with library._connection(library.root, readonly=True) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='kb_library'"
            ).fetchone()
            is None
        ):
            return dict(
                sql_version=None,
                live_version=None,
                publication_pending=False,
            )
        head = conn.execute("SELECT version FROM kb_library").fetchone()[0]
    active = library.published_version()
    return dict(
        sql_version=head,
        live_version=active,
        publication_pending=active is not None and active != head,
    )
