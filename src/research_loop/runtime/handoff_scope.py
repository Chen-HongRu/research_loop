"""Carry bound disclosure authority across directly supplied role outputs.

This inspects immutable dispatch metadata, never tool logs or knowledge bodies.
The task pool is shared control state, so model-authored changes must be suitable
for every role before they enter that pool.
"""

import json

from .call_context import dispatched_input
from .knowledge import KnowledgeBase
from .role_access import RoleAccess, directory_grants
from .source_scope import derived_dependencies, directory_source_scope
from .store import StateStore

_NATIVE = {"native-runtime-v1", "native-script-runtime-v1"}


def require_bound_assembly(assembly):
    if assembly.get("version") != 2 or not isinstance(
        assembly.get("knowledge_dependencies"), list
    ):
        raise ValueError(
            "Native output lacks reliable disclosure authority; settle it with its original deployment"
        )


def attempt_dependencies(store, attempt_id, library_id):
    with store._connection(readonly=True) as conn:
        run = conn.execute(
            "SELECT adapter_json FROM dispatch_runs WHERE category='call' AND attempt_id=?",
            (attempt_id,),
        ).fetchone()
    # Explicit legacy/custom controller results have no native directory grants.
    if run is None:
        return []
    packet = dispatched_input(store, attempt_id)
    assembly = packet.get("assembly")
    if assembly is None:
        if json.loads(run["adapter_json"]).get("id") in _NATIVE:
            require_bound_assembly({})
        return []
    require_bound_assembly(assembly)
    return derived_dependencies(assembly, library_id)


def authorize_dependencies(library, dependencies, *, role, aliases):
    """Check whole-library identity/boundaries and independent source grants."""
    ceiling = RoleAccess.resolve(library._config, role).rule.get("knowledge_bases")
    if ceiling is not None and not set(aliases) <= set(ceiling):
        raise PermissionError("Recipient library aliases exceed its role grants")
    metadata = library.directory_bindings(list(dict.fromkeys(["study", *aliases])))
    local_id = metadata[0]["library_id"]
    allowed = {}
    for item in metadata:
        if item["alias"] not in aliases:
            continue
        previous = allowed.get(item["library_id"])
        if previous is None or (
            previous["version"] is not None
            and (item["version"] is None or item["version"] > previous["version"])
        ):
            allowed[item["library_id"]] = item
    grants = directory_source_scope(library, library._config, role, metadata)
    for reference in dependencies:
        if reference["kind"] == "source":
            if reference not in grants:
                raise PermissionError(
                    "Upstream input exceeds the recipient's source grants"
                )
        elif reference["kind"] == "library":
            revision = reference["revision"]
            actual = allowed.get(reference["library_id"])
            # Direct role outputs stay in this Study. Only its own identity uses
            # zero for live access; a different library may have fixed version 0.
            requires_live = reference["library_id"] == local_id and revision == 0
            if (
                reference["id"] != "library"
                or type(revision) is not int
                or revision < 0
                or actual is None
                or (
                    actual["version"] is not None
                    and (requires_live or actual["version"] < revision)
                )
            ):
                raise PermissionError(
                    "Upstream library exceeds the recipient's knowledge grants"
                )
        else:
            raise PermissionError("Unknown upstream disclosure authority")


def authorize_publication(library, dependencies, readers):
    """Require every intended reader to be authorized for free-text derivatives."""
    for role, aliases in readers.items():
        try:
            authorize_dependencies(library, dependencies, role=role, aliases=aliases)
        except PermissionError as error:
            raise PermissionError(
                f"Shared publication exceeds recipient role {role}'s disclosure grants"
            ) from error


def inherited_dependencies(library, package, *, role, aliases):
    """Resolve only role outputs actually present after input selection."""
    if not (library.root / "runtime.sqlite3").exists():
        return []
    store = StateStore(library.root)
    attempts = set()
    context = package["context"]
    with store._connection(readonly=True) as conn:
        for source in package["manifest"]["sources"]:
            if source["disposition"] == "omitted" or source["key"] not in context:
                continue
            ref = source["ref"]
            if ref.startswith("control:attempt:"):
                attempts.add(ref.removeprefix("control:attempt:"))
            else:
                attempts.update(
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM attempts WHERE output_ref=?", (ref,)
                    )
                )
        tasks = []
        if isinstance(context.get("task"), dict):
            tasks.append(context["task"])
        if isinstance(context.get("planning"), dict):
            tasks.extend(context["planning"].get("pool", {}).get("tasks", []))
        for task in tasks:
            row = conn.execute(
                "SELECT origin_id FROM task_revisions WHERE id=? AND revision=? AND origin_kind='judge'",
                (task.get("id"), task.get("revision")),
            ).fetchone()
            if row:
                attempts.add(row[0])
        # The optional legacy history provider directly renders accepted outputs.
        if {"research", "frontiers"} & context.keys():
            version = package["manifest"]["revisions"].get("knowledge", 0)
            attempts.update(
                row[0]
                for row in conn.execute(
                    "SELECT attempt_id FROM publications WHERE id<=?", (version,)
                )
            )
    result = []
    for attempt_id in sorted(attempts):
        for ref in attempt_dependencies(store, attempt_id, library.library_id):
            if ref not in result:
                result.append(ref)
    if result:
        try:
            authorize_dependencies(library, result, role=role, aliases=aliases)
        except PermissionError as error:
            raise PermissionError(
                f"Role {role} cannot receive upstream output outside its source or knowledge grants"
            ) from error
    return result


def authorize_task_publication(owner, attempt_id, config):
    store = owner.store
    library = KnowledgeBase(store.root, config=config)
    dependencies = attempt_dependencies(store, attempt_id, library.library_id)
    # A shared task can subsequently be supplied to any role. Configuration may
    # narrow that role's KB aliases; its source grants remain independent.
    if not dependencies:
        return
    authorize_publication(library, dependencies, directory_grants(config))
