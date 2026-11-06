# Operations

Use one writable host per Study and never run `run`, `resume` or `maintain` concurrently for the same root. The [runtime guide](../runtime/README.md) gives the commands for initialization, status, original submission inspection and recovery. `status` reports framework state and usage; the immutable receipts and independent Reviews are authoritative. Published `knowledge/research.md` is a reading aid, not measurement authority.

For a new Study, optional investigation and experiment concurrency limits are independent and default to 1. Internal concurrency still uses one writable host and one workflow executor; see the [concurrency contract](../docs/durable-research.md#concurrency). The original deadline and authorized slots remain fixed. Never resend an unknown submission; query, bind the original job identity and collect before an eligible resume. STOP or expiry forbids new admission or model work, but inspection and reconciliation remain available. Workflow cancellation does not prove the physical job stopped. See the [operating contract](../docs/durable-research.md).

Inspect effective role definitions and frozen conversation grants with the [role context commands](../docs/role-context.md). Context configuration is frozen at Study start; pre-feature conversations require their original runtime for reconciliation.

For a retired Study, follow the [read-only history guide](../docs/runtime-retirement.md). It is not a migration tool or proof that remote effects are absent. Never alter control records to force progress or create a replacement Study to bypass unresolved effects.
