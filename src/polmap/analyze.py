"""第5段階: 正規化と統合分析（仕様書第8章）。

正規化 → 主成分分析 → クラスタリング の順に実行し、mart/analysis/ に保存する。
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import fcluster, linkage
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import adjusted_rand_score, silhouette_score

from .config import AxesConfig, Settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- 正規化

def weighted_mean_std(x: pd.Series, w: pd.Series | None) -> tuple[float, float]:
    m = x.notna()
    if w is not None:
        m &= w.notna() & (w > 0)
    x = x[m]
    if len(x) < 2:
        return np.nan, np.nan
    if w is None:
        return float(x.mean()), float(x.std(ddof=0))
    w = w[m]
    mean = float(np.average(x, weights=w))
    return mean, float(np.sqrt(np.average((x - mean) ** 2, weights=w)))


def eb_smooth(num: pd.Series, den: pd.Series) -> pd.Series:
    """経験ベイズ平滑化（Marshall の大域推定、モーメント法）。

    全体比率 m に向けて、分母が小さい地域ほど強く引き寄せる。
    """
    ok = num.notna() & den.notna() & (den > 0)
    r = num / den.where(den > 0)
    if ok.sum() < 3:
        return r
    m = num[ok].sum() / den[ok].sum()
    var_between = (den[ok] * (r[ok] - m) ** 2).sum() / den[ok].sum() - m / den[ok].mean() * (1 - m)
    var_between = max(var_between, 0.0)
    shrink = var_between / (var_between + m * (1 - m) / den)
    return (m + shrink * (r - m)).where(ok)


def normalize(long: pd.DataFrame, axes: AxesConfig, settings: Settings) -> pd.DataFrame:
    threshold = float(settings.get("small_population", "threshold", default=0) or 0)
    method = settings.get("small_population", "method", default="exclude")
    weighting = settings.get("normalize", "weighting", default="none")
    clip = float(settings.get("normalize", "winsorize_sigma", default=3.0))
    formulas = {a.axis_id: a.formula for a in axes.axes}

    out = []
    for axis_id, g in long.groupby("axis_id", sort=False):
        g = g.copy()
        g["small"] = g["denominator"].notna() & (g["denominator"] < threshold)
        g["value_used"] = g["value"]
        if method == "eb" and formulas[axis_id] == "ratio":
            g["value_used"] = eb_smooth(g["numerator"], g["denominator"])
            g["excluded"] = g["value_used"].isna()
        else:
            g["excluded"] = g["value"].isna() | (g["small"] if method in ("exclude", "eb") else False)
            g.loc[g["excluded"], "value_used"] = np.nan
        x = g["value_used"]
        mu, sd = weighted_mean_std(x, None)
        g["z_unweighted"] = (x - mu) / sd if sd else np.nan
        mu_w, sd_w = weighted_mean_std(x, g["denominator"])
        g["z_weighted"] = (x - mu_w) / sd_w if sd_w else np.nan
        g["z"] = g["z_weighted"] if weighting == "population" else g["z_unweighted"]
        g["z_clipped"] = g["z"].clip(-clip, clip)
        g["mean"], g["std"] = (mu_w, sd_w) if weighting == "population" else (mu, sd)
        g["rank"] = g["value"].rank(ascending=False, method="min")
        g["rank_of"] = int(g["value"].notna().sum())
        out.append(g)
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------- 主成分分析

def choose_components(ratio: np.ndarray, threshold: float, lo: int, hi: int) -> int:
    cum = np.cumsum(ratio)
    n = int(np.searchsorted(cum, threshold) + 1)
    return int(max(min(n, hi, len(ratio)), min(lo, len(ratio))))


def run_pca(z: pd.DataFrame, settings: Settings, axes: AxesConfig) -> dict:
    n_axes = z.shape[1]
    full = PCA(n_components=n_axes).fit(z.values)
    ratio = full.explained_variance_ratio_
    n = settings.get("pca", "n_components")
    if not n:
        n = choose_components(
            ratio,
            float(settings.get("pca", "cumulative_threshold", default=0.7)),
            int(settings.get("pca", "min_components", default=2)),
            int(settings.get("pca", "max_components", default=4)),
        )
    n = int(min(n, n_axes))
    pcs = [f"PC{i + 1}" for i in range(n)]
    comps = full.components_[:n].copy()
    # 符号を固定: 絶対値最大の負荷量が正になる向き
    for i in range(n):
        if comps[i, np.argmax(np.abs(comps[i]))] < 0:
            comps[i] *= -1
    scores = (z.values - full.mean_) @ comps.T
    loadings = pd.DataFrame(
        comps.T * np.sqrt(full.explained_variance_[:n]), index=z.columns, columns=pcs
    )
    names_cfg = settings.get("pca", "names", default={}) or {}
    by_id = {a.axis_id: a for a in axes.axes}
    names = {}
    for pc in pcs:
        if pc in names_cfg:
            names[pc] = names_cfg[pc]
            continue
        # 仮の名前: 負荷量の絶対値が最大の軸の「A ↔ B」を、符号に合わせて並べる
        top = loadings[pc].abs().idxmax()
        a = by_id[top]
        left, right = (a.left, a.right) if loadings.at[top, pc] > 0 else (a.right, a.left)
        names[pc] = f"{left} ↔ {right}"
    return {
        "pcs": pcs,
        "variance": pd.DataFrame(
            {
                "component": [f"PC{i + 1}" for i in range(n_axes)],
                "explained_variance_ratio": ratio,
                "cumulative": np.cumsum(ratio),
                "eigenvalue": full.explained_variance_,
            }
        ),
        "loadings": loadings,
        "scores": pd.DataFrame(scores, index=z.index, columns=pcs),
        "names": names,
        "provisional": [pc for pc in pcs if pc not in names_cfg],
    }


# ---------------------------------------------------------------- クラスタリング

def run_clustering(scores: pd.DataFrame, settings: Settings) -> dict:
    seed = int(settings.get("seed", default=20201001))
    k_fixed = settings.get("cluster", "k")
    k_lo, k_hi = settings.get("cluster", "k_range", default=[6, 10])
    n = len(scores)
    candidates = [k_fixed] if k_fixed else list(range(int(k_lo), int(k_hi) + 1))
    candidates = [k for k in candidates if 2 <= k < n]
    if not candidates:
        raise RuntimeError(f"クラスタリングに十分な地域がありません（{n} 地域）")
    X = scores.values
    ward_tree = linkage(X, method="ward")
    rows, fits = [], {}
    for k in candidates:
        km = KMeans(n_clusters=k, n_init=20, random_state=seed).fit(X)
        ward = fcluster(ward_tree, t=k, criterion="maxclust")
        fits[k] = (km, ward)
        rows.append(
            {
                "k": k,
                "silhouette": silhouette_score(X, km.labels_, random_state=seed),
                "inertia": km.inertia_,
                "ari_vs_ward": adjusted_rand_score(km.labels_, ward),
            }
        )
    selection = pd.DataFrame(rows)
    k = int(k_fixed or selection.loc[selection["silhouette"].idxmax(), "k"])
    km, ward = fits[k]
    # 番号を大きいクラスター順に 1..k に振り直す（シード固定と合わせて再現性を保つ）
    order = pd.Series(km.labels_).value_counts().index.tolist()
    relabel = {old: i + 1 for i, old in enumerate(order)}
    labels = pd.DataFrame(
        {"cluster": [relabel[l] for l in km.labels_], "cluster_ward": ward}, index=scores.index
    )
    names_cfg = settings.get("cluster", "names", default={}) or {}
    names = {i: names_cfg.get(i, names_cfg.get(str(i), f"類型{i}")) for i in range(1, k + 1)}
    return {"k": k, "selection": selection, "labels": labels, "names": names}


# ---------------------------------------------------------------- 実行

def run(settings: Settings, axes: AxesConfig) -> None:
    mart = settings.layer("mart")
    out = mart / "analysis"
    out.mkdir(exist_ok=True)
    long = pd.read_parquet(mart / "axes_long.parquet")
    norm = normalize(long, axes, settings)
    norm.to_parquet(out / "zscores.parquet", index=False)

    axis_ids = [a.axis_id for a in axes.axes]
    z = norm.pivot(index="area_code", columns="axis_id", values="z_clipped")[axis_ids]
    z.corr().to_csv(out / "correlation.csv")
    complete = z.dropna()
    log.info("統合分析の対象: %d / %d 地域（欠損・除外を含む地域は除く）", len(complete), len(z))
    meta: dict = {"n_areas": int(len(z)), "n_complete": int(len(complete)), "axes": axis_ids}

    if len(axis_ids) >= 2 and len(complete) >= 3:
        pca = run_pca(complete, settings, axes)
        pca["variance"].to_csv(out / "pca_variance.csv", index=False)
        pca["loadings"].to_csv(out / "pca_loadings.csv")
        pca["scores"].reset_index().to_parquet(out / "pca_scores.parquet", index=False)
        meta["pca"] = {"components": pca["pcs"], "names": pca["names"], "provisional": pca["provisional"]}
        cl = run_clustering(pca["scores"], settings)
        cl["selection"].to_csv(out / "cluster_selection.csv", index=False)
        cl["labels"].reset_index().to_parquet(out / "clusters.parquet", index=False)
        raw_wide = norm.pivot(index="area_code", columns="axis_id", values="value")[axis_ids]
        means = raw_wide.join(cl["labels"]["cluster"], how="inner").groupby("cluster").mean()
        zmeans = complete.join(cl["labels"]["cluster"]).groupby("cluster").mean().add_suffix("_z")
        sizes = cl["labels"]["cluster"].value_counts().rename("n_areas")
        means.join(zmeans).join(sizes).to_csv(out / "cluster_means.csv")
        meta["cluster"] = {"k": cl["k"], "names": {str(k): v for k, v in cl["names"].items()}}
    else:
        log.warning("軸が2つ未満、または完全な地域が3未満のため、主成分分析とクラスタリングを省略します")
    (out / "analysis_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
