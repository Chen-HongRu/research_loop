"""Small operator check of the libraries exposed to research scripts."""

import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="/environment.json")
    parser.add_argument("--allow-cpu", action="store_true")
    args = parser.parse_args()
    environment = json.loads(Path(args.environment).read_text())
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
    report = {
        "purpose": "operator library validation; no scientific or performance claim",
        "environment_id": environment["id"],
        "imports": [],
        "checks": {},
    }
    start = time.monotonic()
    try:
        for package in environment["packages"]:
            imported = importlib.import_module(package["import"])
            version = importlib.metadata.version(package["distribution"])
            if version != package["version"]:
                raise RuntimeError(
                    "Library version differs: " + package["distribution"]
                )
            report["imports"].append(
                {**package, "module_file": getattr(imported, "__file__", None)}
            )
        import numpy as np
        import torch
        from scipy import linalg, stats
        import pandas as pd
        import sympy as sp
        from sklearn.linear_model import LinearRegression
        import statsmodels.api as sm
        import h5py
        import pyarrow as pa
        import pyarrow.parquet as pq
        from safetensors.torch import load, save
        from einops import rearrange
        import opt_einsum
        import networkx as nx
        from Bio.Seq import Seq
        import yaml
        import pytest
        import plotly.graph_objects as go
        from numba import njit
        import matplotlib.pyplot as plt
        import seaborn as sns
        from PIL import Image

        x = np.arange(6, dtype=np.float64)
        y = 2 * x + 1
        design = np.column_stack([np.ones_like(x), x])
        np.testing.assert_allclose(linalg.lstsq(design, y)[0], [1, 2])
        assert stats.norm.cdf(0) == 0.5
        frame = pd.DataFrame({"x": x, "y": y})
        assert frame["y"].mean() == 6
        assert LinearRegression().fit(x[:, None], y).coef_[0] == pytest.approx(2)
        np.testing.assert_allclose(sm.OLS(y, design).fit().params, [1, 2])
        symbol = sp.Symbol("x")
        assert sp.diff(symbol**3, symbol) == 3 * symbol**2
        report["checks"]["numerics_statistics_symbolic"] = "passed"

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device != "cuda" and not args.allow_cpu:
            raise RuntimeError("The research allocation did not expose CUDA")
        tensor = torch.arange(6.0, device=device, requires_grad=True)
        tensor.square().sum().backward()
        torch.testing.assert_close(tensor.grad, 2 * tensor.detach())
        torch.testing.assert_close(
            rearrange(tensor.detach(), "(a b) -> a b", a=2).flatten(), tensor.detach()
        )
        np.testing.assert_allclose(opt_einsum.contract("i,i->", x, x), np.dot(x, x))
        torch.testing.assert_close(
            load(save({"x": tensor.detach().cpu()}))["x"], tensor.detach().cpu()
        )
        report["checks"]["tensors_autograd_serialization"] = "passed"
        report["device"] = device
        report["cuda_version"] = torch.version.cuda
        report["gpu"] = torch.cuda.get_device_name() if device == "cuda" else None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with h5py.File(path / "test.h5", "w") as stream:
                stream["x"] = x
            with h5py.File(path / "test.h5", "r") as stream:
                np.testing.assert_array_equal(stream["x"][:], x)
            pq.write_table(pa.Table.from_pandas(frame), path / "test.parquet")
            pd.testing.assert_frame_equal(
                pq.read_table(path / "test.parquet").to_pandas(), frame
            )
        assert yaml.safe_load("value: 2")["value"] == 2
        assert str(Seq("ATG").translate()) == "M"
        assert nx.shortest_path(nx.path_graph(3), 0, 2) == [0, 1, 2]
        assert np.asarray(Image.new("RGB", (2, 2))).shape == (2, 2, 3)
        report["checks"]["tables_files_biology_graphs"] = "passed"

        @njit(cache=False)
        def square_sum(values):
            return (values * values).sum()

        assert square_sum(x) == np.dot(x, x)
        assert json.loads(go.Figure(go.Scatter(x=x, y=y)).to_json())["data"]
        figure, axes = plt.subplots(figsize=(5, 3))
        sns.lineplot(data=frame, x="x", y="y", ax=axes, marker="o")
        axes.set_title("Library validation: y = 2x + 1")
        figure.tight_layout()
        figure.savefig("libraries.svg")
        plt.close(figure)
        report["checks"]["jit_and_headless_plotting"] = "passed"
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - start
        Path("libraries.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    k: report.get(k)
                    for k in ("status", "error", "device", "elapsed_seconds")
                }
            )
        )


if __name__ == "__main__":
    main()
