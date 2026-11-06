import ts from "typescript";
import { readFileSync, readdirSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

function count(path) {
  const text = readFileSync(new URL(path, import.meta.url), "utf8");
  const source = ts.createSourceFile(
    path,
    text,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith(".json")
      ? ts.ScriptKind.JSON
      : path.endsWith(".ts")
        ? ts.ScriptKind.TS
        : ts.ScriptKind.JS,
  );
  const lines = text.split(/\r?\n/),
    covered = new Set();
  function visit(node) {
    const children = node.getChildren(source);
    if (children.length) {
      children.forEach(visit);
      return;
    }
    if (node.kind === ts.SyntaxKind.EndOfFileToken || !node.getWidth(source))
      return;
    const from = source.getLineAndCharacterOfPosition(
      node.getStart(source),
    ).line;
    const to = source.getLineAndCharacterOfPosition(node.end - 1).line;
    for (let line = from; line <= to; line++)
      if (lines[line].trim()) covered.add(line);
  }
  visit(source);
  return covered.size;
}
const list = (folder) =>
  readdirSync(new URL(folder, import.meta.url))
    .filter((name) => name.endsWith(".ts"))
    .map((name) => folder + name);
const runtime = [
  ...list("src/"),
  "../package.json",
  "tsconfig.json",
  "tsconfig.build.json",
  "../tools/package.mjs",
  "../tools/build.mjs",
];
const tests = list("tests/");
const integration = "../integrations/cactus_remote/";
const platform = [
  "backend.ts",
  "cli.ts",
  "tsconfig.json",
  "tsconfig.build.json",
].map((name) => integration + name);
const remoteTask = [integration + "prepare.ts"];
const remoteTests = [integration + "backend.test.ts"];
const files = Object.fromEntries(
  [
    ...runtime,
    ...tests,
    ...platform,
    ...remoteTask,
    ...remoteTests,
    "loc.mjs",
  ].map((path) => [path, count(path)]),
);
const pythonFiles = [
  "../benchmarks/cpu_regression/",
  "../benchmarks/cactus/",
  integration,
].flatMap((folder) =>
  readdirSync(new URL(folder, import.meta.url))
    .filter((name) => name.endsWith(".py"))
    .map((name) => folder + name),
);
const allPython = JSON.parse(
  execFileSync(
    "uv",
    [
      "run",
      "--no-project",
      "--offline",
      "python",
      "-c",
      `
import ast,io,json,tokenize,sys
from pathlib import Path
result={}
for name in sys.argv[2:]:
    text=Path(name).read_text(); tree=ast.parse(text); docs=set(); lines=set()
    for node in ast.walk(tree):
        body=getattr(node,"body",None)
        if isinstance(body,list) and body and isinstance(body[0],ast.Expr) and isinstance(body[0].value,ast.Constant) and isinstance(body[0].value.value,str):
            docs.update(range(body[0].lineno,body[0].end_lineno+1))
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type not in (tokenize.COMMENT,tokenize.NL,tokenize.NEWLINE,tokenize.INDENT,tokenize.DEDENT,tokenize.ENDMARKER):
            lines.update(range(token.start[0],token.end[0]+1))
    result[str(Path(name).relative_to(sys.argv[1]))]=len(lines-docs)
print(json.dumps(result))
`,
      fileURLToPath(new URL("../", import.meta.url)),
      ...pythonFiles.map((path) =>
        fileURLToPath(new URL(path, import.meta.url)),
      ),
    ],
    { encoding: "utf8" },
  ),
);
const python = Object.fromEntries(
  Object.entries(allPython)
    .filter(([path]) => path.startsWith("benchmarks/cpu_regression/"))
    .map(([path, lines]) => [path.split("/").at(-1), lines]),
);
const sum = (paths) => paths.reduce((total, path) => total + files[path], 0);
const core = sum(runtime);
const remotePlatform =
  sum(platform) + allPython["integrations/cactus_remote/remote_runner.py"];
const cactus = Object.fromEntries(
  Object.entries(allPython).filter(([path]) =>
    path.startsWith("benchmarks/cactus/"),
  ),
);
const pythonTest = (path) =>
  path.split("/").at(-1).startsWith("test_") || path.endsWith("_test.py");
console.log(
  JSON.stringify(
    {
      convention:
        "Nonblank token-bearing source lines, comments/docstrings excluded; prompts, schemas and platform glue included. Generated lockfiles/prose excluded. Task adapter and synthetic provider/tests separate.",
      files,
      core,
      productionRuntimeAndPlatform: core + remotePlatform,
      syntheticAndTests: tests.reduce((sum, path) => sum + files[path], 0),
      taskAdapter: python,
      taskAdapterRuntime: Object.entries(python)
        .filter(([name]) => !name.startsWith("test_"))
        .reduce((sum, [, lines]) => sum + lines, 0),
      taskAdapterTests: Object.entries(python)
        .filter(([name]) => name.startsWith("test_"))
        .reduce((sum, [, lines]) => sum + lines, 0),
      cactusRemote: {
        platformRuntime: remotePlatform,
        taskRuntime:
          sum(remoteTask) +
          Object.entries(cactus)
            .filter(([path]) => !pythonTest(path))
            .reduce((total, [, lines]) => total + lines, 0),
        tests:
          sum(remoteTests) +
          allPython["integrations/cactus_remote/remote_runner_test.py"] +
          Object.entries(cactus)
            .filter(([path]) => pythonTest(path))
            .reduce((total, [, lines]) => total + lines, 0),
        pythonFiles: Object.fromEntries(
          Object.entries(allPython).filter(
            ([path]) => !path.startsWith("benchmarks/cpu_regression/"),
          ),
        ),
      },
      reportingTool: files["loc.mjs"],
    },
    null,
    2,
  ),
);
