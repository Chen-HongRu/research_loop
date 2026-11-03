"""Persistent, operation-scoped supervisor failure observations."""


class FailureCounts:
    """A retry threshold never widens a failure's affected scope."""

    def __init__(self, previous, target):
        self.target = str(target)
        self.rows = previous.get("failures", [])
        if not self.rows and previous.get("consecutive_failures"):
            self.rows = [
                {
                    "scope": "parent",
                    "target": self.target,
                    "stage": previous.get("stage"),
                    "error": previous.get("error"),
                    "consecutive_failures": previous["consecutive_failures"],
                    "blocked_since": previous.get("blocked_since"),
                }
            ]

    def success(self, stage):
        for row in self.rows:
            if row["target"] == self.target and row["stage"] == stage:
                row["consecutive_failures"] = 0
                row["blocked_since"] = None

    def count(self, stage, message):
        return max(
            (
                row["consecutive_failures"]
                for row in self.rows
                if row["target"] == self.target
                and row["stage"] == stage
                and row["error"] == message
            ),
            default=0,
        )

    def failure(self, stage, message, now):
        key = (
            "global" if stage == "control" else "parent",
            self.target,
            stage,
            message,
        )
        row = next(
            (
                r
                for r in self.rows
                if tuple(r[k] for k in ("scope", "target", "stage", "error")) == key
            ),
            None,
        )
        if row is None:
            row = dict(zip(("scope", "target", "stage", "error"), key))
            row.update(consecutive_failures=0, blocked_since=None)
            self.rows.append(row)
        first = row["consecutive_failures"] == 0
        if first:
            row["blocked_since"] = now
        row["consecutive_failures"] += 1
        return row, first
