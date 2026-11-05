"""Study-owned immutable evidence and versioned Markdown knowledge snapshots.

SQLite publishes the snapshot pointer, dependency graph and processing cursor in
one transaction. Markdown files are published first and are the authoritative
page bodies. Mounted libraries are opened read-only and never initialized here.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .files import _sync_directory
from .store import ConflictError, DATABASE

_DIRECTORY = "knowledge"
_DATABASE = "knowledge.sqlite3"


def _database(root):
    runtime = Path(root) / DATABASE
    return runtime if runtime.exists() else Path(root) / _DIRECTORY / _DATABASE


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_KINDS = {"evidence", "knowledge", "historical", "index", "source"}
_SCHEMA = """
CREATE TABLE kb_library(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 id TEXT NOT NULL, version INTEGER NOT NULL, processed INTEGER NOT NULL);
CREATE TABLE kb_evidence(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 source_type TEXT NOT NULL, body_json TEXT NOT NULL, refs_json TEXT NOT NULL,
 metadata_json TEXT NOT NULL);
CREATE TABLE kb_snapshots(version INTEGER PRIMARY KEY, pages_json TEXT NOT NULL,
 index_path TEXT NOT NULL, dependencies_json TEXT NOT NULL,
 evidence_through INTEGER NOT NULL,legacy_through INTEGER NOT NULL);
CREATE TABLE kb_revisions(id TEXT NOT NULL, revision INTEGER NOT NULL, version INTEGER NOT NULL,
 title TEXT NOT NULL, path TEXT NOT NULL, dependencies_json TEXT NOT NULL,
 archived INTEGER NOT NULL, PRIMARY KEY(id,revision));
CREATE TABLE kb_commits(request_id TEXT PRIMARY KEY, input_json TEXT NOT NULL,
 receipt_json TEXT NOT NULL);
CREATE TABLE kb_maintenance_acks(signature TEXT PRIMARY KEY);
CREATE TABLE kb_dependencies(page_id TEXT NOT NULL,page_revision INTEGER NOT NULL,
 library_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE INDEX kb_dependency_target ON kb_dependencies(library_id,kind,id,revision);
CREATE INDEX kb_dependency_page ON kb_dependencies(page_id,page_revision);

CREATE VIRTUAL TABLE kb_search USING fts5(title,body,kind UNINDEXED,id UNINDEXED,revision UNINDEXED);
CREATE TABLE kb_historical(id TEXT NOT NULL,revision INTEGER NOT NULL,
 publication_id INTEGER NOT NULL,path TEXT NOT NULL,metadata_json TEXT NOT NULL,
 PRIMARY KEY(id,revision));
CREATE INDEX kb_legacy_publication ON kb_historical(publication_id);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Knowledge IDs must be simple nonempty names")
    return value


def _reference(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"library_id", "kind", "id", "revision"}
        or not isinstance(value["library_id"], str)
        or not value["library_id"]
        or value["kind"] not in _KINDS
        or not isinstance(value["id"], str)
        or not value["id"]
        or (
            (type(value["revision"]) is not int or value["revision"] < 1)
            if value["kind"] != "source"
            else not (
                (type(value["revision"]) is int and value["revision"] >= 0)
                or (
                    isinstance(value["revision"], str)
                    and bool(value["revision"].strip())
                )
            )
        )
        or (
            value["kind"] == "source" and not re.fullmatch(r"[0-9a-f]{32}", value["id"])
        )
        or (value["kind"] == "index" and value["id"] != "index.md")
        or (value["kind"] == "evidence" and value["revision"] != 1)
    ):
        raise ValueError("Expected a fixed typed knowledge reference")
    return deepcopy(value)


def _references(values):
    if not isinstance(values, list):
        raise ValueError("dependencies must be a list of fixed references")
    refs = [_reference(value) for value in values]
    if len({_json(ref) for ref in refs}) != len(refs):
        raise ValueError("Duplicate dependency reference")
    return refs


def _markdown(path, body):
    if not isinstance(body, str):
        raise ValueError("Markdown must be text")
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Knowledge files must not traverse symlinks")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(body.encode("utf-8"))
        handle.flush()
        os.fchmod(handle.fileno(), 0o444)
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


