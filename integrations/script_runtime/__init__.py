"""Operator-configured native Slurm script execution.

API: backend.validate_config(config) and validate_request(request, config) are
pure. invoke(action, operation, request, config, *, transport=None) performs one
bounded native action and returns {event_key, data}; submit/inspect/cancel/collect/
settle aliases map to submission/inspection/cancellation/collection/settlement.
The application must independently require exact accepted-Plan operation equality.

Request parameters are exactly {baseline_ref, source_files, execution}.
source_files is a finite list of {path, content} UTF-8 replacements/additions.
execution is {mode:'script', entrypoint:'research/<name>.py', gpu:true,
time_limit_seconds:1..1800, inputs:[{name,source}], outputs:[relative paths]}.
Resources must equal {resource_pool:1}; cost bounds must equal
{cost_unit:ceil(time_limit_seconds/60)*60}. Lifecycle timeout additionally includes
the configured staging_headroom_seconds, while fees count allocated GPU seconds.

All configuration keys are required (see backend.CONFIG_FIELDS). Paths/identity:
- journal_root, study_root: private durable local operation/control artifact roots.
- baseline_root, baseline_ref, source_paths: explicitly enumerated source snapshot;
  protected_paths is a subset. edit_roots admits new source below those roots.
- runtime_files: {destination_relative_path: trusted_absolute_source_file}.
- shared_exposure_gate: {path, identity:{device,inode,size,mtime_ns}} identifying a
  small operator-owned JSON object with shared_exposure_reconciled:true. This is
  an external authorization assertion, not a clearance certificate the backend
  can generate. The operator must reconcile the original shared GPU exposure.

Transport/allocation: ssh_target, slurm_user, slurm_timezone (IANA zone matching
Slurm's timestamp parser), remote_root, partition, account, cpus, memory,
remote_python; resource_pool/cost_unit name the native resource and fee vectors.
command_timeout_seconds and cleanup_timeout_seconds are each 1..120.
staging_headroom_seconds is staging/queue headroom including a fixed60s safety
margin and must exceed60. Admission reserves it once; preparation/staging consume
it. Submission checks only remaining allocation plus60s before and after staging.
This headroom is not allocated cost.

Inputs/environment:
- input_mappings: {source:{path:absolute, mount:absolute, kind:file|directory}}.
- metadata_checks: [{path:absolute,kind:file|directory,identity:stat_identity|null}].
  These perform only bounded stat/type checks, never dataset/checkpoint reads.
- launcher_source: trusted Python body receiving RUN={remote,execution,inputs,
  seconds,deadline}. It MUST preserve the authorized environment and sandbox,
  honor input mounts, execute only the declared entrypoint, write reports beneath
  remote/output, and keep child processes within the Slurm allocation. The backend
  does not certify a launcher supplied by the operator. Configured staging belongs
  here and runs inside the charged allocation, not in free prepare.

Reports: report_suffixes is a subset of [.json,.csv,.txt,.log,.md];
max_report_bytes<=1MiB, max_collection_bytes<=16MiB. Only declared matching small
reports are transferred. Other declared outputs (including checkpoints) receive
metadata-only descriptors and remain remote. Collection publishes immutable JSON
control artifacts under study_root/artifacts with optional research_evidence;
all are explicitly unverified and evaluations is always empty. allow_feedback is
true for these authorized development reports; use separate backend configuration
when the data-role boundary must prohibit feedback.

The journal is fsynced before any remote effect and records exactly one sbatch
intent, native token/original owner and request/backend/environment identities.
After any uncertain effect there is no retry. Inspection/fees query the original
full job name/comment/user/account; contradictions fail closed. Missing accounting
never proves absence, release or zero fees. Cancellation ACK is not release.
"""
