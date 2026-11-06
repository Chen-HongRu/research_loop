import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import {
  mkdtemp,
  mkdir,
  readFile,
  rename,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import {
  BACKGROUND_CONTEXT,
  withAbortSignal,
} from "@earendil-works/chord/context";
import type { ToolExecutionApi } from "@earendil-works/pi-durable";
import {
  createReadTool,
  createWriteTool,
  createEditTool,
  createBashTool,
} from "@earendil-works/pi-durable/tools";
import { createEnvironment } from "../src/environment.ts";

const native =
  process.platform === "darwin" &&
  process.env.RESEARCH_NATIVE_SANDBOX_TEST === "1";
const python = native
  ? (process.env.RESEARCH_TEST_PYTHON ??
    execFileSync("uv", ["python", "find", "--system"], {
      encoding: "utf8",
    }).trim())
  : "/unused/python";

test(
  "official daemon enforces native file, process and network boundaries",
  { skip: !native, timeout: 25_000 },
  async () => {
    const fixture = await mkdtemp(join(tmpdir(), "research-sandbox-test-"));
    const workspace = join(fixture, "candidate");
    const inputs = join(fixture, "public-inputs");
    const secret = join(fixture, "synthetic-secret");
    const evaluator = join(inputs, "canonical-evaluator");
    const checkpoint = join(inputs, "only-checkpoint");
    await mkdir(workspace);
    await mkdir(inputs);
    await writeFile(evaluator, "canonical fixture");
    await writeFile(checkpoint, "checkpoint fixture");
    await writeFile(secret, "synthetic label fixture");
    await symlink(secret, join(workspace, "escape"));
    process.env.RESEARCH_SYNTHETIC_PARENT_SECRET = "synthetic-parent-marker";
    const isolated = await createEnvironment({
      id: "native-fixture",
      workspace,
      readOnly: [inputs],
      python,
    });
    delete process.env.RESEARCH_SYNTHETIC_PARENT_SECRET;
    const { env } = isolated;
    const context = BACKGROUND_CONTEXT;
    async function exec(argv: string[] | string) {
      let output = "";
      const result = await env.exec(
        argv,
        {
          timeout: 5,
          onOutput: (text) => {
            output += text;
          },
        },
        context,
      );
      assert.ok(result.ok, result.ok ? "" : result.error.message);
      return { exitCode: result.value.exitCode, output };
    }
    try {
      // Only the Harness API is a small test adapter; the tools and all OS operations are official and real.
      let toolOutput = "";
      const api = {
        env,
        output: (text: string) => {
          toolOutput += text;
        },
        outputWindow: undefined,
        retainedOutput: () => ({ text: toolOutput, truncated: false }),
        diagnostic: () => {},
      } as unknown as ToolExecutionApi;
      await createWriteTool().execute(
        { path: "editable.txt", content: "before\n" },
        api,
        context,
      );
      await createEditTool().execute(
        {
          path: "editable.txt",
          edits: [{ oldText: "before", newText: "after" }],
        },
        api,
        context,
      );
      const read = await createReadTool().execute(
        { path: "editable.txt" },
        api,
        context,
      );
      assert.match(JSON.stringify(read), /after/);
      const bash = await createBashTool().execute(
        { command: "printf bash-ok" },
        api,
        context,
      );
      assert.match(JSON.stringify(bash), /bash-ok/);
      assert.equal(
        await readFile(join(workspace, "editable.txt"), "utf8"),
        "after\n",
      );
      assert.equal((await env.readTextFile(evaluator, context)).ok, true);
      const control = dirname(dirname((await env.connection.info()).home));
      assert.equal(
        (await env.writeFile(join(control, "policy.sb"), "changed", context))
          .ok,
        false,
      );
      for (const path of [evaluator, checkpoint]) {
        assert.equal((await env.writeFile(path, "changed", context)).ok, false);
        assert.equal((await env.remove(path, undefined, context)).ok, false);
        assert.equal(
          (await env.renameFile(path, join(workspace, "stolen"), context)).ok,
          false,
        );
        const chmod = await exec(["/bin/chmod", "600", path]);
        assert.notEqual(chmod.exitCode, 0);
      }
      assert.equal((await env.readTextFile(secret, context)).ok, false);
      assert.equal((await env.readTextFile("escape", context)).ok, false);
      assert.equal(
        (await env.writeFile("escape", "changed", context)).ok,
        false,
      );
      assert.notEqual((await exec(["/bin/cat", secret])).exitCode, 0);
      assert.notEqual((await exec(["/bin/cat", "escape"])).exitCode, 0);
      const clean = await exec([
        python,
        "-c",
        "import os; assert 'RESEARCH_SYNTHETIC_PARENT_SECRET' not in os.environ; print('clean')",
      ]);
      assert.equal(clean.output.trim(), "clean");
      const metadata = await exec([
        python,
        "-c",
        `import ctypes,errno; c=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True); mib=(ctypes.c_int*3)(1,49,${process.pid}); buf=ctypes.create_string_buffer(65536); n=ctypes.c_size_t(len(buf)); r=c.sysctl(mib,3,buf,ctypes.byref(n),None,0); assert r == -1, 'procargs unexpectedly readable'; print('blocked:' + str(ctypes.get_errno()))`,
      ]);
      assert.match(metadata.output.trim(), /^blocked:/);
      const network = await exec([
        python,
        "-c",
        "import socket,errno; s=socket.socket(); r=s.connect_ex(('127.0.0.1',9)); assert r == errno.EPERM; print('blocked')",
      ]);
      assert.equal(network.output.trim(), "blocked");
      const sentinel = spawn("/bin/sleep", ["30"], { stdio: "ignore" });
      try {
        const signal = await exec([
          python,
          "-c",
          `import os,errno;\ntry: os.kill(${sentinel.pid},15)\nexcept PermissionError: print('blocked')\nelse: raise AssertionError('external signal permitted')`,
        ]);
        assert.equal(signal.output.trim(), "blocked");
        assert.equal(sentinel.exitCode, null);
      } finally {
        sentinel.kill();
      }
      const uv = await exec([
        "uv",
        "run",
        "--no-project",
        "--python",
        python,
        python,
        "-c",
        "import math; assert math.sqrt(4)==2; print('uv-ok')",
      ]);
      assert.equal(uv.output.trim(), "uv-ok");
      const live = join(inputs, "Knowledge.md");
      await writeFile(live, "whole-old\n");
      assert.deepEqual(await env.readTextFile(live, context), {
        ok: true,
        value: "whole-old\n",
      });
      await writeFile(join(inputs, "replacement"), "whole-new\n");
      await rename(join(inputs, "replacement"), live);
      assert.deepEqual(await env.readTextFile(live, context), {
        ok: true,
        value: "whole-new\n",
      });
      const controller = new AbortController();
      const cancelled = env.exec(
        "(/bin/sleep 1; printf escaped > cancelled-child) & wait",
        undefined,
        withAbortSignal(controller.signal, context),
      );
      await delay(100);
      controller.abort();
      assert.equal((await cancelled).ok, false);
      await delay(1100);
      assert.deepEqual(await env.exists("cancelled-child", context), {
        ok: true,
        value: false,
      });
      const pending = env.exec(
        "(/bin/sleep 1; printf escaped > closed-child) & wait",
        undefined,
        context,
      );
      await delay(100);
      const daemonPid = (await env.connection.info()).pid;
      const other = await createEnvironment({
        id: "other-daemon",
        workspace,
        readOnly: [inputs],
        python,
      });
      await isolated.close();
      try {
        const unaffected = await other.env.exec(
          ["/usr/bin/true"],
          undefined,
          context,
        );
        assert.deepEqual(unaffected, { ok: true, value: { exitCode: 0 } });
      } finally {
        await other.close();
      }
      assert.equal((await pending).ok, false);
      await delay(1100);
      await assert.rejects(readFile(join(workspace, "closed-child")), {
        code: "ENOENT",
      });
      assert.throws(() => process.kill(daemonPid, 0), { code: "ESRCH" });
      assert.equal(await readFile(evaluator, "utf8"), "canonical fixture");
      assert.equal(await readFile(checkpoint, "utf8"), "checkpoint fixture");
      assert.equal(await readFile(secret, "utf8"), "synthetic label fixture");
    } finally {
      await isolated.close();
      await rm(fixture, { recursive: true, force: true });
    }
  },
);

test(
  "rejects write grants that cover protected inputs and non-directory workspaces",
  { skip: !native },
  async () => {
    const fixture = await mkdtemp(join(tmpdir(), "research-overlap-test-"));
    const input = join(fixture, "input");
    await writeFile(input, "fixture");
    try {
      await assert.rejects(
        createEnvironment({
          id: "overlap",
          workspace: fixture,
          readOnly: [input],
          python,
        }),
        /overlap/,
      );
      await assert.rejects(
        createEnvironment({
          id: "file",
          workspace: input,
          readOnly: [],
          python,
        }),
        /not a directory/,
      );
      await assert.rejects(
        createEnvironment({
          id: "root",
          workspace: fixture,
          readOnly: ["/"],
          python,
        }),
        /overlap/,
      );
    } finally {
      await rm(fixture, { recursive: true, force: true });
    }
  },
);

test(
  "prediction snapshot remains read-only while output directory is writable",
  { skip: !native },
  async () => {
    const fixture = await mkdtemp(join(tmpdir(), "research-prediction-test-"));
    const snapshot = join(fixture, "snapshot");
    const output = join(fixture, "predictions");
    await mkdir(snapshot);
    await mkdir(output);
    await writeFile(join(snapshot, "candidate"), "fixture");
    const isolated = await createEnvironment({
      id: "prediction",
      workspace: snapshot,
      readOnly: [snapshot],
      writable: [output],
      python,
    });
    try {
      assert.equal(
        (
          await isolated.env.writeFile(
            "candidate",
            "changed",
            BACKGROUND_CONTEXT,
          )
        ).ok,
        false,
      );
      assert.equal(
        (
          await isolated.env.writeFile(
            join(output, "result"),
            "prediction",
            BACKGROUND_CONTEXT,
          )
        ).ok,
        true,
      );
    } finally {
      await isolated.close();
      await rm(fixture, { recursive: true, force: true });
    }
  },
);
