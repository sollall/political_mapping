"""合成データ（デモ県・サンプル県）を作り、e-Stat API の応答と N03 境界の形式で保存する。

e-Stat のアプリケーションIDやネットワークがなくても、パイプライン全体と地図を確認できる。
値は乱数で作った架空のもので、実在の地域とは関係ない。
"""
from __future__ import annotations

import json

import numpy as np

from .config import Settings
from .config import load_axes
from .fetch import save_raw, table_filters

PREFS = {"98": "サンプル県", "99": "デモ県"}
WARDS = ["北区", "中央区", "南区"]
CITY = ("99100", "デモ市")
MERGED_2015 = ("99299", "99210")  # 2015年の旧町 → 2020年の合併後の市
GRID = (9, 8)  # 列 × 行
TIME_2020, TIME_2015 = "2020000000", "2015000000"


def _municipalities(rng: np.random.Generator) -> list[dict]:
    cols, rows = GRID
    out = []
    for r in range(rows):
        for c in range(cols):
            pref = "98" if c < 4 else "99"
            out.append({"pref": pref, "col": c, "row": r})
    # 区のある市（デモ県の中央）
    city_cells = [(6, 3), (6, 4), (7, 4)]
    counters = {"98": 200, "99": 200}
    unassigned_cell = (4, 7)
    munis = []
    for m in out:
        cell = (m["col"], m["row"])
        if cell == unassigned_cell:
            m["code"] = None
            m["name"] = "所属未定地"
            munis.append(m)
            continue
        if cell in city_cells:
            i = city_cells.index(cell)
            m["code"] = f"99{101 + i}"
            m["city"] = CITY[1]
            m["name"] = WARDS[i]
        else:
            counters[m["pref"]] += 1
            n = counters[m["pref"]]
            m["code"] = f"{m['pref']}{n:03d}"
            kind = "市" if n % 4 == 1 else "町" if n % 4 != 0 else "村"
            head = "北南東西中上下新大小"[(n * 3 + int(m["pref"])) % 10]
            tail = "山川田野原島浜沢森井"[(n * 7) % 10]
            m["name"] = f"{head}{tail}{kind}"
        munis.append(m)
    return munis


def _shares(rng, logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - logits.max())
    return e / e.sum()


def _synth(rng: np.random.Generator, munis: list[dict]) -> None:
    cx, cy = 6.3, 3.7
    for m in munis:
        if m["code"] is None:
            continue
        dist = np.hypot(m["col"] - cx, m["row"] - cy)
        urban = 1.8 - 0.45 * dist + rng.normal(0, 0.35)
        if m["code"].startswith("991"):
            urban += 1.0
        m["urban"] = urban
        m["family"] = rng.normal(0, 1)
        m["pop"] = int(np.exp(9.2 + 0.9 * urban + rng.normal(0, 0.5)))
    # 小人口の村をいくつか作る
    for m in munis:
        if m["code"] and m["code"].endswith(("208", "215", "231")):
            m["pop"] = int(rng.integers(250, 700))


def _split(rng, total: int, logits) -> list[int]:
    return list(rng.multinomial(total, _shares(rng, np.asarray(logits, float))))


def _tables(rng: np.random.Generator, munis: list[dict]) -> dict[str, dict]:
    """統計表ID → {area_code: {cat01: count}}（2020年は市区町村のみ。集計は後で足す）"""
    T: dict[str, dict] = {k: {} for k in ["D0001", "D0002", "D0003", "D0004", "D0005", "D0006", "D0007"]}
    for m in munis:
        code = m.get("code")
        if not code:
            continue
        u, f, P = m["urban"], m["family"], m["pop"]
        unk = rng.uniform(0.002, 0.04 + 0.03 * max(u, 0))
        age = _split(rng, P, [0.2 * f, 1.6 + 0.35 * u, 0.9 - 0.45 * u - 0.15 * f, np.log(unk) + 1])
        T["D0001"][code] = {"000": P, "001": age[0], "002": age[1], "003": age[2], "004": age[3]}
        P15 = int(P / (1 + 0.035 * u - 0.03 + rng.normal(0, 0.012)))
        T["D0002"][code] = {"000": P15}
        W = int(P * rng.uniform(0.45, 0.55))
        ind = _split(rng, W, [-0.4 - 1.1 * u, 0.6 - 0.3 * u + 0.4 * f + rng.normal(0, .3), 1.2 + 0.4 * u, np.log(unk)])
        T["D0003"][code] = {"00": W, "01": ind[0], "02": ind[1], "03": ind[2], "04": ind[3]}
        G = int(P * 0.8)
        edu = _split(rng, G, [-1.2 + 0.55 * u + rng.normal(0, .2), 0.6, np.log(unk) + 0.6])
        T["D0004"][code] = {"0": G, "1": edu[0], "2": edu[1], "9": edu[2]}
        H = int(P / (2.6 - 0.25 * u + 0.15 * f))
        ten = _split(rng, H, [1.3 - 0.75 * u + 0.25 * f, 0, np.log(unk)])
        T["D0005"][code] = {"0": H, "1": ten[0], "2": ten[1], "9": ten[2]}
        com = _split(rng, W, [0.9 - 0.25 * abs(u - 0.8) * 2 - (0.6 if code.startswith("991") else 0) + rng.normal(0, .4),
                              0.1 + 0.3 * u * (1 if not code.startswith("991") else 0.3), np.log(unk)])
        T["D0006"][code] = {"0": W, "1": com[0], "2": com[1], "9": com[2]}
        hh = _split(rng, H, [-0.5 + 0.55 * u - 0.3 * f, 0.8 + 0.1 * f, -0.6 - 0.4 * u + 0.5 * f, np.log(unk)])
        T["D0007"][code] = {"0": H, "1": hh[0], "2": hh[1], "3": hh[2], "9": hh[3]}
    # 2015年は合併前の旧町に分かれていた
    old, new = MERGED_2015
    if new in T["D0002"]:
        total = T["D0002"][new]["000"]
        part = int(total * 0.37)
        T["D0002"][new] = {"000": total - part}
        T["D0002"][old] = {"000": part}
    return T


