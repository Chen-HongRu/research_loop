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
];
const tests = list("tests/");
const files = Object.fromEntries(
  [...runtime, ...tests, "loc.mjs"].map((path) => [path, count(path)]),
);
const pythonFiles = readdirSync(
  new URL("../benchmarks/cpu_regression/", import.meta.url),
).filter((name) => name.endsWith(".py"));
const python = JSON.parse(
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
for name in sys.argv[1:]:
    text=Path(name).read_text(); tree=ast.parse(text); docs=set(); lines=set()
    for node in ast.walk(tree):
        body=getattr(node,"body",None)
        if isinstance(body,list) and body and isinstance(body[0],ast.Expr) and isinstance(body[0].value,ast.Constant) and isinstance(body[0].value.value,str):
            docs.update(range(body[0].lineno,body[0].end_lineno+1))
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type not in (tokenize.COMMENT,tokenize.NL,tokenize.NEWLINE,tokenize.INDENT,tokenize.DEDENT,tokenize.ENDMARKER):
            lines.update(range(token.start[0],token.end[0]+1))
    result[Path(name).name]=len(lines-docs)
print(json.dumps(result))
`,
      ...pythonFiles.map((name) =>
        fileURLToPath(
          new URL("../benchmarks/cpu_regression/" + name, import.meta.url),
        ),
      ),
    ],
    { encoding: "utf8" },
  ),
);
console.log(
  JSON.stringify(
    {
      convention:
        "Nonblank token-bearing source lines, comments/docstrings excluded; prompts, schemas and platform glue included. Generated lockfiles/prose excluded. Task adapter and synthetic provider/tests separate.",
      files,
      core: runtime.reduce((sum, path) => sum + files[path], 0),
      syntheticAndTests: tests.reduce((sum, path) => sum + files[path], 0),
      taskAdapter: python,
      taskAdapterRuntime: Object.entries(python)
        .filter(([name]) => !name.startsWith("test_"))
        .reduce((sum, [, lines]) => sum + lines, 0),
      taskAdapterTests: Object.entries(python)
        .filter(([name]) => name.startsWith("test_"))
        .reduce((sum, [, lines]) => sum + lines, 0),
      reportingTool: files["loc.mjs"],
    },
    null,
    2,
  ),
);
