import assert from "node:assert/strict";
import {
  chmodSync,
  copyFileSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import { inspectHistory, readHistoryFile } from "../src/history.ts";

function seal(root: string): void {
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) seal(path);
    else chmodSync(path, 0o444);
  }
  chmodSync(root, 0o555);
}
function cleanup(root: string): void {
  chmodSync(root, 0o755);
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    if (entry.isDirectory() && !entry.isSymbolicLink())
      cleanup(join(root, entry.name));
  }
  rmSync(root, { recursive: true, force: true });
}
function metadata(path: string) {
  const value = lstatSync(path);
  return {
    mode: value.mode,
    size: value.size,
    mtime: value.mtimeMs,
    ctime: value.ctimeMs,
    inode: value.ino,
  };
}
function contents(root: string) {
  return {
    directory: metadata(root),
    files: readdirSync(root)
      .sort()
      .map((name) => ({
        name,
        metadata: metadata(join(root, name)),
        bytes: readFileSync(join(root, name)),
      })),
  };
}

function walFixture(version = 8) {
  const parent = mkdtempSync(join(tmpdir(), "history-synthetic-wal-"));
  const live = join(parent, "live");
  const snapshot = join(parent, "snapshot");
  mkdirSync(live);
  mkdirSync(snapshot);
  const database = new DatabaseSync(join(live, "runtime.sqlite3"));
  database.exec(`
    PRAGMA journal_mode=WAL;
    PRAGMA wal_autocheckpoint=0;
    CREATE TABLE control(study_id TEXT,owner_open INTEGER,owner_epoch INTEGER,admission_mode TEXT);
    CREATE TABLE operations(id TEXT,job_id TEXT,state TEXT,unexpected_job_ids_json TEXT,external_open INTEGER,collection_ref TEXT,authorization_id TEXT);
    CREATE TABLE dispatch_runs(token TEXT,state TEXT,worker_retired INTEGER,settled INTEGER,input_ref TEXT,completion_ref TEXT);
    INSERT INTO control VALUES('synthetic-study',1,2,'active');
    PRAGMA user_version=${version};
    PRAGMA wal_checkpoint(TRUNCATE);
  `);
  const mainBefore = readFileSync(join(live, "runtime.sqlite3"));
  database.exec(`
    INSERT INTO operations VALUES('recorded-job','synthetic-original-job','submitted','[]',1,'artifacts/original-collection.json','synthetic-authorization');
    INSERT INTO operations VALUES('uncertain-job',NULL,'unknown_submitted','["synthetic-unexpected-job"]',1,NULL,'synthetic-authorization');
    INSERT INTO dispatch_runs VALUES('synthetic-invocation','unknown',0,0,'artifacts/original-input.json',NULL);
  `);
  assert.deepEqual(
    readFileSync(join(live, "runtime.sqlite3")),
    mainBefore,
    "Committed job rows must still exist only in WAL",
  );
  assert.ok(statSync(join(live, "runtime.sqlite3-wal")).size > 0);
  // The synthetic source connection is quiescent while all three files are copied.
  // Keeping it open prevents last-connection checkpoint/removal of the test WAL.
  for (const name of [
    "runtime.sqlite3",
    "runtime.sqlite3-wal",
    "runtime.sqlite3-shm",
  ])
    copyFileSync(join(live, name), join(snapshot, name));
  seal(snapshot);
  return {
    parent,
    live,
    snapshot,
    close: () => {
      database.close();
      cleanup(parent);
    },
  };
}

test("schema-8 history reads WAL-only original jobs without changing DB, WAL, SHM or directory metadata", () => {
  const f = walFixture();
  try {
    const before = contents(f.snapshot);
    const view = inspectHistory(f.snapshot) as any;
    assert.equal(view.format, "legacy-sqlite");
    assert.equal(view.sqlite.schemaVersion, 8);
    assert.equal(view.deploymentState, "unknown");
    assert.equal(view.sqlite.tables.control.rows[0].owner_open, 1);
    const rows = view.sqlite.tables.operations.rows;
    assert.equal(
      rows.find((row: any) => row.id === "recorded-job").job_id,
      "synthetic-original-job",
    );
    const uncertain = rows.find((row: any) => row.id === "uncertain-job");
    assert.equal(uncertain.job_id, null);
    assert.equal(uncertain.state, "unknown_submitted");
    assert.equal(uncertain.external_open, 1);
    assert.equal(
      uncertain.unexpected_job_ids_json,
      '["synthetic-unexpected-job"]',
    );
    assert.equal(rows[0].collection_ref, "artifacts/original-collection.json");
    assert.equal(view.sqlite.tables.dispatch_runs.rows[0].state, "unknown");
    assert.match(view.notes.join(" "), /null or missing job_id is not proof/);
    assert.deepEqual(contents(f.snapshot), before);
  } finally {
    f.close();
  }
});

