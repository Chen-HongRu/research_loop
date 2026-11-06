import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
execFileSync("npm", ["run", "build"], { cwd: root, stdio: "inherit" });
const archive = join(root, "dist", `${pkg.name}-${pkg.version}.tgz`);
// A deployable application root includes its canonical lock. npm pack excludes it.
execFileSync(
  "tar",
  [
    "-czf",
    archive,
    "--exclude=*.tgz",
    "--exclude=__pycache__",
    "--exclude=*.pyc",
    "-C",
    root,
    "package.json",
    "package-lock.json",
    "README.md",
    "LICENSE",
    ...pkg.files,
  ],
  { stdio: "inherit" },
);
console.log(JSON.stringify({ archive, version: pkg.version }));
