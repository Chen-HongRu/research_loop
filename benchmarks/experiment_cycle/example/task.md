# SYNTHETIC paired offset experiment

This hand-constructed CPU fixture tests the experiment-cycle plumbing. It is
development data, not a recovered historical experiment or evidence of research
benefit. No network, model calls, GPU, training data or private inputs are needed.

Question: does adding the prespecified constant 1 to each control prediction
reduce mean squared error by **strictly more than 0.5** on these four paired
observations? The control prediction equals `x`; the candidate equals `x + 1`.
The same rows and targets must be used for both. Do not fit a parameter or select
rows. `inputs.json` is the sole fixed evaluation set; there is no training split.

Plan must specify the hypothesis, matched control, permitted change, the strict
threshold, and the meaning of supported and contradicted outcomes. Claims are
restricted to these four synthetic observations.

Prepare must implement `experiment.py` using the standard library, preserve
`inputs.json`, and return exactly the command `["{python}", "experiment.py"]`.
Use each input row in its original order. Write `result.json` with exactly
`{"version": 1, "observations": [...]}`; each observation has exactly `id`,
`control_prediction`, and `candidate_prediction`. Predictions must be finite
numbers. The script takes no arguments. Do not include targets or summary
metrics in this result. Prepare may compile the script as a smoke; running the
substantive command belongs only to Execute, which starts with no result file.

Evaluate must return exactly `disposition`, `metrics`, `limitations`, `evidence`.
Disposition is `supported` if control MSE minus candidate MSE is strictly above
0.5, otherwise `contradicted`; missing evidence warrants
`insufficient_evidence`. Metrics are `control_mse`, `candidate_mse`, and
`delta_mse` (candidate minus control). `limitations` is nonempty text describing
the synthetic, fixed-sample scope. `evidence` contains workspace-relative file
references including `result.json`. A host checker recomputes the metrics from
predictions and frozen targets; a successful process exit alone is insufficient.
