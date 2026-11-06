import { execFileSync } from "node:child_process";
import { accessSync, constants, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { localDaemon } from "@earendil-works/pi-env";
import { requireNativePlatform } from "./environment.ts";

export function doctor() {
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (major < 22 || (major === 22 && minor < 19))
    throw new Error("Node.js >= 22.19.0 is required");
  requireNativePlatform();
  const root = fileURLToPath(
    new URL(
      import.meta.url.endsWith(".ts") ? "../../" : "../",
      import.meta.url,
    ),
  );
  accessSync(join(root, "dist", "cli.js"), constants.R_OK);
  accessSync(localDaemon(), constants.X_OK);
  const locked = JSON.parse(
    readFileSync(join(root, "package-lock.json"), "utf8"),
  );
  const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
  const dependencies: Record<string, string> = {};
  for (const [name, version] of Object.entries(pkg.dependencies)) {
    const entry = locked.packages[`node_modules/${name}`];
    if (entry?.version !== version)
      throw new Error(`Locked dependency mismatch: ${name}`);
    let directory = dirname(fileURLToPath(import.meta.resolve(name)));
    while (true) {
      try {
        const installed = JSON.parse(
          readFileSync(join(directory, "package.json"), "utf8"),
        );
        if (installed.name === name) {
          if (installed.version !== version)
            throw new Error(`Installed dependency mismatch: ${name}`);
          dependencies[name] = installed.version;
          break;
        }
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
      if (dirname(directory) === directory)
        throw new Error(`Installed dependency metadata missing: ${name}`);
      directory = dirname(directory);
    }
  }
  const uv = execFileSync("uv", ["--version"], {
    encoding: "utf8",
    timeout: 10_000,
  }).trim();
  const python = execFileSync(
    "uv",
    ["python", "find", "--offline", "--no-config", "--no-cache"],
    {
      encoding: "utf8",
      timeout: 10_000,
    },
  ).trim();
  const pythonVersion = execFileSync(python, ["-I", "--version"], {
    encoding: "utf8",
    timeout: 10_000,
  }).trim();
  return {
    version: pkg.version,
    node: process.versions.node,
    platform: process.platform,
    architecture: process.arch,
    uv,
    python: pythonVersion,
    dependencies,
    compiledEntry: true,
    nativePrerequisites: true,
    isolationProbed: false,
    modelCalls: 0,
  };
}
