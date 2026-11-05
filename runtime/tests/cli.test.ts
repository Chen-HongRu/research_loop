import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const cli = fileURLToPath(new URL("../src/cli.ts", import.meta.url));
function run(args: string[]) {
  return spawnSync(
    process.execPath,
    ["--experimental-strip-types", cli, ...args],
    {
      encoding: "utf8",
      timeout: 15_000,
    },
  );
}

test("every legacy CLI admission rejects before creating SQLite, STOP or a new identity", () => {
  const root = mkdtempSync(join(tmpdir(), "research-cli-legacy-"));
  try {
    const original = '{"version":3,"originalAuthorization":"unchanged"}\n';
    writeFileSync(join(root, "study.json"), original);
    writeFileSync(
      join(root, "runtime.sqlite3"),
      "synthetic old database marker",
    );
    const files = readdirSync(root).sort();
    for (const args of [
      ["init", root, join(root, "study.json")],
      ["run", root],
      ["resume", root],
      ["maintain", root],
      ["status", root],
      ["stop", root],
      ["submission", root, "original-slot"],
      ["submission", root, "original-slot", "bind", "original-job"],
      ["submission", root, "original-slot", "collect"],
    ]) {
      const result = run(args);
      assert.equal(result.status, 1, `${args[0]}: ${result.stderr}`);
      assert.match(result.stderr, /history/);
      assert.deepEqual(readdirSync(root).sort(), files);
      assert.equal(readFileSync(join(root, "study.json"), "utf8"), original);
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("history CLI returns explicitly requested original text without wrapping or writing", () => {
  const root = mkdtempSync(join(tmpdir(), "research-cli-history-"));
  const path = join(root, "goal.md");
  const original = "\uFEFF  # Synthetic historical goal\n\n  original text\n";
  try {
    writeFileSync(path, original, { mode: 0o444 });
    chmodSync(root, 0o555);
    const result = run(["history", root, "--file", "goal.md"]);
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, original);
    assert.deepEqual(readdirSync(root), ["goal.md"]);
    assert.equal(readFileSync(path, "utf8"), original);
  } finally {
    chmodSync(root, 0o755);
    rmSync(root, { recursive: true, force: true });
  }
});
