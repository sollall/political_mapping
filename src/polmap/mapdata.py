"""第6段階: 境界ポリゴンと結合し、地図用データ（TopoJSON + 属性JSON）を出力する。

出力（settings.paths.web_data、既定 web/data/）:
  municipalities.topojson … 市区町村ポリゴン（簡略化済み、properties は code / name のみ）
  data.json               … 地域ごとの軸の値・zスコア・順位・分母・クラスター
  meta.json               … 軸・クラスターの説明
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
import shapely
import topojson
from shapely.geometry import mapping, shape

from .config import AxesConfig, Settings

log = logging.getLogger(__name__)

UNASSIGNED = "unassigned"


def load_boundary(settings: Settings) -> list[dict]:
    """N03 の GeoJSON を市区町村コードごとにまとめる（所属未定地などは unassigned）。"""
    path = settings.boundary_path
    if not path.exists():
        raise FileNotFoundError(
            f"境界データがありません: {path}\n国土数値情報「行政区域（N03）」2020年版の GeoJSON を置いてください"
        )
    gj = json.loads(path.read_text(encoding="utf-8"))
    groups: dict[str, list] = defaultdict(list)
    props: dict[str, dict] = {}
    for f in gj["features"]:
        p = f.get("properties") or {}
        code = p.get("N03_007")
        code = str(code).zfill(5) if code else None
        if settings.prefectures:
            pref = code[:2] if code else None
            if pref is None:
                # コードのない所属未定地は都道府県名で判定できないので、範囲内に含める
                pass
            elif pref not in settings.prefectures:
                continue
        key = code or UNASSIGNED
        if f.get("geometry") is None:
            continue
        groups[key].append(shape(f["geometry"]))
        if key not in props:
            city, town = p.get("N03_003") or "", p.get("N03_004") or ""
            name = f"{city}{town}" if city.endswith("市") and town.endswith("区") else (town or city)
            props[key] = {"pref": p.get("N03_001") or "", "name": name or "所属未定地"}
    out = []
    for key, geoms in groups.items():
        geom = shapely.union_all(geoms) if len(geoms) > 1 else geoms[0]
        out.append({"code": key, "geometry": geom, **props[key]})
    return out


def _num(x, digits=6):
    if x is None or (isinstance(x, float) and math.isnan(x)) or pd.isna(x):
        return None
    return round(float(x), digits)


def build_topology(features: list[dict], tolerance: float) -> dict:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"code": f["code"], "name": f["name"]}, "geometry": mapping(f["geometry"])}
            for f in features
        ],
    }
    topo = topojson.Topology(
        fc,
        prequantize=1e6,
        toposimplify=tolerance if tolerance else False,
        object_name="municipalities",
    )
    return json.loads(topo.to_json())


def run(settings: Settings, axes: AxesConfig) -> None:
    mart = settings.layer("mart")
    ana = mart / "analysis"
    out = settings.web_data_dir
    out.mkdir(parents=True, exist_ok=True)

    z = pd.read_parquet(ana / "zscores.parquet")
    meta_in = json.loads((ana / "analysis_meta.json").read_text(encoding="utf-8"))
    names_path = settings.layer("staging") / "areas.parquet"
    stat_names = (
        pd.read_parquet(names_path).set_index("area_code")["area_name"].to_dict() if names_path.exists() else {}
    )

    features = load_boundary(settings)
    boundary_codes = {f["code"] for f in features if f["code"] != UNASSIGNED}
    stat_codes = set(z["area_code"].unique())
    join_log = {
        "statistics_only": sorted(stat_codes - boundary_codes),
        "boundary_only": sorted(boundary_codes - stat_codes),
        "unassigned_polygons": int(any(f["code"] == UNASSIGNED for f in features)),
        "matched": len(stat_codes & boundary_codes),
    }
    (settings.layer("mart") / "join_log.json").write_text(json.dumps(join_log, ensure_ascii=False, indent=2), encoding="utf-8")
    if join_log["statistics_only"] or join_log["boundary_only"]:
        log.warning(
            "地域コードの不一致: 統計のみ %d 件, 境界のみ %d 件（詳細は mart/join_log.json）",
            len(join_log["statistics_only"]),
            len(join_log["boundary_only"]),
        )
    else:
        log.info("地域コードの照合: %d 件すべて一致", join_log["matched"])

    topo = build_topology(features, float(settings.get("map", "simplify_tolerance", default=0.0005)))
    (out / "municipalities.topojson").write_text(json.dumps(topo, separators=(",", ":")), encoding="utf-8")

    # ---- 属性
    axis_ids = [a.axis_id for a in axes.axes]
    by_area: dict[str, dict] = {}
    pref_of = {f["code"]: f["pref"] for f in features}
    bname = {f["code"]: f["name"] for f in features}
    for code in sorted(stat_codes | boundary_codes):
        by_area[code] = {
            "name": stat_names.get(code) or bname.get(code, code),
            "pref": pref_of.get(code, ""),
            "v": {},
        }
    for r in z.itertuples(index=False):
        by_area[r.area_code]["v"][r.axis_id] = [
            _num(r.value), _num(r.z, 4), None if pd.isna(r.rank) else int(r.rank),
            _num(r.denominator, 1), int(bool(r.small)), int(bool(r.excluded)),
        ]
    if (ana / "clusters.parquet").exists():
        for r in pd.read_parquet(ana / "clusters.parquet").itertuples(index=False):
            by_area[r.area_code]["cl"] = int(r.cluster)

    first = z.drop_duplicates("axis_id").set_index("axis_id")
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": settings.get("map", "title", default="政治地理マップ（国勢調査2020）"),
        "scope": settings.prefectures or "全国",
        "weighting": settings.get("normalize", "weighting", default="none"),
        "small_population": settings.get("small_population", default={}),
        "value_fields": ["value", "z", "rank", "denominator", "small", "excluded"],
        "axes": [
            {
                "id": a.axis_id,
                "name": a.name,
                "left": a.left,
                "right": a.right,
                "group": a.group,
                "formula": a.formula,
                "known_bias": a.known_bias,
                "mean": _num(first.at[a.axis_id, "mean"]),
                "std": _num(first.at[a.axis_id, "std"]),
                "rank_of": int(first.at[a.axis_id, "rank_of"]),
            }
            for a in axes.axes
            if a.axis_id in first.index
        ],
    }
    if "cluster" in meta_in:
        means = pd.read_csv(ana / "cluster_means.csv", index_col=0)
        meta["clusters"] = [
            {
                "id": int(k),
                "name": name,
                "n": int(means.at[int(k), "n_areas"]),
                "z": {ax: _num(means.at[int(k), f"{ax}_z"], 3) for ax in axis_ids},
            }
            for k, name in meta_in["cluster"]["names"].items()
        ]
    (out / "data.json").write_text(json.dumps({"areas": by_area}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("地図用データを出力: %s", out)
