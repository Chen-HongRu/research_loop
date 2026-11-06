import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createModels } from "@earendil-works/pi-ai/models";
import { getCurrentTools } from "@earendil-works/pi-ai/utils/transcript";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai/providers/faux";
import {
  assembleContext,
  contextAccess,
  previewContext,
  roleDefinition,
  validateContextConfig,
  type RoleContext,
} from "../src/context.ts";
import type { StudyConfig } from "../src/contract.ts";
import { initialize, readRecord } from "../src/files.ts";
import { runStudy, studyContext, studyStatus } from "../src/engine.ts";

const config: StudyConfig = {
  version: 1,
  id: "synthetic-context",
  goal: "Measure a fixed baseline",
  constraints: ["Keep evaluation fixed"],
  model: { provider: "faux", id: "workflow", thinking: "off" },
  authorization: {
    id: "synthetic-permit",
    expiresAt: Date.now() + 900_000,
    slots: ["slot1"],
    jobSeconds: 30,
  },
  maxRounds: 1,
  tasks: [
    {
      id: "baseline",
      source: "human",
      question: "Measure baseline",
      constraints: [],
      basedOn: [],
    },
  ],
  source: { root: "/synthetic/source", files: ["candidate.py"] },
  inputs: {
    root: "/synthetic/inputs",
    version: "input-v1",
    feedback: "development",
  },
  evaluation: {
    id: "evaluation",
    version: "v1",
    feedback: "development",
    predictCommand: ["/private/predict"],
    command: ["/private/score"],
  },
  python: "/synthetic/python",
};

test("configuration changes tools and named paths without changing required inputs or old assemblies", () => {
  const original = structuredClone(config);
  const request = {
    role: "plan" as const,
    workspace: "/synthetic/workspace",
    records: [],
    task: config.tasks[0],
  };
  const first = assembleContext("/synthetic/study", original, request);
  const old = structuredClone(first);
  original.context = {
    mounts: { literature: "/synthetic/literature" },
    roles: {
      plan: {
        tools: ["bash", "submit_plan"],
        readOnly: ["workspace", "literature"],
        instructions: "Cite the supplied literature.",
      },
    },
  };
  const second = assembleContext("/synthetic/study", original, request);
  assert.deepEqual(second.tools, ["bash", "submit_plan"]);
  assert.deepEqual(contextAccess(second), {
    readOnly: [request.workspace, "/synthetic/literature"],
    writable: [],
  });
  assert.match(second.instructions, /Cite the supplied literature/);
  assert.doesNotMatch(second.instructions, /Published knowledge is at/);
  assert.equal(second.inputs.goal, config.goal);
  assert.deepEqual(
    Object.keys(second.inputs),
    roleDefinition(original, "plan").inputs,
  );
  assert.deepEqual(second.output, first.output);
  assert.deepEqual(first, old);
  assert.equal("prompt" in previewContext(second), false);
  assert.equal("instructions" in previewContext(second), false);
  assert.deepEqual(previewContext(second, true), second);
  assert.doesNotMatch(
    JSON.stringify(second),
    /\/private\/score|\/private\/predict/,
  );
});

test("invalid policies fail with the exact role or configuration field", () => {
  for (const [value, pattern] of [
    [null, /context/],
    [{ roles: null }, /roles/],
    [{ mounts: null }, /mounts/],
    [{ roles: { scout: {} } }, /roles.scout/],
    [{ roles: { plan: { inputs: [] } } }, /roles.plan.inputs/],
    [
      { roles: { plan: { tools: ["read", "unknown", "submit_plan"] } } },
      /roles.plan.tools/,
    ],
    [{ roles: { plan: { tools: ["read"] } } }, /required tool submit_plan/],
    [
      { roles: { execute: { tools: ["finish_execute"] } } },
      /required tool run_experiment/,
    ],
    [
      {
        roles: {
          review: { tools: ["read", "run_experiment", "submit_review"] },
        },
      },
      /roles.review.tools/,
    ],
    [{ roles: { plan: { writable: ["workspace"] } } }, /roles.plan.writable/],
    [
      { roles: { execute: { writable: ["inputs"] } } },
      /roles.execute.writable/,
    ],
    [
      { roles: { plan: { readOnly: ["workspace", "unknown"] } } },
      /roles.plan.readOnly/,
    ],
    [{ roles: { plan: { readOnly: ["knowledge"] } } }, /working directory/],
    [{ roles: { review: { readOnly: ["workspace"] } } }, /Review requires/],
    [{ mounts: { workspace: "/synthetic/other" } }, /mounts.workspace/],
    [{ mounts: { literature: "relative/path" } }, /mounts.literature/],
    ...["/", "/.", "/tmp/..", "//"].map((path) => [
      { mounts: { literature: path } },
      /mounts.literature/,
    ]),
  ] as const)
    assert.throws(() => validateContextConfig(value), pattern);
});

