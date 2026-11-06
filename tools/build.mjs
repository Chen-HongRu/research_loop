import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const compile = (project, extra = []) =>
  execFileSync(join(root, "node_modules/.bin/tsc"), ["-p", project, ...extra], {
    cwd: root,
    stdio: "inherit",
  });
compile("runtime/tsconfig.build.json");
compile("integrations/cactus_remote/tsconfig.build.json");
if (process.argv.includes("--check")) {
  compile("runtime/tsconfig.json", ["--noEmit"]);
  compile("integrations/cactus_remote/tsconfig.json", ["--noEmit"]);
}
