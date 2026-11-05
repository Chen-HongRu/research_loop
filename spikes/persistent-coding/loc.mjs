// Effective source lines, including schemas, prompts and inline fixture programs.
import ts from "typescript";
import { readFileSync, readdirSync } from "node:fs";

function count(path) {
  const text = readFileSync(new URL(path, import.meta.url), "utf8");
  const source = ts.createSourceFile(
    path,
    text,
    ts.ScriptTarget.Latest,
    true,
    path.endsWith(".json")
      ? ts.ScriptKind.JSON
      : path.endsWith(".mjs")
        ? ts.ScriptKind.JS
        : ts.ScriptKind.TS,
  );
  const lines = text.split(/\r?\n/);
  const covered = new Set();
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

const shared = ["common.ts", "provider.ts", "package.json", "tsconfig.json"];
const tests = [
  "synthetic.ts",
  ...readdirSync(new URL("tests/", import.meta.url))
    .filter((name) => name.endsWith(".ts"))
    .map((name) => "tests/" + name),
];
const files = Object.fromEntries(
  [...shared, "durable.ts", "dbos.ts", ...tests, "loc.mjs"].map((path) => [
    path,
    count(path),
  ]),
);
console.log(
  JSON.stringify(
    {
      convention:
        "Nonblank lines covered by parser tokens, excluding comments; schemas, prompts and inline programs included. Generated lockfile and prose excluded. Each candidate includes all shared runtime/config lines. Synthetic provider/tests and reporting tool separate.",
      files,
      runtime: Object.fromEntries(
        ["durable", "dbos"].map((candidate) => [
          candidate,
          [...shared, candidate + ".ts"].reduce(
            (sum, path) => sum + files[path],
            0,
          ),
        ]),
      ),
      syntheticAndTests: tests.reduce((sum, path) => sum + files[path], 0),
      reportingTool: files["loc.mjs"],
    },
    null,
    2,
  ),
);
