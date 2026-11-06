"""Baseline: predict the training mean, without reading development labels."""


def fit_predict(train, dev):
    mean = sum(row["target"] for row in train) / len(train)
    return [{"id": row["id"], "prediction": mean} for row in dev]