def _with_aggregates(table: dict[str, dict]) -> dict[str, dict]:
    out = dict(table)

    def add(dst: str, src: dict):
        acc = out.setdefault(dst, {})
        for k, v in src.items():
            acc[k] = acc.get(k, 0) + v

    for code, cells in table.items():
        if code.startswith("991"):
            add(CITY[0], cells)
        add(code[:2] + "000", cells)
        add("00000", cells)
    return out


def _api_page(tid: str, title: str, time: str, table: dict[str, dict], names: dict[str, str],
              cat_names: dict[str, str], suppress: set[tuple[str, str]]) -> dict:
    values = []
    for area, cells in sorted(table.items()):
        for cat, v in cells.items():
            raw = "X" if (area, cat) in suppress else str(int(v))
            values.append({"@tab": "020", "@cat01": cat, "@area": area, "@time": time, "@unit": "人", "$": raw})
    class_obj = [
        {"@id": "tab", "@name": "表章項目", "CLASS": {"@code": "020", "@name": "人数", "@level": ""}},
        {"@id": "cat01", "@name": "区分", "CLASS": [{"@code": k, "@name": v, "@level": "1"} for k, v in cat_names.items()]},
        {"@id": "area", "@name": "地域", "CLASS": [{"@code": c, "@name": names.get(c, c), "@level": "2"} for c in sorted(table)]},
        {"@id": "time", "@name": "時間軸", "CLASS": {"@code": time, "@name": time[:4] + "年", "@level": "1"}},
    ]
    return {
        "GET_STATS_DATA": {
            "RESULT": {"STATUS": 0, "ERROR_MSG": "正常に終了しました。（デモ用の合成データ）"},
            "PARAMETER": {"STATS_DATA_ID": tid},
            "STATISTICAL_DATA": {
                "RESULT_INF": {"TOTAL_NUMBER": len(values)},
                "TABLE_INF": {"@id": tid, "TITLE": title},
                "CLASS_INF": {"CLASS_OBJ": class_obj},
                "DATA_INF": {"VALUE": values},
            },
        }
    }


CATS = {
    "D0001": ("年齢（3区分）別人口", {"000": "総数", "001": "15歳未満", "002": "15〜64歳", "003": "65歳以上", "004": "不詳"}),
    "D0002": ("人口（2015年）", {"000": "総数"}),
    "D0003": ("産業（大分類）別就業者数", {"00": "総数", "01": "第1次産業", "02": "第2次産業", "03": "第3次産業", "04": "分類不能"}),
    "D0004": ("最終卒業学校別人口", {"0": "総数", "1": "大学・大学院", "2": "その他", "9": "不詳"}),
    "D0005": ("住宅の所有関係別世帯数", {"0": "総数", "1": "持ち家", "2": "借家", "9": "不詳"}),
    "D0006": ("従業地別就業者数", {"0": "総数", "1": "自市区町村で従業", "2": "他市区町村で従業", "9": "不詳"}),
    "D0007": ("世帯の家族類型別世帯数", {"0": "総数", "1": "単独世帯", "2": "核家族世帯", "3": "その他の世帯", "9": "不詳"}),
}


def _boundary(munis: list[dict]) -> dict:
    feats = []
    x0, y0, s = 139.0, 36.0, 0.1
    for m in munis:
        c, r = m["col"], m["row"]
        ring = [[x0 + c * s, y0 - r * s], [x0 + (c + 1) * s, y0 - r * s], [x0 + (c + 1) * s, y0 - (r + 1) * s],
                [x0 + c * s, y0 - (r + 1) * s], [x0 + c * s, y0 - r * s]]
        props = {
            "N03_001": PREFS[m["pref"]], "N03_002": None,
            "N03_003": m.get("city"), "N03_004": m["name"], "N03_007": m["code"],
        }
        feats.append({"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}})
    return {"type": "FeatureCollection", "name": "N03-demo", "features": feats}


def generate(settings: Settings, seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    munis = _municipalities(rng)
    _synth(rng, munis)
    tables = _tables(rng, munis)
    names = {m["code"]: (m.get("city", "") + m["name"]) for m in munis if m["code"]}
    names.update({p + "000": n for p, n in PREFS.items()})
    names.update({"00000": "全国", CITY[0]: CITY[1], MERGED_2015[0]: "旧デモ町"})
    # 小人口の村の第1次産業を秘匿にする
    small = sorted((m for m in munis if m.get("code")), key=lambda m: m["pop"])[:2]
    suppress = {(m["code"], "01") for m in small}
    raw = settings.layer("raw")
    axes = load_axes(settings.axes_path)
    for tid, (title, cats) in CATS.items():
        time = TIME_2015 if tid == "D0002" else TIME_2020
        page = _api_page(tid, title, time, _with_aggregates(tables[tid]), names, cats,
                         suppress if tid == "D0003" else set())
        save_raw(raw / tid, [page])
        # fetch 段階が「取得済み」と判断できるよう、軸定義に対応する取得条件も保存する
        (raw / tid / "request.json").write_text(json.dumps(table_filters(axes, tid)), encoding="utf-8")
    settings.boundary_path.parent.mkdir(parents=True, exist_ok=True)
    settings.boundary_path.write_text(json.dumps(_boundary(munis), ensure_ascii=False), encoding="utf-8")
