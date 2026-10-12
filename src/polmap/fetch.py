"""第1段階: e-Stat API v3 から統計表を取得し、生データをそのまま保存する（raw層）。

raw/<statsDataId>/page_NNNN.json  … APIの応答そのもの
raw/<statsDataId>/values.parquet  … VALUE要素を文字列のまま表にしたもの
raw/<statsDataId>/classes.parquet … CLASS_INF（カテゴリー・地域の名称）
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

from .config import AxesConfig, Settings

log = logging.getLogger(__name__)

API_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"


def _as_list(x: Any) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def fetch_pages(app_id: str, stats_data_id: str, limit: int = 100000, session=None) -> Iterable[dict]:
    """NEXT_KEY をたどって全ページを取得する。"""
    session = session or requests.Session()
    start = 1
    while True:
        params = {
            "appId": app_id,
            "statsDataId": stats_data_id,
            "startPosition": start,
            "limit": limit,
            "metaGetFlg": "Y" if start == 1 else "N",
            "cntGetFlg": "N",
            "replaceSpChars": 0,  # 秘匿記号は整形段階で処理するので、そのまま受け取る
        }
        for attempt in range(4):
            try:
                resp = session.get(API_URL, params=params, timeout=120)
                resp.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 1))
        page = resp.json()
        result = page["GET_STATS_DATA"]["RESULT"]
        if int(result.get("STATUS", 0)) not in (0, 1, 2):
            raise RuntimeError(f"e-Stat API エラー ({stats_data_id}): {result.get('ERROR_MSG')}")
        yield page
        next_key = page["GET_STATS_DATA"]["STATISTICAL_DATA"].get("RESULT_INF", {}).get("NEXT_KEY")
        if not next_key:
            return
        start = int(next_key)


def pages_to_frames(pages: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """APIの応答を (values, classes) の表に変換する。値は文字列のまま。"""
    rows: list[dict] = []
    classes: list[dict] = []
    for page in pages:
        sd = page["GET_STATS_DATA"]["STATISTICAL_DATA"]
        for v in _as_list(sd.get("DATA_INF", {}).get("VALUE")):
            rows.append({k.lstrip("@") if k != "$" else "value": v[k] for k in v})
        for obj in _as_list(sd.get("CLASS_INF", {}).get("CLASS_OBJ")):
            for c in _as_list(obj.get("CLASS")):
                classes.append(
                    {
                        "dimension": obj["@id"],
                        "dimension_name": obj.get("@name", ""),
                        "code": c["@code"],
                        "name": c.get("@name", ""),
                        "level": c.get("@level", ""),
                        "parent": c.get("@parentCode", ""),
                    }
                )
    values = pd.DataFrame(rows).astype("string")
    cls = pd.DataFrame(classes, columns=["dimension", "dimension_name", "code", "name", "level", "parent"]).astype("string")
    return values, cls


def save_raw(out_dir: Path, pages: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("page_*.json"):
        old.unlink()
    for i, page in enumerate(pages, 1):
        (out_dir / f"page_{i:04d}.json").write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    values, classes = pages_to_frames(pages)
    values.to_parquet(out_dir / "values.parquet", index=False)
    classes.to_parquet(out_dir / "classes.parquet", index=False)


def run(settings: Settings, axes: AxesConfig, force: bool = False) -> None:
    raw_dir = settings.layer("raw")
    app_id = settings.app_id
    for tid in sorted(axes.tables):
        out = raw_dir / tid
        if (out / "values.parquet").exists() and not force:
            log.info("取得済みのためスキップ: %s", tid)
            continue
        if not app_id:
            raise RuntimeError(
                "e-Stat のアプリケーションIDがありません。環境変数 ESTAT_APP_ID を設定してください"
                f"（未取得の統計表: {tid}）"
            )
        log.info("取得中: %s", tid)
        pages = list(fetch_pages(app_id, tid, limit=settings.get("estat", "limit", default=100000)))
        save_raw(out, pages)
