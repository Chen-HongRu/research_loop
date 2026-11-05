# Research Loop

Research Loop runs bounded, evidence-driven investigations using PiDurable 1.5 and official pi-env 1.5. A round freezes a Plan, executes an authorized local CPU operation, and publishes a trusted receipt and independent Review. Development feedback is adaptive and does not establish final-test generalization.

The supported source entry is the root Node package. Node.js >= 22.19, Python and [uv](https://docs.astral.sh/uv/) are required. Native isolation uses macOS `sandbox-exec`; local macOS arm64 CPU is the verified platform. Other platforms, GPU and remote execution are not validated.

```sh
npm ci
npm run build
node dist/cli.js doctor
```

`doctor` makes no model call. It checks local prerequisites, compiled entry and locked dependency metadata; successful native isolation requires the native tests or an authorized smoke. For a bounded synthetic study, see the [runtime instructions](runtime/README.md). For existing Studies, read [recovery and authorization](docs/durable-research.md#recovery-and-authorization); for retired Studies, use the [retirement and history guide](docs/runtime-retirement.md).

## Evidence and boundaries

The [stage B evidence](docs/durable-research-evidence.md) records observed results and limitations. Source retirement does not establish that any production deployment has switched or that an old service has stopped. Existing installations and jobs require operator reconciliation; issues [#94](https://github.com/Chen-HongRu/research_loop/issues/94) and [#97](https://github.com/Chen-HongRu/research_loop/issues/97) remain open. Historical reports are identified in the [retirement index](docs/runtime-retirement.md#historical-material).

The [source and package checks](docs/runtime-retirement.md#source-and-package-verification) include 33 passing native tests and an independent installation of the compiled application archive, with one real CPU evaluation and no model call.

## Development

```sh
npm run check
RESEARCH_NATIVE_SANDBOX_TEST=1 UV_CACHE_DIR=/path/to/cache npm test
```

Native cases require the flag and macOS. Use `uv` for Python environments and run `uvx ruff format` and `uvx ruff check` on changed Python files, relevant tests and the smallest real smoke. See [contributing](CONTRIBUTING.md). Model, GPU and remote work require explicit account and resource authorization.
