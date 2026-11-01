Source: https://github.com/Chen-HongRu/ProTok/blob/8ba779e23b177e18f246aa1b04320c5345e48d65/reports/h-readout-kabsch-ca-w128-10k-20261024/README.md
GitHub blob: 5eb61af137a8babfb4a84658eee0f8ee07dba61a
Retrieved: 2026-11-01

# Frozen CA H → Y → Kabsch readout

Server run: `/data/user/hongruchen/experiments/ProTok/h-readout-kabsch-ca-w128-10k-20261024`

Formal job **757665**, one H100 on ACD1-36. GPU smoke **757628** completed with exit code 0.

Final status verified on 2026-10-25: job757665 completed with exit code0:0 in1:53:50. The completion receipt records global step, optimizer steps and scheduler step10000. All six full4491 validation points (0/2k/4k/6k/8k/10k) completed. W&B reports `finished`, optimizer_step10000, completion_verified=true and no missing validation steps. This supersedes the earlier startup-only status.

W&B: https://forge.coreweave.com/wandb/LeWorldModel/ProTok/runs/hreadout-ca128-frozen46k-10k-757665

## Final comparison

Both runs use the same4491 validation rows, per-row noise, loss protocol and100-step Euler sampling. X is the frozen source CA-w128 checkpoint at46000, not a fresh10k control.

| Metric | Source X,46k | H readout,10k |
| --- | ---: | ---: |
| CA RMSD (Angstrom) | 1.956120 | 2.192616 |
| CA lDDT | 0.785870 | 0.682832 |
| TM-score | 0.848203 | 0.816592 |
| CA bond RMSE (Angstrom) | 0.389174 | 0.953067 |
| Validation velocity loss | 0.206821 | 0.239205 |

The best sampled RMSD among saved H validations is2.129910 at6000, still worse than X. Between6000 and10000, validation loss decreases from0.250233 to0.239205 while sampled RMSD worsens. At10000, paired RMSD improves on879/4491 proteins and lDDT on3/4491. This frozen-backbone probe does not establish that H lacks structural information or identify Kabsch instability as the cause.

## Experiment

- Freeze CA-w128 D12 checkpoint at step 46000, including input projection, time embedding, and all feature/coordinate layers. Actual checkpoint global step, Adam steps, and scheduler step were verified as 46000 on an allocated compute node.
- Train only the 17,155-parameter `128 → 128 → GELU → LayerNorm → 3` readout. Frozen backbone: 1,271,824 parameters. Use final H, not raw 1024-dimensional teacher hidden.
- Center predicted Y, fit a proper SO(3) rotation to the current noisy Xt with masked FP64 Kabsch, and supervise the resulting Cartesian clean endpoint. Rotation remains in the autograd graph. No target alignment, reflection, scale fitting, or fixed-size normalization.
- Fresh readout optimizer steps 0–10000. Preserve B32, accumulation8, K4, seed42, data/noise seed20261022, Proteina times, FP32/TF32off, compiled denoise, workers2, AdamW1e-4/betas(.9,.99)/matrix weight decay.05/warmup1000.
- Preserve the first 10k steps of the original **50k cosine horizon**, minimum LR factor .1. This is not a 10k cosine decay or a source-optimizer resume.
- Training denominator floor .05; sampling floor .01; 100-step Cartesian Euler. Ten-update mean training loss; all4491 validation at0/2000/4000/6000/8000/10000. Smoke used2 validation rows and is not uploaded as formal validation.

## Evidence

- Synthetic rotation error: 1.55e-15; proper determinant approximately1. Both determinant branches passed double-precision finite-difference gradient checks; the test distinguishes full gradients from detached-rotation gradients.
- Real two-row condition and H match source exactly. After two updates, frozen weights remain bitwise equal and have no gradients. Real output rotation error1.37e-6; Euler rotation error1.46e-6; manual sampler exact.
- Compiled/eager maximum output difference2.26e-6; gradient relative RMSE2.89e-7.
- B32/A8/K4 training smoke completed10 optimizer/scheduler steps with finite loss and readout gradient; backbone gradients zero. All changed Python files passed Ruff format/check. Independent static mathematics/code review found no blocker.
- Rank0/1 synthetic alignments are explicitly nonidentifiable, with nonfinite gradients; they are diagnostic limitations, not stability passes. The tests do not establish long-run convergence or stability for every degenerate input.

## Startup and cost

Formal initialization1.58s; metadata validation0.0014s, two manifest reads totaling130,940bytes and two identity file stats, no dataset-content validation. First-batch latency0.134s. Checkpoint loading and data-loader prefetch are additional I/O; physical bytes before the first batch are unmeasured. Cache staging for the smoke took89s; the formal run reused the published node cache in0s. Smoke peak allocated GPU memory1.38GB. Its first10 updates averaged7.57s including compilation, so this is not a steady-state throughput measurement. Early post-compilation training throughput is recorded above. Sampled GPU utilization during initial validation was81–96%; a training-wide utilization average is unmeasured.

## Stopped width runs

All eight previous CA/HBFM width runs are stopped, with checkpoints/logs preserved. Final four CSV optimizer steps: CA12847220, CA32035320, HBFM12844560, HBFM32036000. Last full validation steps:46000/34000/44000/34000 respectively. W&B histories were verified against CSV; native status is `failed`, with explicit `user-stopped` tags and stop reason. Interrupted HBFM320 validation at36000 is not represented as a completed validation.

## Source archive boundary

`artifacts/code/` contains five exact historical source/configuration files: the H model, trainer, contract test, JSON configuration and sbatch launcher. These and `artifacts/trainer.patch` are explicitly included in Git for review. Selected experiment receipts are also tracked; checkpoints and uploader state remain local.

`records/train/` contains the final config, startup/completion receipts, complete train/eval curves and evaluation protocols retrieved from the server on2026-10-25. Use these for final analysis; `artifacts/train/` is the historical startup-only copy. `records/paired/` contains all4491 per-protein metrics for H6000/H10000 and the source X46000, allowing an exact row_id/name/length join without protein coordinates. `records/retrieval.json` records the source paths and retrieval time.

This is a partial source excerpt, not a standalone runnable checkout. The complete run snapshot remains at the server path above under `code/`; it also needs the original source checkpoint, hidden cache and Linux environment. The excerpt does not identify a complete base Git revision; exact dependency-source equivalence with the current main checkout is unverified.

Do not apply `trainer.patch` to current main or replace its trainer with this copy: the historical trainer predates main's current validation/logging changes. Future integration requires a separate scoped change and verification. `wandb-state.json` is local runtime state; the completed result is recorded above.

