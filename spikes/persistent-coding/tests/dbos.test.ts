import assert from "node:assert/strict";
import {
  appendFileSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage } from "@earendil-works/pi-ai/providers/faux";
import { initRun, PROMPT } from "../common.ts";
import {
  dbosIdentity,
  reconcileDbosSession,
  runDbos,
  statusDbos,
} from "../dbos.ts";

function fixture() {
  const parent = mkdtempSync(join(tmpdir(), "rl95-dbos-test-"));
  const root = join(parent, "run");
  const config = initRun(root, "dbos");
  const identity = dbosIdentity(root, config);
  const start = () => {
    mkdirSync(dirname(identity.sessionFile), { recursive: true });
    const manager = SessionManager.open(
      identity.sessionFile,
      undefined,
      config.workspace,
    );
    manager.appendCustomEntry("persistent-coding-episode", {
      fingerprint: identity.fingerprint,
    });
    manager.appendMessage({
      role: "user",
      content: [{ type: "text", text: PROMPT }],
      timestamp: Date.now(),
    });
    return manager;
  };
  return {
    parent,
    root,
    config,
    identity,
    start,
    cleanup: () => rmSync(parent, { recursive: true, force: true }),
  };
}

test("DBOS reconciliation reads exact completed Pi session without modifying it", () => {
  const f = fixture();
  try {
    assert.equal(reconcileDbosSession(f.root, f.config), null);
    const manager = f.start();
    manager.appendMessage(
      fauxAssistantMessage('{"summary":"done","tests_passed":true}'),
    );
    const before = readFileSync(f.identity.sessionFile, "utf8");
    const result = reconcileDbosSession(f.root, f.config);
    assert.equal(result?.sessionId, manager.getSessionId());
    assert.equal(result?.output, '{"summary":"done","tests_passed":true}');
    assert.equal(readFileSync(f.identity.sessionFile, "utf8"), before);
    assert.equal(
      readFileSync(join(f.config.workspace, "app.py"), "utf8").includes(
        "raise",
      ),
      false,
    );
  } finally {
    f.cleanup();
  }
});

test("DBOS reconciliation rejects interrupted prompt and damaged terminal tail", () => {
  const f = fixture();
  try {
    const manager = f.start();
    assert.throws(
      () => reconcileDbosSession(f.root, f.config),
      /MANUAL_RECONCILIATION_REQUIRED.*interrupted/,
    );
    manager.appendMessage(fauxAssistantMessage("done"));
    appendFileSync(f.identity.sessionFile, '{"type":"message"');
    assert.throws(
      () => reconcileDbosSession(f.root, f.config),
      /MANUAL_RECONCILIATION_REQUIRED.*incomplete tail/,
    );
  } finally {
    f.cleanup();
  }
});

test("DBOS reconciliation rejects changed frozen episode identity", () => {
  const f = fixture();
  try {
    f.start().appendMessage(fauxAssistantMessage("done"));
    assert.throws(
      () =>
        reconcileDbosSession(f.root, {
          ...f.config,
          deadline: f.config.deadline + 1,
        }),
      /MANUAL_RECONCILIATION_REQUIRED.*identity/,
    );
  } finally {
    f.cleanup();
  }
});

test("DBOS reconciliation never initializes a claimed empty session on recovery", () => {
  const f = fixture();
  try {
    mkdirSync(dirname(f.identity.sessionFile), { recursive: true });
    writeFileSync(f.identity.sessionFile, "", { flag: "wx" });
    assert.throws(
      () => reconcileDbosSession(f.root, f.config),
      /MANUAL_RECONCILIATION_REQUIRED/,
    );
    assert.equal(readFileSync(f.identity.sessionFile, "utf8"), "");
  } finally {
    f.cleanup();
  }
});

test(
  "DBOS real PostgreSQL + Pi native tools coding smoke and completed-step replay",
  {
    skip: !process.env.DBOS_SYSTEM_DATABASE_URL,
    timeout: 60_000,
  },
  async () => {
    const f = fixture();
    try {
      const first = await runDbos(f.root);
      assert.equal(first.verification.passed, true);
      const before = readFileSync(join(f.root, "trace.jsonl"), "utf8");
      const requests = before
        .split("\n")
        .filter((line) => line.includes('"event":"model.request"')).length;
      assert.equal(requests, 7);
      assert.match(before, /"name":"read"/);
      assert.match(before, /"name":"edit"/);
      assert.match(before, /"name":"bash"/);
      assert.match(before, /"isError":true/);
      assert.match(before, /"event":"pi_idle"/);
      assert.match(
        before,
        /"event":"pi_settled","candidate":"dbos","aborted":false/,
      );
      assert.deepEqual(await runDbos(f.root), first);
      assert.equal(readFileSync(join(f.root, "trace.jsonl"), "utf8"), before);
      assert.equal((await statusDbos(f.root))?.status, "SUCCESS");
    } finally {
      f.cleanup();
    }
  },
);
