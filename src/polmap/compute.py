"""第4段階: 軸定義に従って指標を計算する（mart層）。

mart/axes_long.parquet … 地域×軸の縦持ち（分子・分母・不詳率・秘匿セル数つき）
mart/axes_wide.parquet … 地域×軸の広い表（値のみ）
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import Axis, AxesConfig, Quantity, Ratio, Settings
from .stage import sum_quantity

log = logging.getLogger(__name__)


def _q(df: pd.DataFrame, q: Quantity, policy: str) -> pd.DataFrame:
    return sum_quantity(df, q.stats_data_id, q.filter, policy)


def _div(a: pd.Series, b: pd.Series) -> pd.Series:
    b = b.where(b > 0)
    return a / b


def _ratio(df: pd.DataFrame, r: Ratio, policy: str, index: pd.Index) -> pd.DataFrame:
    num = _q(df, r.numerator, policy).reindex(index)
    den = _q(df, r.denominator, policy).reindex(index)
    out = pd.DataFrame(index=index)
    out["numerator"] = num["value"]
    out["denominator"] = den["value"]
    out["value"] = _div(num["value"], den["value"])
    out["suppressed_cells"] = num["suppressed"].fillna(0) + den["suppressed"].fillna(0)
    if r.unknown is not None:
        unk = _q(df, r.unknown, policy).reindex(index)
        out["unknown"] = unk["value"]
        out["unknown_rate"] = _div(unk["value"], den["value"] + unk["value"].fillna(0))
        out["suppressed_cells"] += unk["suppressed"].fillna(0)
    else:
        out["unknown"] = np.nan
        out["unknown_rate"] = np.nan
    return out


def compute_axis(df: pd.DataFrame, axis: Axis, policy: str = "missing") -> pd.DataFrame:
    index = pd.Index(sorted(df["area_code"].unique()), name="area_code")
    if axis.formula == "ratio":
        out = _ratio(df, axis.terms[0], policy, index)
    elif axis.formula == "difference":
        a = _ratio(df, axis.terms[0], policy, index)
        b = _ratio(df, axis.terms[1], policy, index)
        out = a.copy()
        out["value"] = a["value"] - b["value"]
        out["unknown_rate"] = pd.concat([a["unknown_rate"], b["unknown_rate"]], axis=1).max(axis=1, skipna=True)
        out["suppressed_cells"] = a["suppressed_cells"] + b["suppressed_cells"]
    else:  # growth
        cur = _q(df, axis.growth[0], policy).reindex(index)
        base = _q(df, axis.growth[1], policy).reindex(index)
        out = pd.DataFrame(index=index)
        out["numerator"] = cur["value"]
        out["denominator"] = base["value"]
        out["value"] = _div(cur["value"], base["value"]) - 1
        out["unknown"] = np.nan
        out["unknown_rate"] = np.nan
        out["suppressed_cells"] = cur["suppressed"].fillna(0) + base["suppressed"].fillna(0)
    out["suppressed_cells"] = out["suppressed_cells"].astype(int)
    out.insert(0, "axis_id", axis.axis_id)
    return out.reset_index()


def run(settings: Settings, axes: AxesConfig) -> None:
    if not axes.axes:
        raise RuntimeError("軸が1つも定義されていません（axes.yaml の axes に追加してください）")
    df = pd.read_parquet(settings.layer("staging") / "aligned.parquet")
    policy = settings.get("compute", "suppressed_policy", default="missing")
    longs = []
    for axis in axes.axes:
        res = compute_axis(df, axis, policy)
        log.info("%s: 有効 %d / %d 地域", axis.axis_id, int(res["value"].notna().sum()), len(res))
        longs.append(res)
    long = pd.concat(longs, ignore_index=True)
    mart = settings.layer("mart")
    long.to_parquet(mart / "axes_long.parquet", index=False)
    wide = long.pivot(index="area_code", columns="axis_id", values="value")
    wide = wide[[a.axis_id for a in axes.axes]].reset_index()
    wide.to_parquet(mart / "axes_wide.parquet", index=False)
