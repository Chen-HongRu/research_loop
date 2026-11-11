"""The assigned architecture intervention must actually be present."""

import pytest

from benchmarks.experiment_cycle.interventions import _BASELINE, verify_intervention


@pytest.fixture
def candidates():
    baseline = _BASELINE
    return {
        1: baseline.replace("nn.ReLU()", "nn.GELU()"),
        2: baseline.replace("nn.ReLU()", "nn.SiLU()"),
        3: baseline.replace(
            "nn.Conv2d(3, 32, 3, padding=1),",
            "nn.Conv2d(3, 32, 3, padding=1), nn.BatchNorm2d(32),",
        )
        .replace(
            "nn.Conv2d(32, 64, 3, padding=1),",
            "nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64),",
        )
        .replace(
            "nn.Conv2d(64, 64, 3, padding=1),",
            "nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64),",
        ),
        4: baseline.replace(
            "nn.Conv2d(3, 32, 3, padding=1),",
            "nn.Conv2d(3, 32, 3, padding=1), nn.GroupNorm(8, 32),",
        )
        .replace(
            "nn.Conv2d(32, 64, 3, padding=1),",
            "nn.Conv2d(32, 64, 3, padding=1), nn.GroupNorm(8, 64),",
        )
        .replace(
            "nn.Conv2d(64, 64, 3, padding=1),",
            "nn.Conv2d(64, 64, 3, padding=1), nn.GroupNorm(8, 64),",
        ),
        5: baseline.replace("AdaptiveAvgPool2d", "AdaptiveMaxPool2d"),
        6: baseline.replace("nn.Flatten(),", "nn.Flatten(), nn.Dropout(0.2),"),
        7: baseline.replace("nn.Conv2d(32, 64,", "nn.Conv2d(32, 96,")
        .replace("nn.Conv2d(64, 64,", "nn.Conv2d(96, 96,")
        .replace("nn.Linear(64,", "nn.Linear(96,"),
        8: baseline.replace("nn.Conv2d(32, 64,", "nn.Conv2d(32, 32,")
        .replace("nn.Conv2d(64, 64,", "nn.Conv2d(32, 32,")
        .replace("nn.Linear(64,", "nn.Linear(32,"),
        9: baseline.replace(
            "nn.Conv2d(3, 32, 3, padding=1)", "nn.Conv2d(3, 32, 5, padding=2)"
        ),
        10: baseline.replace(
            "nn.AdaptiveAvgPool2d(1)",
            "nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(), nn.AdaptiveAvgPool2d(1)",
        ),
    }


@pytest.mark.parametrize("number", range(1, 11))
def test_each_declared_intervention_and_noop(candidates, number):
    assert verify_intervention(_BASELINE, candidates[number], number) is None
    with pytest.raises(ValueError):
        verify_intervention(_BASELINE, _BASELINE, number)
    with pytest.raises(ValueError):
        verify_intervention(_BASELINE, candidates[number % 10 + 1], number)


def test_normalizes_keywords_defaults_and_pairs(candidates):
    candidate = (
        candidates[1]
        .replace(
            "nn.Conv2d(3, 32, 3, padding=1)",
            "nn.Conv2d(kernel_size=(3, 3), out_channels=32, in_channels=3, padding=(1, 1), stride=1, bias=True)",
        )
        .replace(
            "nn.MaxPool2d(2)",
            "nn.MaxPool2d(kernel_size=(2, 2), stride=(2, 2), padding=0)",
        )
    )
    candidate = candidate.replace("nn.GELU()", "nn.GELU(approximate='none')")
    candidate = candidate.replace("nn.Flatten()", "nn.Flatten(start_dim=1, end_dim=-1)")
    candidate = candidate.replace(
        "nn.Linear(64, 1)", "nn.Linear(out_features=1, in_features=64, bias=True)"
    )
    verify_intervention(_BASELINE, candidate, 1)


@pytest.mark.parametrize("bad_id", [0, 11, True, 1.0, "1", None])
def test_invalid_id_rejected(bad_id):
    with pytest.raises(ValueError):
        verify_intervention(_BASELINE, _BASELINE, bad_id)


@pytest.mark.parametrize(
    "replacement",
    [
        "nn.Unknown(2)",
        "nn.Conv2d(3, 32, compute())",
        "nn.Conv2d(3, 32, 3, padding=1, **{})",
        "nn.Conv2d(3, 32, 3, padding=1, stride=1, **dict())",
    ],
)
def test_malformed_layers_rejected(candidates, replacement):
    with pytest.raises(ValueError):
        verify_intervention(
            _BASELINE,
            candidates[1].replace("nn.Conv2d(3, 32, 3, padding=1)", replacement),
            1,
        )


def test_wrong_baseline_rejected(candidates):
    with pytest.raises(ValueError, match="Baseline"):
        verify_intervention(
            _BASELINE.replace("nn.Linear(64, 1)", "nn.Linear(64, 2)"), candidates[1], 1
        )
