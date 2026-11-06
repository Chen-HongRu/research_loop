# Role context

A Study may configure `context` in its `StudyConfig` before initialization. Omitted policies use the defaults below. `instructions` adds text to the role instructions; `tools`, `readOnly`, and `writable` replace their respective default lists, rather than extending them.

```json
"context": {
  "mounts": { "literature": "/path/to/public-literature" },
  "roles": {
    "plan": {
      "instructions": "Consider the supplied literature.",
      "tools": ["read", "bash", "submit_plan"],
      "readOnly": ["workspace", "inputs", "literature"]
    }
  }
}
```

| Role    | Default tools                                                       | Default read-only resources                               | Writable resources |
| ------- | ------------------------------------------------------------------- | --------------------------------------------------------- | ------------------ |
| Plan    | `read`, `submit_plan`                                               | `workspace`, `inputs`, `knowledge`                        | none               |
| Execute | `read`, `write`, `edit`, `bash`, `run_experiment`, `finish_execute` | `inputs`, `knowledge`                                     | `workspace`        |
| Review  | `read`, `submit_review`                                             | `workspace`, `inputs`, `knowledge`, `records`, `evidence` | none               |
| Manager | `read`, `write`, `edit`, `bash`, `publish_notes`                    | `knowledge`, `records`                                    | `workspace`        |

Plan and Review may additionally select `bash`, but their workspace and reference paths remain read-only and they cannot select `write` or `edit`. Each role must retain its finish tool; Execute must also retain `run_experiment`. Output schemas and mandatory inputs are fixed. Each role must retain access to its workspace, and Review must retain `evidence`. Write grants are fixed to the Execute and Manager workspaces. Built-in read resources are limited to those in the table for each role, plus `workspace` for Execute and Manager; a resource cannot be both readable and writable in one policy.

The sandbox also supplies system runtime libraries and private writable scratch independently of configured role data grants.

Named mounts are explicit **trusted operator read grants**. Give them bounded absolute paths to public or development references only. Initialization canonicalizes mount paths and rejects root, reserved or invalid names and unknown resources. No content scan is performed, and mounts are not versioned knowledge libraries. Only roles that list a mount alias receive its read grant. File grants do not remove controller-authoritative mandatory input values: for example, a role can still receive inlined task, Plan or receipt data when `inputs` or `records` file access is omitted.

The controller freezes the assembled instructions, mandatory inputs, prompt, output schema, tools and resolved path access in each conversation. Tool configuration, tool authorization and the sandbox use that same assembly. Published knowledge remains a live read for roles granted `knowledge`; Manager's private draft starts from published notes. SDK conversation history automatic compaction is disabled.

Inspect the effective definitions before starting, or a stored conversation after it is issued:

```sh
node dist/cli.js context /path/to/run
node dist/cli.js context /path/to/run CONVERSATION_ID --expand
```

The first command reads the configuration and shows effective definitions and mounts without starting a model, daemon or new SQLite database. By default previews show input names, tools, output schema and grants, omitting instructions, input bodies and prompts; `--expand` includes them. Use `status` state `investigations` planner and executor conversation IDs, and record conversation IDs for Review. The programmatic interfaces are exported from `research-loop/context` and `studyContext` from `research-loop/engine`.

The Study configuration is frozen once started; changing it on start or resume is rejected. This does not create a new model budget or support hot reload. Conversations issued before frozen role contexts were introduced cannot execute under this runtime; completed records and status remain readable. Reconcile them using their original release; there is no automatic migration or replay.
