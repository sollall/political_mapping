"""第7段階: 品質チェック（仕様書第10章）を実行し、レポートに出す。

reports/validation.md / validation.json
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .config import AxesConfig, Settings
from .stage import sum_quantity

log = logging.getLogger(__name__)

STATUS_ORDER = {"pass": 0, "info": 0, "warn": 1, "fail": 2}


@dataclass
class Check:
    name: str
    criterion: str
    status: str
    summary: str
    details: list = field(default_factory=list)


def check_codes(settings: Settings) -> Check:
    path = settings.layer("mart") / "join_log.json"
    crit = "統計と境界データの不一致が0件"
    if not path.exists():
        return Check("地域コードの照合", crit, "warn", "map 段階の結果がないため未実施")
    j = json.loads(path.read_text(encoding="utf-8"))
    n = len(j["statistics_only"]) + len(j["boundary_only"])
    details = [{"code": c, "side": "統計のみ"} for c in j["statistics_only"]]
    details += [{"code": c, "side": "境界のみ"} for c in j["boundary_only"]]
    return Check("地域コードの照合", crit, "pass" if n == 0 else "fail", f"一致 {j['matched']} 件、不一致 {n} 件", details)


def check_totals(settings: Settings, axes: AxesConfig, tol: float) -> Check:
    staging = settings.layer("staging")
    muni = pd.read_parquet(staging / "aligned.parquet")
    agg = pd.read_parquet(staging / "aggregates.parquet")
    crit = "市区町村の合計が都道府県・全国の公表値と一致"
    seen, details = set(), []
    n_compared, has_suppressed = 0, False
    for axis in axes.axes:
        for q in axis.quantities():
            key = (q.stats_data_id, json.dumps(q.filter, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            m = sum_quantity(muni, q.stats_data_id, q.filter, "skip")
            a = sum_quantity(agg, q.stats_data_id, q.filter, "skip")
            if a.empty:
                continue
            m = m.assign(pref=m.index.str[:2])
            by_pref = m.groupby("pref").agg(value=("value", "sum"), suppressed=("suppressed", "sum"))
            prefs = a[a.index.str.endswith("000") & (a.index != "00000")]
            for code, row in prefs.iterrows():
                p = code[:2]
                if p not in by_pref.index:
                    continue
                n_compared += 1
                diff = float(by_pref.at[p, "value"] - row["value"])
                if abs(diff) > tol:
                    sup = int(by_pref.at[p, "suppressed"])
                    has_suppressed |= sup > 0
                    details.append(
                        {"axis": axis.axis_id, "table": q.stats_data_id, "filter": q.filter, "area": code,
                         "published": row["value"], "sum": float(by_pref.at[p, "value"]), "diff": diff,
                         "suppressed_cells": sup}
                    )
            if "00000" in a.index and not settings.prefectures:
                n_compared += 1
                total = float(m["value"].sum())
                diff = total - float(a.at["00000", "value"])
                if abs(diff) > tol:
                    sup = int(m["suppressed"].sum())
                    has_suppressed |= sup > 0
                    details.append(
                        {"axis": axis.axis_id, "table": q.stats_data_id, "filter": q.filter, "area": "00000",
                         "published": float(a.at["00000", "value"]), "sum": total, "diff": diff,
                         "suppressed_cells": sup}
                    )
    if n_compared == 0:
        return Check("合計の整合", crit, "warn", "比較できる都道府県・全国の値がありません")
    status = "pass" if not details else ("warn" if all(d.get("suppressed_cells", 0) > 0 for d in details) else "fail")
    summary = f"{n_compared} 件を比較、不一致 {len(details)} 件"
    if has_suppressed:
        summary += "（秘匿セルを含む不一致あり）"
    return Check("合計の整合", crit, status, summary, details)


def check_ranges(long: pd.DataFrame, axes: AxesConfig) -> Check:
    details = []
    bounds = {"ratio": (0, 1), "difference": (-1, 1)}
    for a in axes.axes:
        if a.formula not in bounds:
            continue
        lo, hi = bounds[a.formula]
        v = long[(long["axis_id"] == a.axis_id)]
        bad = v[(v["value"] < lo - 1e-9) | (v["value"] > hi + 1e-9)]
        details += [{"axis": a.axis_id, "area": r.area_code, "value": r.value} for r in bad.itertuples()]
    return Check("比率の範囲", "すべての比率が0〜1の範囲内（差は-1〜1）", "pass" if not details else "fail",
                 f"範囲外 {len(details)} 件", details)


def check_unknown(long: pd.DataFrame, threshold: float) -> Check:
    details = []
    for axis_id, g in long.groupby("axis_id", sort=False):
        u = g["unknown_rate"]
        if u.notna().any():
            details.append({"axis": axis_id, "mean": float(u.mean()), "max": float(u.max()),
                            "over_threshold": int((u > threshold).sum())})
    over = sum(d["over_threshold"] for d in details)
    return Check("不詳率", f"軸ごとに記録し、閾値（{threshold:.0%}）超えを警告", "warn" if over else "pass",
                 f"記録 {len(details)} 軸、閾値超え {over} 地域", details)


def check_small(z: pd.DataFrame, threshold: float) -> Check:
    details = [
        {"axis": axis_id, "flagged": int(g["small"].sum()), "excluded": int(g["excluded"].sum())}
        for axis_id, g in z.groupby("axis_id", sort=False)
    ]
    n = sum(d["flagged"] for d in details)
    return Check("小人口", f"分母が閾値（{threshold:g}）未満の地域にフラグ", "info",
                 f"フラグ {n} 件（軸×地域）", details)


def check_suppressed(long: pd.DataFrame) -> Check:
    details = [
        {"axis": axis_id, "areas_with_suppressed": int((g["suppressed_cells"] > 0).sum()),
         "suppressed_cells": int(g["suppressed_cells"].sum()), "missing_values": int(g["value"].isna().sum())}
        for axis_id, g in long.groupby("axis_id", sort=False)
    ]
    return Check("秘匿値", "秘匿・欠損の件数を軸ごとに記録", "info",
                 f"欠損 {sum(d['missing_values'] for d in details)} 件（軸×地域）", details)


def check_correlation(settings: Settings, threshold: float) -> Check:
    path = settings.layer("mart") / "analysis" / "correlation.csv"
    crit = f"軸間の相関行列を確認（|r| ≥ {threshold} の組を表示）"
    if not path.exists():
        return Check("軸間の相関", crit, "info", "未計算")
    corr = pd.read_csv(path, index_col=0)
    details = []
    cols = list(corr.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            r = corr.at[a, b]
            if pd.notna(r) and abs(r) >= threshold:
                details.append({"a": a, "b": b, "r": round(float(r), 3)})
    return Check("軸間の相関", crit, "info", f"強い相関 {len(details)} 組（都市化の交絡に注意）", details)


def render_markdown(checks: list[Check], overall: str) -> str:
    icon = {"pass": "✅", "info": "ℹ️", "warn": "⚠️", "fail": "❌"}
    lines = ["# 検証レポート", "", f"総合判定: **{overall}**", "", "| チェック | 基準 | 結果 | 概要 |", "|---|---|---|---|"]
    for c in checks:
        lines.append(f"| {c.name} | {c.criterion} | {icon[c.status]} {c.status} | {c.summary} |")
    for c in checks:
        if not c.details:
            continue
        lines += ["", f"## {c.name}", ""]
        keys = list(c.details[0].keys())
        lines.append("| " + " | ".join(keys) + " |")
        lines.append("|" + "---|" * len(keys))
        for d in c.details[:200]:
            lines.append("| " + " | ".join(_fmt(d.get(k)) for k in keys) + " |")
        if len(c.details) > 200:
            lines.append(f"\n（ほか {len(c.details) - 200} 件は validation.json を参照）")
    return "\n".join(lines) + "\n"


def _fmt(v) -> str:
    if isinstance(v, float):
        return "" if np.isnan(v) else f"{v:.4g}"
    if isinstance(v, dict):
        return ", ".join(f"{k}={'/'.join(x) if isinstance(x, list) else x}" for k, x in v.items())
    return str(v)


def run(settings: Settings, axes: AxesConfig) -> str:
    mart = settings.layer("mart")
    long = pd.read_parquet(mart / "axes_long.parquet")
    z = pd.read_parquet(mart / "analysis" / "zscores.parquet")
    q = settings.get("quality", default={}) or {}
    checks = [
        check_codes(settings),
        check_totals(settings, axes, float(q.get("total_tolerance", 0.5))),
        check_ranges(long, axes),
        check_unknown(long, float(q.get("unknown_rate_threshold", 0.1))),
        check_small(z, float(settings.get("small_population", "threshold", default=0) or 0)),
        check_suppressed(long),
        check_correlation(settings, float(q.get("correlation_threshold", 0.7))),
    ]
    overall = max((c.status for c in checks), key=lambda s: STATUS_ORDER[s])
    overall = "pass" if overall == "info" else overall
    settings.report_dir.mkdir(parents=True, exist_ok=True)
    (settings.report_dir / "validation.json").write_text(
        json.dumps({"overall": overall, "checks": [asdict(c) for c in checks]}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (settings.report_dir / "validation.md").write_text(render_markdown(checks, overall), encoding="utf-8")
    for c in checks:
        (log.warning if c.status in ("warn", "fail") else log.info)("[%s] %s: %s", c.status, c.name, c.summary)
    return overall
