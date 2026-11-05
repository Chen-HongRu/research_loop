# Durable research evidence

The sanitized [observations](../runtime/observations.json) record the checks and synthetic-study counters below. The original Study, saved inputs, seed, candidate and evaluation references, records and credentials remain outside the repository; this report does not rewrite them.

## Observed

- On local macOS arm64 CPU, the combined native suite passed 25 tests (13 engine, 3 environment, 9 platform), with no failures or skips. A separate qualitative `metrics: {}` test passed 1/1, and the Python scorer passed 3/3. TypeScript checking and Ruff formatting/checking of changed Python files passed.
- Coverage includes STOP and deadline, invalid and final-only feedback and identity checks, unknown acknowledgement with bind and no resend, role and Manager failures, symlink/FIFO/oversized output, corrupted cached receipts, native publication restart and diagnostic log failure.
- One authorized DeepSeek Flash study completed two adaptive development rounds in 385,650 ms, before its original 900-second deadline, with 71 logical model requests. Six distinct Plan, Execute and Review conversations were used. Round two referenced `r0001` and had a different candidate hash. Both receipts were valid and execution-complete.
- Both rounds had MSE 0.866224587 and MAE 0.812348423, compared with the frozen training-mean baseline MSE 2.369779875 and MAE 1.265868931 on the same development labels. Round two did not improve these metrics. Manager published twice; pending records and framework live tasks were both zero at observation.
- The framework executes all nine business checkpoints: seven Loop phases and two Manager phases, rather than a parallel phase ledger. Under the counting convention in the observations, core runtime size is 2,509 lines including SDK glue, tools, schemas, prompts, CLI, config and platform code; the task adapter is 220 lines, tests and synthetic provider 1,552, Python tests 53, and the reporting tool 104.

To run the native combined tests, from `runtime/` after `npm ci`:

```sh
RESEARCH_NATIVE_SANDBOX_TEST=1 UV_CACHE_DIR=/path/to/cache npm test
```

Without the flag, native environment and engine cases skip; the experiments suite separately gates native cases on macOS. A run with skips is not evidence of the native combined result above.

## Limitations

The equal aggregate metrics do not establish that prediction vectors were identical. Candidate-internal CV and ablation figures were agent reports and were not trusted scorer measurements. The fixture's development feedback is adaptive; this run does not establish final-test generalization or general research benefit. Preparation generates a new seed and input version, so reproducing these numbers requires the original saved inputs.

The usage count includes cache tokens; actual billing and server routing or thinking were not verified. Requesting thinking off does not establish that the server performed no reasoning. Ordinary storage failures can require stopping and manual intervention; the tested recovery cases do not establish automatic recovery from every failure.

Linux, Windows, GPU, remote jobs and production deployment were not validated. Stage C still requires code exit and an actually authorized migration; this evidence does not complete or close [#94](https://github.com/Chen-HongRu/research_loop/issues/94) or [#97](https://github.com/Chen-HongRu/research_loop/issues/97).