test("initialization canonicalizes mount identity and rejects a root symlink before publication", () => {
  const parent = mkdtempSync(join(tmpdir(), "context-init-"));
  try {
    const mounts = join(parent, "mounts");
    mkdirSync(mounts);
    const alias = join(parent, "alias");
    symlinkSync(mounts, alias);
    const rootAlias = join(parent, "root-alias");
    symlinkSync("/", rootAlias);
    const current = {
      ...structuredClone(config),
      source: { root: mounts, files: ["candidate.py"] },
      inputs: { ...config.inputs, root: mounts },
      python: process.execPath,
      context: { mounts: { literature: rootAlias } },
    };
    const refused = join(parent, "refused");
    assert.throws(() => initialize(refused, current), /mounts.literature/);
    assert.equal(existsSync(refused), false);
    current.context.mounts.literature = alias;
    const root = initialize(join(parent, "accepted"), current);
    const saved = JSON.parse(readFileSync(join(root, "study.json"), "utf8"));
    assert.equal(
      saved.context.mounts.literature,
      current.context.mounts.literature,
    );
    assert.equal(saved.context.mounts.literature.endsWith("/mounts"), true);
  } finally {
    rmSync(parent, { recursive: true, force: true });
  }
});

test("read-only context preview works before any runtime database exists", async () => {
  const parent = mkdtempSync(join(tmpdir(), "context-preview-"));
  try {
    const current = {
      ...structuredClone(config),
      source: { root: parent, files: ["candidate.py"] },
      inputs: { ...config.inputs, root: parent },
      python: process.execPath,
    };
    const root = initialize(join(parent, "study"), current);
    const preview = await studyContext(root);
    assert.ok("roles" in preview);
    assert.equal(existsSync(join(root, "runtime.sqlite")), false);
    const cli = fileURLToPath(new URL("../src/cli.ts", import.meta.url));
    const output = spawnSync(
      process.execPath,
      ["--experimental-strip-types", cli, "context", root],
      { encoding: "utf8" },
    );
    assert.equal(output.status, 0, output.stderr);
    assert.deepEqual(JSON.parse(output.stdout), preview);
    assert.equal(existsSync(join(root, "runtime.sqlite")), false);
  } finally {
    rmSync(parent, { recursive: true, force: true });
  }
});

