# Cactus smoke

The local controller uses the root project's Pydantic v2 dependency (`uv sync
--locked` at the repository root). Its report-only model supplies both the CLI
schema and local validation; remote trainer dependencies and scoring are unchanged.

Controller preparation uses the prepared dataset in the [TIGER-Lab mirror](https://huggingface.co/datasets/TIGER-Lab/mle-bench/commit/01a3db85f9245ba64d87ed2a1170d486ee147e1f), pinned to `01a3db85f9245ba64d87ed2a1170d486ee147e1f`. The ~19 MB archive pair contains actual Kaggle Cactus images. No Kaggle credential was available. Mirror image bytes are not independently verified against Kaggle. The official preparation and grading sources are pinned to [MLE-bench commit 507f92e](https://github.com/openai/mle-bench/tree/507f92e1138bb6e40dac5c6ee7a6758e6424bf97).

Official public/private partition: `train_test_split(original_train, test_size=0.19, random_state=0)`, yielding 14,175 public and 3,325 private rows. Additional project train/dev partition: `train_test_split(public_train, test_size=0.20, random_state=0)` without stratification, yielding 11,340 fitting rows and 2,835 dev rows. The one-time contract check compares exact IDs and labels against the official partition under original sorted IDs, and checks dev isolation. It reads CSV metadata, never image contents. It is not part of training startup.

Use `uv` for the task environment. To reuse an operator-provisioned CUDA/PyTorch Python environment, create a task-local environment with `uv venv --python /path/to/python --system-site-packages .venv`. Choose a CUDA/PyTorch combination appropriate to the task host. Install the other dependencies pinned in this directory's `pyproject.toml` using `uv pip install --python .venv/bin/python ...`. Training records actual installed package versions; the inherited base environment is not a fully locked environment.

Preparation, once, by the trusted controller:

```sh
.venv/bin/python prepare.py --root /absolute/task/prepared
.venv/bin/python check_contract.py --root /absolute/task/prepared
```

`prepared/data` contains fitting labels, unlabeled dev/test IDs, separate image directories, and configuration metadata. `prepared/controller` contains all source labels and the authoritative upstream grader and is mode 0700. This permission alone does not isolate an agent sharing the controller's root identity. The smoke uses a local agent without remote SSH or network access; a trusted bridge invokes fixed remote commands, gives the agent only permitted data/configuration and dev feedback, and keeps controller labels remote.

Fixed training invocation (output directory must not already exist):

```sh
CUDA_VISIBLE_DEVICES=1 .venv/bin/python train.py \
  --data /absolute/task/prepared/data --output /absolute/task/runs/candidate-001 \
  --epochs 1 --seed 0 --device cuda:0 --model tiny-cnn \
  --batch-size 128 --lr 0.001 --threads 4
```

The model is three convolution blocks and a pooled linear binary logit, fitted with Adam and binary cross entropy, using RGB tensors of shape `[batch, 3, 32, 32]` scaled to `[0,1]`. There is no pretrained asset, augmentation, early stopping, or held-out-label access. Rows retain CSV order for prediction. Outputs: `checkpoint.pt`, `dev-predictions.csv`, `test-predictions.csv`, source snapshot `train.py`, `run.json`, and `timing.json`. No training data is loaded eagerly or read for validation. Timing excludes Python/import startup; first batch includes image decoding, forward/backward and optimizer update. Throughput excludes the first batch. GPU utilization must be sampled externally; compressed image bytes before the first batch are unmeasured.

Controller dev evaluation:

```sh
.venv/bin/python evaluate.py --controller /absolute/task/prepared/controller \
  --submission /absolute/task/runs/candidate-001/dev-predictions.csv --split dev
```

Output is one JSON object: `{"split":"dev","metric":"roc_auc","score":0.0}` (score shown only as a schema example). Evaluation executes the unmodified official `prepare_for_auroc_metric` function and competition `grade` function AST nodes; pandas, sklearn ROC-AUC, and the invalid-submission exception are bound locally. The official helper validates row count, ID matching, numeric/range constraints, and sorts by ID. Only the controller may invoke `--split test`, after the loop freezes its final candidate. No private score is returned during research.
