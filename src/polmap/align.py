"""第3段階: 境界の揃え込み。

- 全国・都道府県・政令市（市全体）・特別区部などの集計レコードを市区町村表から外す
  （集計レコードは合計の整合チェック用に aggregates.parquet へ分けて保存）
- 対象都道府県で絞り込む
- 2015年など過去の値を2020年境界に組み替える（合併・分割の対応表を使う）
"""
from __future__ import annotations

import logging

import pandas as pd

from .config import AxesConfig, Settings

log = logging.getLogger(__name__)

# 2020年10月1日時点の政令指定都市（市全体のコード）。行政区を集計単位とするため除外する。
DESIGNATED_CITIES = {
    "01100": "札幌市", "04100": "仙台市", "11100": "さいたま市", "12100": "千葉市",
    "14100": "横浜市", "14130": "川崎市", "14150": "相模原市", "15100": "新潟市",
    "22100": "静岡市", "22130": "浜松市", "23100": "名古屋市", "26100": "京都市",
    "27100": "大阪市", "27140": "堺市", "28100": "神戸市", "33100": "岡山市",
    "34100": "広島市", "40100": "北九州市", "40130": "福岡市", "43100": "熊本市",
}
# 東京都区部（23区の合計）。23区それぞれを市区町村と同格に扱うため除外する。
AGGREGATE_EXTRA = {"13100": "特別区部"}


def classify(code: str, extra_aggregates: set[str] = frozenset()) -> str:
    """地域コードを national / prefecture / aggregate / municipality / other に分類する。"""
    if not (len(code) == 5 and code.isdigit()):
        return "other"  # 「市部」「郡部」など
    if code == "00000":
        return "national"
    if code.endswith("000"):
        return "prefecture"
    if code in DESIGNATED_CITIES or code in AGGREGATE_EXTRA or code in extra_aggregates:
        return "aggregate"
    return "municipality"


def load_changes(settings: Settings) -> pd.DataFrame:
    """合併・分割の対応表: from_code, to_code, weight（分割時の按分比。省略時は1）"""
    path = settings.boundary_changes_path
    if path is None or not path.exists():
        return pd.DataFrame(columns=["from_code", "to_code", "weight"])
    ch = pd.read_csv(path, dtype={"from_code": str, "to_code": str}, comment="#")
    ch["from_code"] = ch["from_code"].str.zfill(5)
    ch["to_code"] = ch["to_code"].str.zfill(5)
    ch["weight"] = pd.to_numeric(ch.get("weight", 1.0), errors="coerce").fillna(1.0)
    bad = ch.groupby("from_code")["weight"].sum().round(6)
    if (bad != 1).any():
        raise ValueError(f"対応表の weight の合計が1でない旧コードがあります: {list(bad[bad != 1].index)}")
    return ch


def remap(df: pd.DataFrame, changes: pd.DataFrame) -> pd.DataFrame:
    """旧境界の値を2020年境界に組み替える。対応表にないコードはそのまま。"""
    if changes.empty:
        return df
    hit = df["area_code"].isin(changes["from_code"])
    if not hit.any():
        return df
    moved = df[hit].merge(changes, left_on="area_code", right_on="from_code")
    moved["value"] = moved["value"] * moved["weight"]
    moved["area_code"] = moved["to_code"]
    moved = moved.drop(columns=["from_code", "to_code", "weight"])
    combined = pd.concat([df[~hit], moved], ignore_index=True)
    keys = [c for c in combined.columns if c not in {"value", "suppressed", "raw_value"}]
    out = (
        combined.groupby(keys, dropna=False, sort=False)
        .agg(value=("value", lambda s: s.sum(min_count=1)), suppressed=("suppressed", "max"), raw_value=("raw_value", "first"))
        .reset_index()
    )
    return out


def run(settings: Settings, axes: AxesConfig) -> None:
    staging = settings.layer("staging")
    values = pd.read_parquet(staging / "values.parquet")
    extra = {str(c).zfill(5) for c in settings.get("scope", "extra_aggregate_codes", default=[]) or []}
    values["area_kind"] = values["area_code"].map(lambda c: classify(c, extra))

    aggregates = values[values["area_kind"].isin(["national", "prefecture", "aggregate"])]
    muni = values[values["area_kind"] == "municipality"].drop(columns="area_kind")
    log.info("集計レコード %d 行を分離、市区町村 %d 行", len(aggregates), len(muni))

    changes = load_changes(settings)
    current_year = int(settings.get("scope", "year", default=2020))
    parts = []
    for year, part in muni.groupby("year"):
        if int(year) != current_year:
            before = part["area_code"].nunique()
            part = remap(part, changes)
            log.info("%s年 → %d年境界: %d → %d 地域", year, current_year, before, part["area_code"].nunique())
        parts.append(part)
    muni = pd.concat(parts, ignore_index=True)

    if settings.prefectures:
        muni = muni[muni["area_code"].str[:2].isin(settings.prefectures)]
        aggregates = aggregates[
            aggregates["area_code"].str[:2].isin(settings.prefectures) | (aggregates["area_code"] == "00000")
        ]
    muni.to_parquet(staging / "aligned.parquet", index=False)
    aggregates.drop(columns="area_kind").to_parquet(staging / "aggregates.parquet", index=False)
