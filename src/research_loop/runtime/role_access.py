"""Study-owned role rules. Selection policies may narrow these inputs further."""

from dataclasses import dataclass


# Output sections that can request effects through the trusted controller.
ROLE_ACTIONS = {
    "plan": {"operations"},
    "execute": {"operations", "source_edits"},
    "review": {"knowledge", "reviews", "corrections"},
    "scout": {"knowledge"},
    "advocate": {"proposals"},
    "judge": {"changes"},
    "knowledge_manager": {"pages", "index_markdown"},
}
ROLES = frozenset(ROLE_ACTIONS)


def directory_grants(config):
    """Immutable Study ceilings for every reader, including existing calls."""
    rules = config.get("role_access", {})
    result = {}
    for role in sorted(ROLES):
        rule = rules.get(role, {})
        result[role] = list(
            rule.get(
                "knowledge_bases",
                [] if rule.get("published_history") is False else ["study"],
            )
        )
    return result


def shared_knowledge_readers(config):
    """All roles that can retain a live handle to this Study's shared library."""
    return {
        role: aliases
        for role, aliases in directory_grants(config).items()
        if "study" in aliases
    }


def check_draft_actions(paths, actions):
    paths = set(paths)
    if "index.md" in paths and "index_markdown" not in actions:
        raise ValueError("Role knowledge_manager lacks permission for index_markdown")
    if paths - {"index.md"} and "pages" not in actions:
        raise ValueError("Role knowledge_manager lacks permission for pages")


def validate_role_access(config):
    rules = config.get("role_access", {})
    if not isinstance(rules, dict) or not rules.keys() <= ROLES:
        raise ValueError("role_access requires a map of known role names")
    for role, rule in rules.items():
        if not isinstance(rule, dict) or rule.keys() - {
            "permissions",
            "context_sources",
            "published_history",
            "actions",
            "inputs",
            "knowledge_bases",
            "tools",
        }:
            raise ValueError(f"Unknown role_access rule for {role}")
        if type(rule.get("published_history", True)) is not bool:
            raise ValueError("published_history must be boolean")
        for key in (
            "permissions",
            "context_sources",
            "actions",
            "inputs",
            "knowledge_bases",
            "tools",
        ):
            names = rule.get(key, [])
            if (
                not isinstance(names, list)
                or any(not isinstance(n, str) or not n.strip() for n in names)
                or len(names) != len(set(names))
            ):
                raise ValueError(f"role_access {key} requires unique names")
        if set(rule.get("actions", [])) - ROLE_ACTIONS[role]:
            raise ValueError(f"Invalid action for {role}")


@dataclass(frozen=True)
class RoleAccess:
    role: str
    rule: dict
    configured: bool
    actions: frozenset[str]

    @classmethod
    def resolve(cls, config, role):
        if role not in ROLES:
            raise ValueError("Unknown runtime role")
        rules = config.get("role_access", {})
        rule = rules.get(role, {}).copy()
        if "published_history" not in rule:
            adapter = config.get("runtime", {}).get("worker_adapter", {}).get("id")
            definition = config.get("role_definitions", {}).get(role, {})
            if adapter in {"native-runtime-v1", "native-script-runtime-v1"}:
                rule["published_history"] = "history" in definition.get("inputs", [])
        actions = set(rule.get("actions", ROLE_ACTIONS[role]))
        if role in rules:
            for permission, action in (
                ("trusted_backend_execution", "operations"),
                ("source_edits", "source_edits"),
            ):
                permissions = config.get("permissions", {})
                if permission in permissions and (
                    permissions[permission] is not True
                    or permission not in rule.get("permissions", permissions)
                ):
                    actions.discard(action)
        return cls(role, rule, role in rules, frozenset(actions))

    @property
    def published_history(self):
        return self.rule.get("published_history", True)

    @property
    def resource_context(self):
        return self.role in {"plan", "execute"}

    @property
    def accepted_plan(self):
        return self.role in {"execute", "review"}

    @property
    def own_execution(self):
        return self.role == "review"

    def permissions(self, config):
        values = config.get("permissions", {})
        if "permissions" not in self.rule:
            return values
        return {k: v for k, v in values.items() if k in self.rule["permissions"]}

    def controls(self, config):
        controls = {"permissions": self.permissions(config)}
        if self.configured:
            controls["allowed_actions"] = sorted(self.actions)
        return controls

    def source_allowed(self, entry):
        names = self.rule.get("context_sources")
        return (
            entry["allow_feedback"]
            and self.role in entry["roles"]
            and (names is None or entry["name"] in names)
        )

    def check_output(self, output):
        if not self.configured:
            return
        if self.role == "knowledge_manager":
            check_draft_actions(
                set(output.get("files", {})).union(output.get("remove_paths", [])),
                self.actions,
            )
        for action in ROLE_ACTIONS[self.role] - self.actions:
            effect = (
                output.get(action) is not None
                if action == "index_markdown"
                else bool(output.get(action))
            )
            if effect:
                raise ValueError(f"Role {self.role} lacks permission for {action}")
