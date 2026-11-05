import { execFile } from "node:child_process";
import { accessSync, constants } from "node:fs";
import {
  access,
  mkdir,
  mkdtemp,
  realpath,
  rm,
  stat,
  writeFile,
  lstat,
  readlink,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve, sep } from "node:path";
import { promisify } from "node:util";
import {
  Connection,
  localDaemon,
  RemoteExecutionEnv,
} from "@earendil-works/pi-env";

export interface EnvironmentOptions {
  id: string;
  workspace: string;
  readOnly: string[];
  writable?: string[];
  python: string;
}

const run = promisify(execFile);
export function requireNativePlatform() {
  if (process.platform !== "darwin")
    throw new Error(
      "Native research isolation currently requires macOS sandbox-exec",
    );
  accessSync("/usr/bin/sandbox-exec", constants.X_OK);
}
const within = (path: string, root: string) =>
  root === sep || path === root || path.startsWith(root + sep);
const overlaps = (a: string, b: string) => within(a, b) || within(b, a);
const quoted = (path: string) => JSON.stringify(path);
async function existing(path: string) {
  return realpath(resolve(path));
}
async function directory(path: string) {
  const canonical = await existing(path);
  if (!(await stat(canonical)).isDirectory())
    throw new Error("Sandbox directory is not a directory");
  return canonical;
}
async function runtimeLinks(path: string): Promise<string[]> {
  const links: string[] = [];
  let current: string = sep;
  for (const part of resolve(path).split(sep).filter(Boolean)) {
    current = join(current, part);
    if ((await lstat(current)).isSymbolicLink()) {
      links.push(current);
      links.push(
        ...(await runtimeLinks(
          resolve(dirname(current), await readlink(current)),
        )),
      );
      current = await realpath(current);
    }
  }
  return links;
}
async function findUv() {
  for (const entry of (process.env.PATH ?? "").split(delimiter)) {
    if (!entry) continue;
    try {
      const candidate = join(entry, "uv");
      await access(candidate, 1);
      return await existing(candidate);
    } catch {
      /* Try the next PATH entry. */
    }
  }
  throw new Error("uv is required for the isolated Python environment");
}

