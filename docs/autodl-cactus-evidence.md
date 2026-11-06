# AutoDL Cactus research evidence

The [sanitized observations](../integrations/cactus_remote/observations.json) accompany this report. Controller source was frozen at commit `43b1f9912ea9222c1aeca1d916f8ca2955416c5f`. Pi Durable and pi-env 1.5.0 and DeepSeek Flash ran on local macOS arm64; the fixed trusted tiny-CNN training ran on one existing AutoDL RTX 4090. No existing Study was migrated.

The fixed protocol used 11,340 training and 2,835 adaptive development rows, one epoch, seed 0, batch 128, Adam and binary cross entropy. Prediction and the official local scorer used development data only; no final-test score was produced.

| Round | Learning rate | Development ROC-AUC | Training time | Peak allocated CUDA memory |
| --- | ---: | ---: | ---: | ---: |
| r0001 | 0.001 | 0.9655185578113197 | 4.350685 s | 78,624,256 bytes |
| r0002 | 0.0003 | 0.9457665578970493 | 2.886676 s | 78,624,256 bytes |

Round two's autonomous task referenced `r0001` and its ROC-AUC fell by 0.0197519999142704. Six distinct Plan, Execute and Review conversations and two Manager publications completed; pending records, live framework tasks and live submissions were zero. The recorded achieved goal means the two investigations were completed, not that AUC improved or generalization was established.

From preparation to the final Manager publication took 353,796 ms under the original 1,800-second authorization. Framework usage recorded 77 logical model calls. A subsequent `run` on the completed Study retained 77 calls and the same two remote operations, with no new calls or operations. Both original remote workers had exited; the later GPU snapshot was 3 MiB and 0% utilization, not a training-period utilization measurement. Training-period GPU utilization was not measured.

Verification included 39 local native TypeScript checks without skips, six remote Linux standard-library marker/sleep fault tests without GPU, and one malformed-scorer-artifact test with five subcases. TypeScript checking, build, Prettier and Ruff passed. These checks and the two-round run cover the fixed trusted program and typed learning-rate parameter, not arbitrary agent-authored Python on the remote host.

The final source count includes 3,306 lines of generic controller, configuration and deployment code, plus 1,361 of the actual remote platform bridge: 4,667 combined. Cactus task preparation/training/evaluation is 474 lines and the CPU task is 220, counted separately. Controller tests and synthetic provider are 1,914, Cactus tests 719 and CPU Python tests 53; the counting tool is 168. Generated lockfiles, compiled copies and prose are excluded. The observations contain per-file counts and initialization, first-batch, throughput and memory measurements; unmeasured costs are explicit.

The model request specified thinking off; server-side reasoning, routing and actual billing were not verified. Development feedback is adaptive and does not establish final-test performance or general research benefit. The user selected a new Study and project subdirectory; this evidence does not establish that any legacy deployment or other Study has stopped or migrated. See the [adapter contract](../integrations/cactus_remote/README.md) and [retirement boundaries](runtime-retirement.md).
