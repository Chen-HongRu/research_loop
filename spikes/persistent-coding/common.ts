import { createHash, randomUUID } from "node:crypto";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

export interface RunConfig {
  id: string;
  candidate: "durable" | "dbos";
  workspace: string;
  provider: string;
  model: string;
  reasoning: "off" | "medium";
  deadline: number;
}

export const INITIAL_SOURCE =
  "def clamp(value, lower, upper):\n    return max(lower, value)\n";
export const WRONG_SOURCE =
  "def clamp(value, lower, upper):\n    return min(upper, max(lower, value))\n";
export const FIXED_SOURCE =
  "def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError('reversed bounds')\n    return min(upper, max(lower, value))\n";
const TESTS = `import unittest
from app import clamp

class ClampTests(unittest.TestCase):
    def test_bounds(self):
        for value, lower, upper, expected in [(-3, 0, 5, 0), (3, 0, 5, 3), (9, 0, 5, 5), (-2, -5, -1, -2), (3, 2, 2, 2)]:
            with self.subTest(value=value):
                self.assertEqual(clamp(value, lower, upper), expected)

    def test_reversed(self):
        with self.assertRaises(ValueError):
            clamp(2, 5, 0)

if __name__ == '__main__':
    unittest.main()
`;
export const TEST_COMMAND =
  "uv run --no-project --offline python -m unittest -v test_app.py";
export const PROMPT = `Read app.py and test_app.py. Fix clamp in app.py to satisfy the existing tests.
Use native read, edit or write, and bash tools; actually run ${TEST_COMMAND}, observe the output and correct failures.
Do not change tests. Work only in the supplied disposable workspace.
Return a concise summary after testing. The host will construct a structured result from independent verification, not from your success claims.`;

export function initRun(
  root: string,
  candidate: RunConfig["candidate"],
  options: Partial<
    Pick<RunConfig, "provider" | "model" | "deadline" | "reasoning">
  > = {},
): RunConfig {
  if (!["off", "medium"].includes(options.reasoning ?? "medium"))
    throw new Error("Supported reasoning settings: off, medium");
  root = resolve(root);
  mkdirSync(root, { recursive: false });
  const workspace = join(root, "workspace");
  mkdirSync(workspace);
  const config: RunConfig = {
    id: randomUUID(),
    candidate,
    workspace: realpathSync(workspace),
    provider: options.provider ?? "faux",
    model: options.model ?? "coding",
    reasoning: options.reasoning ?? "medium",
    deadline: options.deadline ?? Date.now() + 300_000,
  };
  writeFileSync(join(workspace, "app.py"), INITIAL_SOURCE);
  writeFileSync(join(workspace, "test_app.py"), TESTS);
  writeFileSync(join(workspace, ".spike-workspace"), config.id);
  writeFileSync(
    join(root, "run.json"),
    JSON.stringify(config, null, 2) + "\n",
    { flag: "wx" },
  );
  return config;
}

export function readRun(root: string): RunConfig {
  const config = JSON.parse(
    readFileSync(join(root, "run.json"), "utf8"),
  ) as RunConfig;
  if (
    !statSync(config.workspace).isDirectory() ||
    realpathSync(config.workspace) !== config.workspace ||
    readFileSync(join(config.workspace, ".spike-workspace"), "utf8") !==
      config.id
  ) {
    throw new Error(
      "Original workspace missing or replaced; inspect it manually. No new workspace was created.",
    );
  }
  return config;
}

export function checkDeadline(config: RunConfig): void {
  if (Date.now() >= config.deadline)
    throw new Error(
      "Original deadline expired; no new coding work is authorized.",
    );
  if (existsSync(join(config.workspace, "..", "STOP")))
    throw new Error("STOP is set; inspect/cancel existing work.");
}

export function trace(root: string, event: string, detail: object = {}): void {
  appendFileSync(
    join(root, "trace.jsonl"),
    JSON.stringify({ at: new Date().toISOString(), event, ...detail }) + "\n",
  );
}

export function verify(config: RunConfig) {
  const testsUnchanged =
    readFileSync(join(config.workspace, "test_app.py"), "utf8") === TESTS;
  const result = spawnSync(
    "uv",
    [
      "run",
      "--no-project",
      "--offline",
      "python",
      "-m",
      "unittest",
      "-v",
      "test_app.py",
    ],
    {
      cwd: config.workspace,
      encoding: "utf8",
      timeout: 15_000,
    },
  );
  return {
    passed: testsUnchanged && result.status === 0,
    exitCode: result.status,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? result.error?.message ?? "",
    sourceDigest: createHash("sha256")
      .update(readFileSync(join(config.workspace, "app.py")))
      .digest("hex"),
  };
}