test(
  "configured context drives actual tools, native path denial and persisted call preview",
  { skip: process.env.RESEARCH_NATIVE_SANDBOX_TEST !== "1", timeout: 30_000 },
  async () => {
    const parent = mkdtempSync(join(tmpdir(), "context-native-"));
    try {
      const prepared = join(parent, "fixture");
      const output = spawnSync(
        "uv",
        [
          "run",
          "--no-project",
          "--offline",
          "python",
          fileURLToPath(
            new URL(
              "../../benchmarks/cpu_regression/prepare.py",
              import.meta.url,
            ),
          ),
          prepared,
          "--provider",
          "faux",
          "--model",
          "workflow",
        ],
        { encoding: "utf8" },
      );
      assert.equal(output.status, 0, output.stderr);
      const current = JSON.parse(
        readFileSync(join(prepared, "study.json"), "utf8"),
      ) as StudyConfig;
      current.maxRounds = 1;
      current.minimumRounds = 1;
      current.model.thinking = "off";
      const library = join(parent, "library");
      mkdirSync(library);
      writeFileSync(join(library, "allowed.txt"), "SYNTHETIC_PUBLIC_REFERENCE");
      const secret = join(parent, "not-granted.txt");
      writeFileSync(secret, "SYNTHETIC_PRIVATE_REFERENCE");
      symlinkSync(secret, join(library, "escape"));
      current.context = {
        mounts: { literature: library },
        roles: {
          plan: {
            tools: ["bash", "submit_plan"],
            readOnly: ["workspace", "literature"],
          },
        },
      };
      const root = initialize(join(parent, "study"), current);
      const faux = fauxProvider({
        models: [{ id: "workflow", reasoning: false }],
      });
      const models = createModels();
      models.setProvider(faux.provider);
      let captured: RoleContext | undefined;
      let planId: number | undefined;
      const respond: FauxResponseFactory = async (ctx) => {
        const user = ctx.messages.find((message) => message.role === "user");
        const text =
          user?.role === "user" && typeof user.content === "string"
            ? user.content
            : "";
        const feedback = ctx.messages.filter(
          (message) => message.role === "toolResult",
        );
        const turn = feedback.length;
        const call = (name: string, args: Parameters<typeof fauxToolCall>[1]) =>
          fauxAssistantMessage(fauxToolCall(name, args), {
            stopReason: "toolUse",
          });
        if (text.startsWith("Goal:") && text.includes("\nHuman constraints:")) {
          if (turn === 0) {
            planId = (await studyStatus(root)).state!.investigations[0]
              .planner!;
            captured = (await studyContext(root, planId, true)) as RoleContext;
            assert.equal(text, captured.prompt);
            assert.deepEqual(
              getCurrentTools(ctx.messages).map((tool) => tool.name),
              captured.tools,
            );
            assert.ok(
              ctx.messages.some(
                (message) =>
                  message.role === "system" &&
                  message.sections?.instructions?.includes(
                    captured!.instructions,
                  ),
              ),
            );
            return call("read", { path: join(library, "allowed.txt") });
          }
          if (turn === 1) {
            assert.equal(feedback.at(-1)?.isError, true);
            return call("bash", {
              command: `cat '${join(library, "allowed.txt")}'`,
            });
          }
          if (turn === 2) {
            assert.match(
              JSON.stringify(feedback.at(-1)?.content),
              /SYNTHETIC_PUBLIC_REFERENCE/,
            );
            return call("bash", {
              command: `cat '${join(library, "escape")}'`,
            });
          }
          if (turn === 3) {
            assert.equal(feedback.at(-1)?.isError, true);
            return call("bash", { command: "printf changed > forbidden.txt" });
          }
          if (turn === 4) assert.equal(feedback.at(-1)?.isError, true);
          return call("submit_plan", {
            question: "Measure baseline",
            change: "Measure the supplied baseline without edits",
            rationale: "Establish a measured reference",
            basedOn: [],
          });
        }
        if (text.includes("\nImmutable human constraints:"))
          return turn === 0
            ? call("run_experiment", {})
            : call("finish_execute", {
                summary: "Measured the unchanged baseline",
              });
        if (text.startsWith("You are an independent fresh Review")) {
          if (turn === 0) {
            const receipts = JSON.parse(
              text
                .split("\nTrusted receipts: ")[1]
                .split("\nFixed evaluation:")[0],
            );
            return call("read", { path: receipts[0].rawResult });
          }
          assert.equal(feedback.at(-1)?.isError, false);
          return call("submit_review", {
            interpretation: "Trusted baseline measurement",
            limitations: ["Synthetic development data"],
            knowledgeChanged: true,
            goal: "not-achieved",
            continue: false,
            nextQuestion: "Compare a new candidate",
          });
        }
        if (turn === 0)
          return call("write", {
            path: "research.md",
            content:
              "# Reviewed baseline\n\nSynthetic development result only.\n",
          });
        return call("publish_notes", {});
      };
      faux.setResponses(Array.from({ length: 30 }, () => respond));
      const outcome = await runStudy(root, { models });
      assert.equal(outcome.status, "completed", JSON.stringify(outcome));
      assert.ok(captured && planId);
      assert.equal(
        existsSync(join(captured.workspace, "forbidden.txt")),
        false,
      );
      const record = readRecord(root, "r0001");
      assert.equal(record.receipts[0].valid, true);
      assert.notEqual(record.conversations.review, planId);
      assert.deepEqual(await studyContext(root, planId, true), captured);
      const before = faux.state.callCount;
      const edited = JSON.parse(readFileSync(join(root, "study.json"), "utf8"));
      edited.context.roles.plan.tools = ["read", "submit_plan"];
      writeFileSync(join(root, "study.json"), JSON.stringify(edited));
      assert.deepEqual(await studyContext(root, planId, true), captured);
      await assert.rejects(
        runStudy(root, { models }),
        /Original Study contract changed/,
      );
      assert.equal(faux.state.callCount, before);
    } finally {
      rmSync(parent, { recursive: true, force: true });
    }
  },
);