class KnowledgeBase:
    """Resolve trusted library roots separately from model-visible manifests."""

    def __init__(self, study_root, config=None, *, source_scope=None):
        self.root = Path(study_root).resolve()
        self.folder = self.root / _DIRECTORY
        self.path = _database(self.root)
        self._roots = {}
        self._mounts = []
        self._config = {}
        self._source_scope = {_json(_reference(ref)) for ref in (source_scope or [])}
        if config is not None:
            self.configure(config)

    def configure(self, config):
        mounts = config.get("knowledge_libraries", [])
        if not isinstance(mounts, list):
            raise ValueError("knowledge_libraries must be a list")
        aliases = {"study"}
        checked = []
        for mount in mounts:
            if not isinstance(mount, dict) or set(mount) - {
                "alias",
                "path",
                "version",
                "description",
            }:
                raise ValueError("Invalid knowledge_libraries mount")
            alias = _identifier(mount.get("alias"))
            if alias in aliases:
                raise ValueError("Duplicate or reserved knowledge library alias")
            aliases.add(alias)
            version = mount.get("version")
            if type(version) is not int or version < 0:
                raise ValueError("Mounted knowledge libraries require a fixed version")
            if not isinstance(mount.get("path"), str) or not mount["path"]:
                raise ValueError("Knowledge library mount requires a path")
            root = Path(mount["path"])
            if not root.is_absolute():
                root = self.root / root
            root = root.resolve()
            # A path denotes another Study, never an arbitrary database file.
            row = self._library(root)
            with self._connection(root, readonly=True) as conn:
                if (
                    conn.execute(
                        "SELECT 1 FROM kb_snapshots WHERE version=?", (version,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError(f"Unknown fixed knowledge version for {alias}")
            self._roots[row["id"]] = root
            checked.append(dict(mount, path=root, library_id=row["id"]))
        self._mounts = checked
        self._config = deepcopy(config)
        return self

    @staticmethod
    @contextmanager
    def _connection(root, *, readonly=False):
        path = _database(root)
        folder = path.parent
        if folder.is_symlink() or path.is_symlink():
            raise ValueError("Knowledge storage must not be a symlink")
        mode = "ro" if readonly else "rw"
        conn = sqlite3.connect(
            path.as_uri() + f"?mode={mode}", uri=True, timeout=10, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        try:
            if not readonly:
                conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    @classmethod
    def _library(cls, root):
        try:
            with cls._connection(root, readonly=True) as conn:
                row = conn.execute(
                    "SELECT * FROM kb_library WHERE singleton=1"
                ).fetchone()
                if row is None:
                    raise ValueError("Invalid knowledge library")
                return dict(row)
        except sqlite3.Error as exc:
            raise ValueError("Cannot read knowledge library") from exc

    def _ensure(self):
        if self.path.exists():
            with self._connection(self.root, readonly=True) as conn:
                initialized = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kb_library'"
                ).fetchone()
            if initialized:
                row = self._library(self.root)
                self._roots[row["id"]] = self.root
                return
        self.root.mkdir(parents=True, exist_ok=True)
        self.folder.mkdir(exist_ok=True)
        if self.folder.is_symlink():
            raise ValueError("Knowledge storage must not be a symlink")
        if self.path == self.root / DATABASE:
            # Existing Studies retain their runtime schema version. The library
            # is a cohesive extension sharing acceptance/publication transactions.
            with self._connection(self.root) as conn:
                conn.execute("BEGIN IMMEDIATE")
                initialized = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='kb_library'"
                ).fetchone()
                if not initialized:
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT INTO kb_library VALUES(1,?,0,0)", (uuid.uuid4().hex,)
                    )
                    conn.execute("INSERT INTO kb_snapshots VALUES(0,'{}','','[]',0,0)")
                conn.commit()
        else:
            # Atomic initialization prevents readers observing a partial schema.
            temporary = self.folder / f".initialize-{uuid.uuid4().hex}"
            conn = sqlite3.connect(temporary)
            try:
                conn.executescript(_SCHEMA)
                conn.execute(
                    "INSERT INTO kb_library VALUES(1,?,0,0)", (uuid.uuid4().hex,)
                )
                conn.execute("INSERT INTO kb_snapshots VALUES(0,'{}','','[]',0,0)")
                conn.commit()
            finally:
                conn.close()
            try:
                os.link(temporary, self.path)
                _sync_directory(self.folder)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
        row = self._library(self.root)
        self._roots[row["id"]] = self.root

    @property
    def library_id(self):
        self._ensure()
        return self._library(self.root)["id"]

    def publish_evidence(
        self, evidence_id, source_type, content, *, source_refs=None, metadata=None
    ):
        """Append one typed record; an exact retry returns its original identity."""
        _identifier(evidence_id)
        if not isinstance(source_type, str) or not source_type.strip():
            raise ValueError("Evidence requires an explicit source_type")
        refs = [] if source_refs is None else source_refs
        if not isinstance(refs, list):
            raise ValueError("source_refs must be a list")
        for ref in refs:
            if isinstance(ref, dict):
                _reference(ref)
            elif not isinstance(ref, str) or not ref:
                raise ValueError(
                    "Evidence source references must be text or typed references"
                )
        metadata = {} if metadata is None else metadata
        if not isinstance(metadata, dict):
            raise ValueError("Evidence metadata must be an object")
        values = (source_type, _json(content), _json(refs), _json(metadata))
        self._ensure()
        with self._connection(self.root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM kb_evidence WHERE id=?", (evidence_id,)
            ).fetchone()
            if prior is not None:
                if (
                    tuple(
                        prior[key]
                        for key in (
                            "source_type",
                            "body_json",
                            "refs_json",
                            "metadata_json",
                        )
                    )
                    != values
                ):
                    raise ConflictError(
                        "Evidence identity already has different immutable content"
                    )
            else:
                conn.execute(
                    "INSERT INTO kb_evidence(id,source_type,body_json,refs_json,metadata_json) VALUES(?,?,?,?,?)",
                    (evidence_id, *values),
                )
                conn.execute(
                    "INSERT INTO kb_search(title,body,kind,id,revision) VALUES(?,?,'evidence',?,1)",
                    (evidence_id, _json(content), evidence_id),
                )
            conn.commit()
        return {
            "library_id": self.library_id,
            "kind": "evidence",
            "id": evidence_id,
            "revision": 1,
        }

    def freeze(self, config=None, aliases=None):
        """Freeze authorized aliases without placing their filesystem paths in context."""
        if config is not None:
            self.configure(config)
        self._ensure()
        if aliases is None:
            aliases = ["study"]
        if not isinstance(aliases, (list, tuple)) or len(set(aliases)) != len(aliases):
            raise ValueError("Knowledge aliases must be a unique list")
        available = {mount["alias"]: mount for mount in self._mounts}
        available["study"] = {"path": self.root, "description": "Study knowledge"}
        result = []
        for alias in aliases:
            if alias not in available:
                raise ValueError(f"Unknown knowledge library alias: {alias}")
            mount = available[alias]
            root = mount["path"]
            with self._connection(root, readonly=True) as conn:
                conn.execute("BEGIN")
                row = dict(conn.execute("SELECT * FROM kb_library").fetchone())
                self._roots[row["id"]] = root
                version = row["version"] if alias == "study" else mount["version"]
                through = conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM kb_evidence"
                ).fetchone()[0]
                legacy = conn.execute(
                    "SELECT COALESCE(MAX(publication_id),0) FROM kb_historical"
                ).fetchone()[0]
                if alias != "study":
                    frozen = conn.execute(
                        "SELECT evidence_through,legacy_through FROM kb_snapshots WHERE version=?",
                        (version,),
                    ).fetchone()
                    through, legacy = (
                        frozen["evidence_through"],
                        frozen["legacy_through"],
                    )
            result.append(
                dict(
                    alias=alias,
                    library_id=row["id"],
                    version=version,
                    evidence_through=through,
                    legacy_through=legacy,
                    index="index.md",
                    description=mount.get("description", ""),
                )
            )
        return result

    def _manifest(self, manifest):
        if not isinstance(manifest, list):
            raise ValueError("Expected a frozen knowledge manifest")
        if self.path.exists():
            own = self._library(self.root)
            self._roots[own["id"]] = self.root
        result = {}
        aliases = set()
        for item in manifest:
            if not isinstance(item, dict):
                raise ValueError("Invalid knowledge manifest entry")
            library_id = item.get("library_id")
            if (
                library_id not in self._roots
                or library_id in result
                or item.get("alias") in aliases
            ):
                raise PermissionError("Unknown or duplicate knowledge library")
            if any(
                type(item.get(key)) is not int or item[key] < 0
                for key in ("version", "evidence_through", "legacy_through")
            ):
                raise ValueError("Invalid frozen knowledge boundary")
            result[library_id] = dict(item, root=self._roots[library_id])
            aliases.add(item.get("alias"))
        return result

    def _record(self, scoped, ref):
        """Return metadata plus body, requiring a fixed record within the snapshot."""
        ref = _reference(ref)
        item = scoped.get(ref["library_id"])
        if item is None:
            raise PermissionError("Reference library is outside the frozen scope")
        if ref["kind"] == "source":
            if _json(ref) not in self._source_scope:
                raise PermissionError(
                    "Source dependency is outside the role disclosure scope"
                )
            return {}, []
        with self._connection(item["root"], readonly=True) as conn:
            if ref["kind"] == "index":
                row = conn.execute(
                    "SELECT index_path AS path,dependencies_json FROM kb_snapshots WHERE version=? AND version<=?",
                    (ref["revision"], item["version"]),
                ).fetchone()
                if row is None or not row["path"]:
                    raise PermissionError(
                        "Index revision is outside the frozen snapshot"
                    )
                return dict(row), json.loads(row["dependencies_json"])
            if ref["kind"] == "evidence":
                row = conn.execute(
                    "SELECT * FROM kb_evidence WHERE id=? AND seq<=?",
                    (ref["id"], item["evidence_through"]),
                ).fetchone()
                if row is None:
                    raise PermissionError("Evidence is outside the frozen snapshot")
                return dict(row), [
                    r for r in json.loads(row["refs_json"]) if isinstance(r, dict)
                ]
            if ref["kind"] == "historical":
                row = conn.execute(
                    "SELECT * FROM kb_historical WHERE id=? AND revision=? AND publication_id<=?",
                    (ref["id"], ref["revision"], item["legacy_through"]),
                ).fetchone()
                if row is None:
                    raise PermissionError(
                        "Historical page is outside the frozen snapshot"
                    )
                return dict(row), []
            row = conn.execute(
                "SELECT * FROM kb_revisions WHERE id=? AND revision=? AND version<=?",
                (ref["id"], ref["revision"], item["version"]),
            ).fetchone()
            if row is None:
                raise PermissionError(
                    "Knowledge revision is outside the frozen snapshot"
                )
            return dict(row), json.loads(row["dependencies_json"])

    def _authorized(self, scoped, reference, seen=None):
        seen = set() if seen is None else seen
        key = _json(reference)
        if key in seen:
            return
        seen.add(key)
        _, dependencies = self._record(scoped, reference)
        for ref in dependencies:
            self._authorized(scoped, ref, seen)

    def _snapshot(self, item):
        with self._connection(item["root"], readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM kb_snapshots WHERE version=?", (item["version"],)
            ).fetchone()
            if row is None:
                raise PermissionError("Knowledge snapshot is unavailable")
            return dict(row)

    @staticmethod
    def _body(item, path):
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid stored Markdown path")
        root = item["root"] / _DIRECTORY
        target = root / relative
        for parent in [target, *target.parents]:
            if parent == root.parent:
                break
            if parent.is_symlink():
                raise ValueError("Knowledge Markdown must not traverse symlinks")
        if not target.is_file() or target.stat().st_mode & 0o222:
            raise ValueError("Knowledge Markdown must remain an immutable regular file")
        return target.read_text(encoding="utf-8")

    def _visible_pages(self, scoped, item):
        snapshot = self._snapshot(item)
        pages = []
        for page_id, revision in json.loads(snapshot["pages_json"]).items():
            ref = dict(
                library_id=item["library_id"],
                kind="knowledge",
                id=page_id,
                revision=revision,
            )
            try:
                self._authorized(scoped, ref)
            except PermissionError:
                continue
            row, _ = self._record(scoped, ref)
            if not row["archived"]:
                pages.append((ref, row))
        return snapshot, pages

    def read_index(self, manifest, alias="study"):
        return self.index_with_sources(manifest, alias)["content"]

    def index_with_sources(self, manifest, alias="study"):
        """Return the rendered Index and the fixed sources that contributed text."""
        scoped = self._manifest(manifest)
        item = next((item for item in scoped.values() if item["alias"] == alias), None)
        if item is None:
            raise PermissionError("Knowledge alias is outside the frozen scope")
        snapshot, pages = self._visible_pages(scoped, item)
        complete = True
        for ref in json.loads(snapshot["dependencies_json"]):
            try:
                self._authorized(scoped, ref)
            except PermissionError:
                complete = False
                break
        if complete and snapshot["index_path"]:
            text = self._body(item, snapshot["index_path"])
            dependencies = [
                dict(
                    library_id=item["library_id"],
                    kind="index",
                    id="index.md",
                    revision=item["version"],
                )
            ]
        else:
            dependencies = [ref for ref, _ in pages]
            lines = ["# Knowledge index", ""]
            lines.extend(
                f"- [{row['title']}]({ref['id']}@{ref['revision']})"
                for ref, row in pages
            )
            text = "\n".join(lines) + "\n"
        with self._connection(item["root"], readonly=True) as conn:
            historical = list(
                conn.execute(
                    "SELECT id,MAX(revision) AS revision FROM kb_historical WHERE publication_id<=? GROUP BY id ORDER BY id",
                    (item["legacy_through"],),
                )
            )
        if historical:
            text += "\n## Historical accepted entries (read-only)\n\n"
            text += (
                "\n".join(
                    f"- {row['id']} (historical revision {row['revision']})"
                    for row in historical
                )
                + "\n"
            )
            dependencies.extend(
                dict(
                    library_id=item["library_id"],
                    kind="historical",
                    id=row["id"],
                    revision=row["revision"],
                )
                for row in historical
            )
        return dict(content=text, dependencies=dependencies)

    def resolve_reference(self, manifest, alias, path):
        scoped = self._manifest(manifest)
        item = next((item for item in scoped.values() if item["alias"] == alias), None)
        if item is None:
            raise PermissionError("Knowledge alias is outside the frozen scope")
        if isinstance(path, dict):
            ref = _reference(path)
            if ref["library_id"] != item["library_id"]:
                raise PermissionError(
                    "Reference does not belong to the requested alias"
                )
        elif path == "index.md":
            raise ValueError(
                "Index uses a snapshot boundary, not an evidence reference"
            )
        else:
            _identifier(path)
            snapshot = self._snapshot(item)
            pages = json.loads(snapshot["pages_json"])
            if path in pages:
                ref = dict(
                    library_id=item["library_id"],
                    kind="knowledge",
                    id=path,
                    revision=pages[path],
                )
            else:
                ref = dict(
                    library_id=item["library_id"], kind="evidence", id=path, revision=1
                )
        self._authorized(scoped, ref)
        return ref

    def read(self, manifest, alias, path):
        return self.read_with_sources(manifest, alias, path)["content"]

    def read_with_sources(self, manifest, alias, path):
        """Read one frozen record and its traversable citations after authorization."""
        if path == "index.md":
            return self.index_with_sources(manifest, alias)
        scoped = self._manifest(manifest)
        ref = self.resolve_reference(manifest, alias, path)
        row, dependencies = self._record(scoped, ref)
        if ref["kind"] == "source":
            raise PermissionError("Source dependencies do not grant artifact reads")
        if ref["kind"] == "evidence":
            content = dict(
                reference=ref,
                source_type=row["source_type"],
                content=json.loads(row["body_json"]),
                source_refs=json.loads(row["refs_json"]),
                metadata=json.loads(row["metadata_json"]),
            )
        else:
            content = self._body(scoped[ref["library_id"]], row["path"])
        return {"content": content, "fixed_ref": ref, "dependencies": dependencies}

    def search(self, manifest, query, limit=10):
        """Query the rebuildable FTS index, authorizing before returning snippets."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Search query must be nonempty text")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        scoped = self._manifest(manifest)
        words = re.findall(r"\w+", query, flags=re.UNICODE)
        if not words:
            return []
        expression = " AND ".join('"' + word.replace('"', '""') + '"' for word in words)
        found = []
        for item in scoped.values():
            page_map = json.loads(self._snapshot(item)["pages_json"])
            with self._connection(item["root"], readonly=True) as conn:
                rows = conn.execute(
                    "SELECT title,kind,id,revision,snippet(kb_search,1,'','',' … ',36) AS excerpt FROM kb_search WHERE kb_search MATCH ? ORDER BY bm25(kb_search)",
                    (expression,),
                )
                for row in rows:
                    ref = dict(
                        library_id=item["library_id"],
                        kind=row["kind"],
                        id=row["id"],
                        revision=int(row["revision"]),
                    )
                    if (
                        ref["kind"] == "knowledge"
                        and page_map.get(ref["id"]) != ref["revision"]
                    ):
                        continue
                    try:
                        self._authorized(scoped, ref)
                    except PermissionError:
                        continue
                    record, _ = self._record(scoped, ref)
                    if ref["kind"] == "knowledge" and record["archived"]:
                        continue
                    found.append(
                        dict(
                            alias=item["alias"],
                            title=row["title"],
                            snippet=row["excerpt"],
                            reference=ref,
                        )
                    )
                    if len(found) == limit:
                        return found
        return found

    def rebuild_search_index(self):
        """Explicitly rebuild derivative FTS data; never runs during startup/read."""
        self._ensure()
        item = dict(root=self.root)
        with self._connection(self.root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM kb_search")
            for row in conn.execute("SELECT * FROM kb_revisions"):
                conn.execute(
                    "INSERT INTO kb_search(title,body,kind,id,revision) VALUES(?,?,'knowledge',?,?)",
                    (
                        row["title"],
                        self._body(item, row["path"]),
                        row["id"],
                        row["revision"],
                    ),
                )
            for row in conn.execute("SELECT * FROM kb_historical"):
                conn.execute(
                    "INSERT INTO kb_search(title,body,kind,id,revision) VALUES(?,?,'historical',?,?)",
                    (
                        row["id"],
                        self._body(item, row["path"]),
                        row["id"],
                        row["revision"],
                    ),
                )
            for row in conn.execute("SELECT * FROM kb_evidence"):
                conn.execute(
                    "INSERT INTO kb_search(title,body,kind,id,revision) VALUES(?,?,'evidence',?,1)",
                    (row["id"], row["body_json"], row["id"]),
                )
            conn.commit()

    def _affected(self, manifest, conn=None):
        scoped = self._manifest(manifest)
        main_id = self.library_id
        if main_id not in scoped:
            return []
        own = scoped[main_id]
        current = json.loads(self._snapshot(own)["pages_json"])
        result = []
        with (
            nullcontext(conn)
            if conn is not None
            else self._connection(self.root, readonly=True)
        ) as reader:
            acknowledged = {
                row[0]
                for row in reader.execute("SELECT signature FROM kb_maintenance_acks")
            }
        maps = {
            key: json.loads(self._snapshot(value)["pages_json"])
            for key, value in scoped.items()
        }
        for page_id, revision in current.items():
            ref = dict(
                library_id=main_id, kind="knowledge", id=page_id, revision=revision
            )
            try:
                self._authorized(scoped, ref)
            except PermissionError:
                continue
            row, deps = self._record(scoped, ref)
            if row["archived"]:
                continue
            for dep in deps:
                latest = (
                    maps.get(dep["library_id"], {}).get(dep["id"], 0)
                    if dep["kind"] == "knowledge"
                    else 0
                )
                if latest > dep["revision"]:
                    signature = _json([ref, dep, latest])
                    if signature not in acknowledged:
                        result.append(
                            dict(
                                reference=ref,
                                dependency=dep,
                                current_revision=latest,
                                signature=signature,
                            )
                        )
        return result

    def status(self):
        """Read only constant-size controller metadata and indexed high-water marks."""
        self._ensure()
        with self._connection(self.root, readonly=True) as conn:
            conn.execute("BEGIN")
            row = dict(conn.execute("SELECT * FROM kb_library").fetchone())
            evidence = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM kb_evidence"
            ).fetchone()[0]
            legacy = conn.execute(
                "SELECT COALESCE(MAX(publication_id),0) FROM kb_historical"
            ).fetchone()[0]
            stale = conn.execute(
                "SELECT d.page_id,d.page_revision,d.library_id,d.kind,d.id,d.revision,MAX(target.revision) AS latest FROM kb_dependencies d JOIN kb_revisions target ON target.id=d.id AND target.revision>d.revision JOIN kb_revisions source ON source.id=d.page_id AND source.revision=d.page_revision WHERE d.library_id=? AND d.kind='knowledge' AND source.archived=0 AND d.page_revision=(SELECT MAX(r.revision) FROM kb_revisions r WHERE r.id=d.page_id) GROUP BY d.page_id,d.page_revision,d.library_id,d.kind,d.id,d.revision",
                (row["id"],),
            )
            needs_review = False
            for value in stale:
                ref = dict(
                    library_id=row["id"],
                    kind="knowledge",
                    id=value["page_id"],
                    revision=value["page_revision"],
                )
                dep = dict(
                    library_id=value["library_id"],
                    kind=value["kind"],
                    id=value["id"],
                    revision=value["revision"],
                )
                signature = _json([ref, dep, value["latest"]])
                if (
                    conn.execute(
                        "SELECT 1 FROM kb_maintenance_acks WHERE signature=?",
                        (signature,),
                    ).fetchone()
                    is None
                ):
                    needs_review = True
                    break
        return dict(
            library_id=row["id"],
            version=row["version"],
            processed_through=row["processed"],
            evidence_through=evidence,
            legacy_through=legacy,
            needs_review=needs_review,
        )

    def pending(self, *, manifest=None):
        self._ensure()
        with self._connection(self.root, readonly=True) as conn:
            conn.execute("BEGIN")
            row = dict(conn.execute("SELECT * FROM kb_library").fetchone())
            through = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM kb_evidence"
            ).fetchone()[0]
            if manifest is not None:
                scoped = self._manifest(manifest)
                item = scoped.get(row["id"])
                if item is None:
                    raise PermissionError("Maintenance requires the main library")
                through = min(through, item["evidence_through"])
            records = [
                dict(
                    library_id=row["id"],
                    kind="evidence",
                    id=value["id"],
                    revision=1,
                    sequence=value["seq"],
                    source_type=value["source_type"],
                )
                for value in conn.execute(
                    "SELECT seq,id,source_type FROM kb_evidence WHERE seq>? AND seq<=? ORDER BY seq",
                    (row["processed"], through),
                )
            ]
        frozen = (
            manifest
            if manifest is not None
            else self.freeze(
                self._config, ["study", *[m["alias"] for m in self._mounts]]
            )
        )
        scoped = self._manifest(frozen)
        visible = []
        blocked_by_scope = False
        for record in records:
            reference = {
                key: record[key] for key in ("library_id", "kind", "id", "revision")
            }
            try:
                self._authorized(scoped, reference)
            except PermissionError:
                # Processing is a contiguous cursor. Do not silently acknowledge
                # denied E1 merely because a later E2 is visible.
                blocked_by_scope = True
                break
            visible.append(
                dict(
                    reference=reference,
                    sequence=record["sequence"],
                    source_type=record["source_type"],
                )
            )
        return dict(
            base_version=scoped[row["id"]]["version"],
            processed_through=row["processed"],
            evidence_through=max(
                (record["sequence"] for record in visible), default=row["processed"]
            ),
            evidence=visible,
            affected_pages=self._affected(frozen),
            blocked_by_scope=blocked_by_scope,
        )

    def commit(
        self,
        base_version,
        pages,
        index_markdown,
        processed_through,
        request_id,
        *,
        manifest=None,
        index_dependencies=None,
        conn=None,
    ):
        """CAS-publish page edits, index and processing progress, or advance progress only.

        A page upsert creates its next stable revision. ``archived=True`` keeps its
        history while removing it from the current index/search. Split and merge
        are atomic combinations of upserts and archives in this same operation.
        """
        if (
            type(base_version) is not int
            or base_version < 0
            or type(processed_through) is not int
            or processed_through < 0
        ):
            raise ValueError("Invalid knowledge commit boundary")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Knowledge commit requires an idempotency key")
        if not isinstance(pages, list):
            raise ValueError("pages must be a list")
        if index_markdown is not None and not isinstance(index_markdown, str):
            raise ValueError("index_markdown must be text or null")
        normalized = []
        seen = set()
        for page in pages:
            if not isinstance(page, dict) or set(page) - {
                "id",
                "title",
                "markdown",
                "dependencies",
                "archived",
            }:
                raise ValueError("Invalid knowledge page fields")
            identity = _identifier(page.get("id"))
            if identity in seen:
                raise ValueError("Duplicate page edit")
            seen.add(identity)
            if (
                not isinstance(page.get("title"), str)
                or not page["title"].strip()
                or not isinstance(page.get("markdown"), str)
            ):
                raise ValueError("Knowledge pages require title and Markdown text")
            if type(page.get("archived", False)) is not bool:
                raise ValueError("archived must be boolean")
            normalized.append(
                dict(
                    page,
                    dependencies=_references(page.get("dependencies", [])),
                    archived=page.get("archived", False),
                )
            )
        self._ensure()
        extra_dependencies = _references(index_dependencies or [])
        identity = _json(
            dict(
                base_version=base_version,
                pages=normalized,
                index_markdown=index_markdown,
                processed_through=processed_through,
                index_dependencies=extra_dependencies,
            )
        )
        with (
            nullcontext(conn)
            if conn is not None
            else self._connection(self.root, readonly=True)
        ) as reader:
            prior = reader.execute(
                "SELECT * FROM kb_commits WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior is not None:
                if prior["input_json"] != identity:
                    raise ConflictError(
                        "Knowledge commit retry differs from its original request"
                    )
                return json.loads(prior["receipt_json"])
        if manifest is None:
            manifest = self.freeze(
                self._config, ["study", *[m["alias"] for m in self._mounts]]
            )
        scoped = self._manifest(manifest)
        main_id = self.library_id
        if main_id not in scoped or scoped[main_id]["version"] != base_version:
            raise ConflictError("Commit base differs from its frozen main snapshot")
        if processed_through > scoped[main_id]["evidence_through"]:
            raise ConflictError(
                "Processing progress exceeds the frozen evidence boundary"
            )
        current_map = json.loads(self._snapshot(scoped[main_id])["pages_json"])
        for page in normalized:
            if page["id"] in current_map:
                self._authorized(
                    scoped,
                    dict(
                        library_id=main_id,
                        kind="knowledge",
                        id=page["id"],
                        revision=current_map[page["id"]],
                    ),
                )
            for ref in page["dependencies"]:
                self._authorized(scoped, ref)
        if index_markdown is not None:
            for ref in json.loads(self._snapshot(scoped[main_id])["dependencies_json"]):
                self._authorized(scoped, ref)
        extra_dependencies = _references(index_dependencies or [])
        for ref in extra_dependencies:
            self._authorized(scoped, ref)
        external = conn is not None
        if external and self.path != self.root / DATABASE:
            raise ValueError(
                "External publication transaction requires the Study runtime database"
            )
        with nullcontext(conn) if external else self._connection(self.root) as conn:
            if not external:
                conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT * FROM kb_commits WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior is not None:
                if prior["input_json"] != identity:
                    raise ConflictError(
                        "Knowledge commit retry differs from its original request"
                    )
                return json.loads(prior["receipt_json"])
            current = dict(conn.execute("SELECT * FROM kb_library").fetchone())
            if current["version"] != base_version:
                raise ConflictError("Knowledge base changed before publication")
            if processed_through < current["processed"]:
                raise ConflictError("Knowledge processing cursor cannot move backwards")
            old = dict(
                conn.execute(
                    "SELECT * FROM kb_snapshots WHERE version=?", (base_version,)
                ).fetchone()
            )
            page_map = json.loads(old["pages_json"])
            # Exact unchanged edits do not create redundant revisions.
            edits = []
            for page in normalized:
                previous = conn.execute(
                    "SELECT * FROM kb_revisions WHERE id=? AND revision=?",
                    (page["id"], page_map.get(page["id"], 0)),
                ).fetchone()
                if (
                    previous is not None
                    and previous["title"] == page["title"]
                    and previous["archived"] == page["archived"]
                    and json.loads(previous["dependencies_json"])
                    == page["dependencies"]
                    and self._body(scoped[main_id], previous["path"])
                    == page["markdown"]
                ):
                    continue
                edits.append(page)
            index_changed = index_markdown is not None and (
                not old["index_path"]
                or self._body(scoped[main_id], old["index_path"]) != index_markdown
            )
            changed = bool(
                edits
                or index_changed
                or extra_dependencies != json.loads(old["dependencies_json"])
                and index_dependencies is not None
            )
            version = base_version + int(changed)
            if changed:
                prefix = f"versions/{version}-{uuid.uuid4().hex}"
                for page in edits:
                    revision = page_map.get(page["id"], 0) + 1
                    path = f"{prefix}/pages/{page['id']}.md"
                    _markdown(self.folder / path, page["markdown"])
                    conn.execute(
                        "INSERT INTO kb_revisions VALUES(?,?,?,?,?,?,?)",
                        (
                            page["id"],
                            revision,
                            version,
                            page["title"],
                            path,
                            _json(page["dependencies"]),
                            int(page["archived"]),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO kb_search(title,body,kind,id,revision) VALUES(?,?,'knowledge',?,?)",
                        (page["title"], page["markdown"], page["id"], revision),
                    )
                    conn.executemany(
                        "INSERT INTO kb_dependencies VALUES(?,?,?,?,?,?)",
                        [
                            (
                                page["id"],
                                revision,
                                dep["library_id"],
                                dep["kind"],
                                dep["id"],
                                dep["revision"],
                            )
                            for dep in page["dependencies"]
                        ],
                    )
                    page_map[page["id"]] = revision
                index_path = old["index_path"]
                if index_markdown is not None:
                    index_path = f"{prefix}/index.md"
                    _markdown(self.folder / index_path, index_markdown)
                # All pages contribute to the index's disclosure boundary, even
                # when the Manager omitted an explicit index reference.
                dependencies = list(extra_dependencies)
                if index_dependencies is None and index_markdown is None:
                    dependencies = json.loads(old["dependencies_json"])
                dependencies += [
                    dict(
                        library_id=main_id, kind="knowledge", id=key, revision=revision
                    )
                    for key, revision in page_map.items()
                ]
                dependencies = list({_json(ref): ref for ref in dependencies}.values())
                conn.execute(
                    "INSERT INTO kb_snapshots VALUES(?,?,?,?,?,?)",
                    (
                        version,
                        _json(page_map),
                        index_path,
                        _json(dependencies),
                        scoped[main_id]["evidence_through"],
                        scoped[main_id]["legacy_through"],
                    ),
                )
            for affected in self._affected(manifest, conn):
                conn.execute(
                    "INSERT OR IGNORE INTO kb_maintenance_acks VALUES(?)",
                    (affected["signature"],),
                )
            receipt = dict(
                version=version,
                processed_through=processed_through,
                changed=changed,
                request_id=request_id,
            )
            conn.execute(
                "UPDATE kb_library SET version=?,processed=?",
                (version, processed_through),
            )
            conn.execute(
                "INSERT INTO kb_commits VALUES(?,?,?)",
                (request_id, identity, _json(receipt)),
            )
            if not external:
                conn.commit()
            return receipt

    def reverse_references(self, reference):
        reference = _reference(reference)
        self._ensure()
        found = []
        with self._connection(self.root, readonly=True) as conn:
            for row in conn.execute(
                "SELECT id,revision,version,dependencies_json FROM kb_revisions ORDER BY version,id"
            ):
                if reference in json.loads(row["dependencies_json"]):
                    found.append(
                        dict(
                            library_id=self.library_id,
                            kind="knowledge",
                            id=row["id"],
                            revision=row["revision"],
                            version=row["version"],
                        )
                    )
        return found

    def import_legacy(self, snapshot, publications):
        """Archive accepted pre-library revisions without treating prose as evidence.

        Inputs are ``Publisher.snapshot()`` and ``{publication_id:
        normalized_output}``. Original IDs, revisions, publication IDs and roles
        survive in both metadata and the human-readable Markdown header.
        """
        from .publication_views import _entry

        self._ensure()
        measurement_ids = {row["id"]: row for row in snapshot.get("measurements", [])}
        for batch in publications.values():
            for entry in batch["knowledge"]:
                measurement_ids.update({key: {} for key in entry["measurement_ids"]})
        imported = []
        with self._connection(self.root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in snapshot["knowledge"]:
                entry = _entry(
                    publications[row["publication_id"]]["knowledge"][
                        row["entry_index"]
                    ],
                    row,
                    measurement_ids,
                )
                previous = conn.execute(
                    "SELECT * FROM kb_historical WHERE id=? AND revision=?",
                    (row["id"], row["revision"]),
                ).fetchone()
                metadata = dict(row, entry=entry)
                if previous is not None:
                    if previous["metadata_json"] != _json(metadata):
                        raise ConflictError("Historical publication identity changed")
                    continue
                lines = [
                    f"# {row['id']}",
                    "",
                    f"Original revision: {row['revision']}",
                    f"Publication: {row['publication_id']}",
                    f"Source role: {row['source_role']}",
                    f"Kind: {row['kind']}",
                    "",
                    entry["statement"],
                    "",
                ]
                for heading, field in (
                    ("Conditions", "conditions"),
                    ("Sources", "source_refs"),
                    ("Measurement IDs", "measurement_ids"),
                    ("Limitations", "limitations"),
                ):
                    lines.extend(
                        [
                            f"## {heading}",
                            "",
                            *[f"- {value}" for value in entry[field]],
                            "",
                        ]
                    )
                lines.extend(
                    ["## Interpretation (unverified)", "", entry["interpretation"], ""]
                )
                path = f"historical/{uuid.uuid4().hex}.md"
                _markdown(self.folder / path, "\n".join(lines))
                conn.execute(
                    "INSERT INTO kb_historical VALUES(?,?,?,?,?)",
                    (
                        row["id"],
                        row["revision"],
                        row["publication_id"],
                        path,
                        _json(metadata),
                    ),
                )
                conn.execute(
                    "INSERT INTO kb_search(title,body,kind,id,revision) VALUES(?,?,'historical',?,?)",
                    (row["id"], "\n".join(lines), row["id"], row["revision"]),
                )
                imported.append(
                    dict(
                        library_id=self.library_id,
                        kind="historical",
                        id=row["id"],
                        revision=row["revision"],
                    )
                )
            conn.commit()
        return imported
