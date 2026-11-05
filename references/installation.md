# Installation

Install the complete repository at the intended revision, including root package files, `runtime/`, `benchmarks/`, references and docs. Do not install only `SKILL.md`. Verify the selected revision and materialized files. From the installation root:

```sh
npm ci
npm run build
node dist/cli.js doctor
```

For an application deployment archive, run `npm run pack` in the source root. It builds `dist/research-loop-0.2.0.tgz` containing compiled JavaScript, the root `package-lock.json` and the necessary documentation and benchmarks. Extract it into a new directory, then run:

```sh
npm ci --omit=dev
node dist/cli.js doctor
```

The extracted application does not need TypeScript compilation. This is an application-root archive, not a registry package: do not use `npm install` on the archive or `npm pack` for this deployment. npm packages exclude `package-lock.json`, so that route does not preserve this transitive dependency lock. See [npm's lockfile documentation](https://docs.npmjs.com/cli/v12/configuring-npm/package-lock-json/#npm-shrinkwrapjson).

Node.js >= 22.19, Python, uv and macOS `sandbox-exec` are required for the supported native path. The doctor checks prerequisites, compiled entry and locked dependency metadata without a model call; it does not prove sandbox isolation. Run the flagged native tests or an authorized smoke for that evidence. Local macOS arm64 is the verified platform. Use `node /path/to/installed/research-loop/dist/cli.js` or the installed `research-loop` bin for this release, not an unrelated global installation. See [runtime](../runtime/README.md).
