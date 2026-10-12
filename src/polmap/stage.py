"""第2段階: 地域コード×カテゴリー×値の縦持ち形式に揃える（staging層）。

国勢調査の「-」は「該当数値なし」（0）なので 0 にする。
秘匿・不詳の記号（「X」「…」「***」など、それ以外の数値でない値）は欠損に変換し、
suppressed=True を立てる。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import AxesConfig, Settings

log = logging.getLogger(__name__)

NON_DIM_COLUMNS = {"area", "value", "unit", "annotation"}


ZERO_MARKS = ["-", "－", "―"]  # 該当数値なし（0）


def parse_values(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    txt = s.astype("string").str.replace(",", "", regex=False).str.strip()
    txt = txt.mask(txt.isin(ZERO_MARKS), "0")
    num = pd.to_numeric(txt, errors="coerce")
    return num.astype(float), num.isna()


def stage_table(values: pd.DataFrame, stats_data_id: str, year: int) -> pd.DataFrame:
    df = values.copy()
    if "area" not in df.columns:
        raise ValueError(f"{stats_data_id}: 地域（area）次元がない統計表は使えません")
    out = pd.DataFrame(
        {
            "stats_data_id": stats_data_id,
            "year": year,
            "area_code": df["area"].astype(str).str.zfill(5),
        }
    )
    for col in df.columns:
        if col not in NON_DIM_COLUMNS:
            out[col] = df[col].astype("string")
    out["raw_value"] = df["value"].astype("string")
    out["value"], out["suppressed"] = parse_values(df["value"])
    return out


def run(settings: Settings, axes: AxesConfig) -> None:
    raw_dir = settings.layer("raw")
    frames, areas = [], []
    for tid, table in sorted(axes.tables.items()):
        path = raw_dir / tid / "values.parquet"
        if not path.exists():
            raise FileNotFoundError(f"raw層にありません: {path}（先に fetch を実行）")
        staged = stage_table(pd.read_parquet(path), tid, table.year)
        log.info("%s: %d 行（秘匿・欠損 %d）", tid, len(staged), int(staged["suppressed"].sum()))
        frames.append(staged)
        cls_path = raw_dir / tid / "classes.parquet"
        if cls_path.exists():
            cls = pd.read_parquet(cls_path)
            a = cls[cls["dimension"] == "area"][["code", "name"]].rename(columns={"code": "area_code", "name": "area_name"})
            a["year"] = table.year
            areas.append(a)
    values = pd.concat(frames, ignore_index=True)
    dim_cols = [c for c in values.columns if c not in {"stats_data_id", "year", "area_code", "raw_value", "value", "suppressed"}]
    values[dim_cols] = values[dim_cols].astype("string")
    out = settings.layer("staging")
    values.to_parquet(out / "values.parquet", index=False)
    if areas:
        a = pd.concat(areas, ignore_index=True)
        a["area_code"] = a["area_code"].astype(str).str.zfill(5)
        # 2020年の名称を優先
        a = a.sort_values("year", ascending=False).drop_duplicates("area_code")[["area_code", "area_name"]]
    else:
        a = pd.DataFrame({"area_code": pd.Series(dtype=str), "area_name": pd.Series(dtype=str)})
    a.to_parquet(out / "areas.parquet", index=False)


def match(df: pd.DataFrame, flt: dict[str, list[str]]) -> pd.Series:
    """filter（次元→コードのリスト）に一致する行のマスク。"""
    mask = pd.Series(True, index=df.index)
    for dim, codes in flt.items():
        if dim not in df.columns:
            return pd.Series(False, index=df.index)
        mask &= df[dim].isin(codes).fillna(False).astype(bool)
    return mask


def sum_quantity(
    df: pd.DataFrame, stats_data_id: str, flt: dict[str, list[str]], suppressed_policy: str = "missing"
) -> pd.DataFrame:
    """地域ごとに filter に一致するセルを合計する。秘匿セルの数も返す。

    suppressed_policy="missing": 秘匿セルを1つでも含む合計は欠損にする（既定）
    suppressed_policy="skip":    秘匿セルを除いて合計する
    """
    sub = df[(df["stats_data_id"] == stats_data_id)]
    sub = sub[match(sub, flt)]
    g = sub.groupby("area_code")
    res = pd.DataFrame(
        {
            "value": g["value"].sum(min_count=1),
            "suppressed": g["suppressed"].sum().astype(int),
            "cells": g.size(),
        }
    )
    if suppressed_policy == "missing":
        res.loc[res["suppressed"] > 0, "value"] = np.nan
    return res