test("writable originals and incomplete WAL snapshots are refused before creating sidecars", () => {
  const f = walFixture();
  try {
    const liveBefore = contents(f.live);
    const refused = inspectHistory(f.live) as any;
    assert.equal(refused.status, "unknown");
    assert.match(refused.reason, /Writable.*refused/);
    assert.deepEqual(contents(f.live), liveBefore);
    chmodSync(f.snapshot, 0o755);
    rmSync(join(f.snapshot, "runtime.sqlite3-shm"));
    chmodSync(f.snapshot, 0o555);
    const incompleteBefore = contents(f.snapshot);
    const incomplete = inspectHistory(f.snapshot) as any;
    assert.equal(incomplete.status, "unknown");
    assert.match(incomplete.sqlite.reason, /runtime.sqlite3-shm missing/);
    assert.deepEqual(contents(f.snapshot), incompleteBefore);
  } finally {
    f.close();
  }
});

test("unsupported SQLite schema stays unknown and does not assert that the deployment is inactive", () => {
  const f = walFixture(999);
  try {
    const before = contents(f.snapshot);
    const view = inspectHistory(f.snapshot) as any;
    assert.equal(view.status, "unknown");
    assert.equal(view.sqlite.schemaVersion, 999);
    assert.equal(view.deploymentState, "unknown");
    assert.match(view.sqlite.reason, /Unsupported/);
    assert.deepEqual(contents(f.snapshot), before);
  } finally {
    f.close();
  }
});

test("legacy JSON, Plan/Review text and invocation references retain their original meaning and bytes", () => {
  const parent = mkdtempSync(join(tmpdir(), "history-synthetic-text-"));
  try {
    const root = join(parent, "snapshot");
    mkdirSync(root);
    writeFileSync(
      join(root, "study.json"),
      '{"version":3,"workspace":"/path/to/original-workspace"}\n',
    );
    writeFileSync(
      join(root, "state.json"),
      '{"phase":"review","status":"paused","active_attempt":"original-attempt","goal_status":"undetermined","review_ref":"iterations/0001/review.md"}\n',
    );
    const original =
      "\ufeff# Original Plan\n\n  Keep original spelling and whitespace.  \n";
    mkdirSync(join(root, "iterations", "0001"), { recursive: true });
    writeFileSync(join(root, "iterations", "0001", "plan.md"), original);
    const invocation = join(root, "invocations", "synthetic-token");
    mkdirSync(invocation, { recursive: true });
    writeFileSync(
      join(invocation, "claimed.json"),
      JSON.stringify({
        launch_token: "synthetic-token",
        binding: {
          kind: "backend",
          input_ref: "artifacts/request.json",
          deadline: 1234,
        },
      }),
    );
    writeFileSync(join(invocation, "started.json"), '{"pid":12345}\n');
    seal(root);
    const before = metadata(join(root, "iterations", "0001", "plan.md"));
    assert.equal(readHistoryFile(root, "iterations/0001/plan.md"), original);
    const view = inspectHistory(root) as any;
    assert.equal(view.status, "historical-metadata");
    assert.equal(view.original.state.metadata.phase, "review");
    assert.equal(view.original.state.metadata.goal_status, "undetermined");
    assert.equal(view.invocations.records[0].currentState, "unknown");
    assert.equal(
      view.invocations.records[0].claim.metadata.binding.input_ref,
      "artifacts/request.json",
    );
    assert.ok(
      view.invocations.records[0].refs.includes(
        "invocations/synthetic-token/started.json",
      ),
    );
    assert.deepEqual(
      metadata(join(root, "iterations", "0001", "plan.md")),
      before,
    );
    assert.equal(
      readHistoryFile(root, "state.json"),
      readFileSync(join(root, "state.json"), "utf8"),
    );
  } finally {
    cleanup(parent);
  }
});

test("explicit text reading rejects traversal, symlinks, writable paths and unknown JSON schemas remain unknown", () => {
  const parent = mkdtempSync(join(tmpdir(), "history-synthetic-path-"));
  try {
    const root = join(parent, "snapshot");
    mkdirSync(root);
    writeFileSync(join(parent, "outside.md"), "synthetic outside text\n");
    writeFileSync(join(root, "study.json"), '{"version":999}\n');
    symlinkSync(join(parent, "outside.md"), join(root, "link.md"));
    symlinkSync(parent, join(root, "directory-link"), "dir");
    seal(root);
    for (const reference of [
      "../outside.md",
      "/outside.md",
      "directory-link/outside.md",
      "link.md",
      "./study.json",
      "a/../study.json",
      "a\\study.json",
    ])
      assert.throws(() => readHistoryFile(root, reference));
    const view = inspectHistory(root) as any;
    assert.equal(view.status, "unknown");
    assert.equal(view.deploymentState, "unknown");
    assert.equal(readHistoryFile(root, "study.json"), '{"version":999}\n');
    chmodSync(join(root, "study.json"), 0o644);
    assert.throws(() => readHistoryFile(root, "study.json"), /Writable/);
  } finally {
    cleanup(parent);
  }
});
