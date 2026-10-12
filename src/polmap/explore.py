"""統計表IDとカテゴリーコードを調べるための e-Stat 検索（polmap search / polmap meta）。

axes.yaml の「TBD」を埋めるときに使う。
"""
from __future__ import annotations

from typing import Any

import requests

BASE_URL = "https://api.e-stat.go.jp/rest/3.0/app/json"
CENSUS_STATS_CODE = "00200521"  # 政府統計コード: 国勢調査
COLLECT_AREA = {"national": 1, "prefecture": 2, "municipality": 3}


def _as_list(x: Any) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _text(x: Any) -> str:
    """e-Stat の要素は文字列か {"$": ...} のどちらかで返る。"""
    if isinstance(x, dict):
        return str(x.get("$", ""))
    return "" if x is None else str(x)


def _get(endpoint: str, params: dict, session=None) -> dict:
    session = session or requests.Session()
    resp = session.get(f"{BASE_URL}/{endpoint}", params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def search_tables(app_id: str, words: str, year: str = "2020", area: str = "municipality",
                  limit: int = 50, session=None) -> list[dict]:
    """国勢調査の統計表をキーワードで探す。"""
    params = {
        "appId": app_id,
        "statsCode": CENSUS_STATS_CODE,
        "searchWord": words,
        "surveyYears": year,
        "limit": limit,
    }
    if area in COLLECT_AREA:
        params["collectArea"] = COLLECT_AREA[area]
    page = _get("getStatsList", params, session)
    root = page["GET_STATS_LIST"]
    status = int(root["RESULT"].get("STATUS", 0))
    if status not in (0, 1):
        raise RuntimeError(f"e-Stat API エラー: {root['RESULT'].get('ERROR_MSG')}")
    rows = []
    for t in _as_list(root.get("DATALIST_INF", {}).get("TABLE_INF")):
        rows.append(
            {
                "id": t.get("@id", ""),
                "title": _text(t.get("TITLE")),
                "statistics": _text(t.get("STATISTICS_NAME")),
                "survey_date": _text(t.get("SURVEY_DATE")),
                "rows": _text(t.get("OVERALL_TOTAL_NUMBER")),
            }
        )
    return rows


def table_meta(app_id: str, stats_data_id: str, session=None) -> dict:
    """統計表の次元とカテゴリーコードを返す: {"title": ..., "dimensions": [{id, name, classes:[{code,name,level}]}]}"""
    page = _get("getMetaInfo", {"appId": app_id, "statsDataId": stats_data_id}, session)
    root = page["GET_META_INFO"]
    status = int(root["RESULT"].get("STATUS", 0))
    if status != 0:
        raise RuntimeError(f"e-Stat API エラー ({stats_data_id}): {root['RESULT'].get('ERROR_MSG')}")
    meta = root["METADATA_INF"]
    dims = []
    for obj in _as_list(meta.get("CLASS_INF", {}).get("CLASS_OBJ")):
        dims.append(
            {
                "id": obj.get("@id", ""),
                "name": obj.get("@name", ""),
                "classes": [
                    {"code": c.get("@code", ""), "name": c.get("@name", ""), "level": c.get("@level", "")}
                    for c in _as_list(obj.get("CLASS"))
                ],
            }
        )
    return {"id": stats_data_id, "title": _text(meta.get("TABLE_INF", {}).get("TITLE")), "dimensions": dims}


def format_search(rows: list[dict]) -> str:
    if not rows:
        return "見つかりませんでした。キーワードを変えるか --area all を試してください。"
    lines = [f"{len(rows)} 件"]
    for r in rows:
        lines.append(f"{r['id']}  {r['title']}  （{r['statistics']}、{r['rows']} 行）")
    return "\n".join(lines)


def format_meta(meta: dict, max_areas: int = 5) -> str:
    lines = [f"{meta['id']}  {meta['title']}"]
    for d in meta["dimensions"]:
        lines.append(f"\n[{d['id']}] {d['name']}（{len(d['classes'])} 件）")
        classes = d["classes"]
        if d["id"] == "area" and len(classes) > max_areas:
            shown = classes[:max_areas]
            lines += [f"  {c['code']}  {c['name']}" for c in shown]
            lines.append(f"  …ほか {len(classes) - max_areas} 件")
            continue
        for c in classes:
            indent = "  " * max(1, int(c["level"] or 1))
            lines.append(f"{indent}{c['code']}  {c['name']}")
    return "\n".join(lines)
