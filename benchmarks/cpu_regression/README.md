# Historical synthetic CPU regression fixture

This fixture targets the retired runtime and is not a current runtime example. See [runtime history](../../docs/runtime-retirement.md).

This fixture supplies 96 training examples with labels and 48 unlabeled development feature rows. A preparation-only random seed and the development labels remain in the external host's private directory. The candidate's `fit_predict(train, dev)` returns one prediction per development ID. Its initial implementation predicts the training mean.

Prepare an external study with the exact provider and model you intend to authorize:

```sh
uv run --no-project python benchmarks/cpu_regression/prepare.py /path/to/new-study --provider PROVIDER --model MODEL
```

Preparation creates `study.json`, the candidate source, public inputs and a protected private scorer directory. The generated configuration authorizes two CPU slots for at most 30 seconds each, expiring after 900 seconds unless `--seconds` is supplied. It disables model thinking explicitly. Python resolves to the exact preparation interpreter unless `--python` is supplied. The original deadline is never refreshed on recovery. Keep all generated files outside Git.

Prediction runs from a frozen code snapshot inside the OS environment with read access to that snapshot and public inputs, and write access only to the prediction output directory. The runtime reads a bounded regular prediction file through that same environment with symlink following disabled, then seals the captured bytes in a host-only file. The trusted host scorer pairs only that sealed input with hidden labels; it never imports candidate code. It requires the exact ID set, unique IDs and finite numeric predictions, then reports MSE, MAE and R². Candidate-written scores are ignored.

Preparation also freezes the training-target mean in a private baseline file. Every scoring result includes `baseline_mse` and `baseline_mae`, measured on the exact same development labels as the candidate. Compare candidate `mse`/`mae` directly with those baseline values. R² uses the development-label mean in its denominator; R² ≤ 0 alone does not establish whether the candidate beats the training-mean baseline. The scorer never consults candidate code to determine the baseline.

Two rounds are a test of the research loop, not a promise that the second result improves. Development scores are adaptive feedback and provide no evidence about final-test generalization. This fixture uses real local CPU processes; it does not validate GPU or remote execution.