/** Every environment operation is performed by the sandboxed official daemon. */
export async function createEnvironment(options: EnvironmentOptions): Promise<{
  env: RemoteExecutionEnv;
  close(): Promise<void>;
}> {
  requireNativePlatform();
  const workspace = await directory(options.workspace);
  const writable = await Promise.all(
    (options.writable ?? [workspace]).map(directory),
  );
  const readOnly = await Promise.all(options.readOnly.map(existing));
  const python = await existing(options.python);
  const uv = await findUv();
  const daemon = await existing(localDaemon());
  // Probe only the configured interpreter; isolated mode never imports candidate code.
  const { stdout } = await run(
    python,
    [
      "-I",
      "-c",
      'import json,sys,sysconfig; print(json.dumps([sys.base_prefix, sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]))',
    ],
    {
      env: { PATH: "/usr/bin:/bin", PYTHONDONTWRITEBYTECODE: "1" },
      timeout: 10_000,
      maxBuffer: 4096,
    },
  );
  const installation = JSON.parse(stdout) as string[];
  const pythonRoot = await directory(installation[0]);
  const pythonLibraries: string[] = [];
  for (const library of new Set(installation.slice(1))) {
    try {
      pythonLibraries.push(await directory(library));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  const pythonLinks = [
    ...(await runtimeLinks(options.python)),
    ...(await runtimeLinks(installation[0])),
  ];
  if (pythonRoot === sep)
    throw new Error("Python runtime must have a bounded installation prefix");
  const protectedPaths = [
    ...readOnly,
    pythonRoot,
    ...pythonLibraries,
    python,
    uv,
    daemon,
    "/System",
    "/usr",
    "/bin",
    "/sbin",
  ];
  if (
    writable.some((write) =>
      protectedPaths.some((read) => overlaps(write, read)),
    )
  ) {
    throw new Error(
      "Writable sandbox paths overlap a protected read-only path",
    );
  }
  const control = await realpath(
    await mkdtemp(join(tmpdir(), "research-env-")),
  );
  let connection: Connection | undefined;
  try {
    const scratch = join(control, "scratch");
    const home = join(scratch, "home");
    const temp = join(scratch, "tmp");
    await mkdir(home, { recursive: true, mode: 0o700 });
    await mkdir(temp, { mode: 0o700 });
    if (writable.some((write) => overlaps(write, control)))
      throw new Error("Writable sandbox paths overlap sandbox control files");
    const readable = [
      workspace,
      ...readOnly,
      ...writable,
      pythonRoot,
      ...pythonLibraries,
      python,
      uv,
      daemon,
    ];
    const grants = await Promise.all(
      readable.map(
        async (path) =>
          `(${(await stat(path)).isDirectory() ? "subpath" : "literal"} ${quoted(path)})`,
      ),
    );
    // /System/Volumes/Data aliases host user data, so never grant the whole /System tree.
    const profile = `(version 1)
(deny default)
(deny process-info* sysctl-read network* signal)
(allow syscall* mach-bootstrap)
(import "dyld-support.sb")
(allow process-fork process-exec)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read (sysctl-name "hw.pagesize" "hw.pagesize32" "vm.pagesize" "hw.ncpu" "hw.activecpu" "hw.logicalcpu" "hw.physicalcpu" "hw.memsize" "kern.osrelease" "kern.ostype" "kern.osversion" "kern.argmax" "kern.boottime" "hw.machine" "kern.hostname" "kern.version" "kern.usrstack64" "kern.usrstack" "hw.pagesize_compat"))
(allow file-read* file-map-executable
  (subpath "/System/Library") (subpath "/usr/lib") (subpath "/usr/share")
  (subpath "/bin") (subpath "/usr/bin") (subpath "/sbin") (subpath "/usr/sbin")
  (literal "/private/var/select/sh") ${grants.join(" ")})
(allow file-read-metadata (literal "/var") (literal "/tmp") (literal "/etc") ${[...new Set([...readable, ...pythonLinks, scratch])].map((path) => `(path-ancestors ${quoted(path)}) (literal ${quoted(path)})`).join(" ")})
(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))
(allow file-read* file-write-data (literal "/dev/null") (literal "/dev/zero"))
(allow file-read* file-write* ${[...writable, scratch].map((path) => `(subpath ${quoted(path)})`).join(" ")})
`;
    const profilePath = join(control, "policy.sb");
    await writeFile(profilePath, profile, { mode: 0o600 });
    const safeEnv = {
      PATH: [
        ...new Set([
          dirname(python),
          dirname(uv),
          "/usr/bin",
          "/bin",
          "/usr/sbin",
          "/sbin",
        ]),
      ].join(":"),
      HOME: home,
      TMPDIR: temp,
      LANG: "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONNOUSERSITE: "1",
      UV_OFFLINE: "1",
      UV_NO_CACHE: "1",
      UV_PYTHON: python,
      UV_PYTHON_DOWNLOADS: "never",
      UV_NO_CONFIG: "1",
    };
    connection = new Connection({
      command: [
        "/usr/bin/env",
        "-i",
        ...Object.entries(safeEnv).map(([key, value]) => `${key}=${value}`),
        "/usr/bin/sandbox-exec",
        "-f",
        profilePath,
        daemon,
      ],
    });
    await connection.info();
    const env = new RemoteExecutionEnv({
      id: options.id,
      cwd: workspace,
      connection,
      shellPath: "/bin/bash",
    });
    let closed = false;
    return {
      env,
      async close() {
        if (closed) return;
        closed = true;
        // Official daemon owns and terminates its exec process groups on disconnect.
        connection!.close();
        await rm(control, { recursive: true, force: true });
      },
    };
  } catch (error) {
    connection?.close();
    await rm(control, { recursive: true, force: true });
    throw error;
  }
}
