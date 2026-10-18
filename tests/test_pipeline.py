import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from polmap import align, analyze, compute, demo, fetch, stage
from polmap.cli import run_pipeline
from polmap.config import ConfigError, load_axes, load_settings, parse_axis

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 設定

def _axis(**kw):
    base = {
        "axis_id": "family_single", "name": "家族 ↔ 単独", "group": "家族", "formula": "ratio",
        "stats_data_id": "T1", "numerator": {"cat01": ["1"]}, "denominator": {"cat01": ["1", "2"]},
    }
    base.update(kw)
    return base


def test_parse_axis_ok():
    a = parse_axis(_axis())
    assert a.left == "家族" and a.right == "単独"
    assert a.terms[0].numerator.filter == {"cat01": ["1"]}


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"axis_id": "Bad-Id"}, "スラッグ"),
        ({"name": "家族と単独"}, "A ↔ B"),
        ({"formula": "log"}, "formula"),
        ({"group": ""}, "group"),
        ({"formula": "difference"}, "terms"),
    ],
)
def test_parse_axis_errors(kw, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_axis(_axis(**kw))


def test_unregistered_table(tmp_path):
    p = tmp_path / "axes.yaml"
    p.write_text(yaml.safe_dump({"tables": {}, "axes": [_axis()]}, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="tables"):
        load_axes(p)


def test_repo_configs_load():
    s = load_settings(ROOT / "config/settings.yaml")
    assert s.prefectures == []  # 既定は全国
    real = load_axes(s.axes_path)
    assert len(real.axes) == 19  # Democracy 4 の有権者グループに基づく16軸 + 試作の3軸
    assert len(real.placeholders()) == 6  # 試作の3軸だけ未記入（統計表3件 + 軸3件）
    d = load_axes(ROOT / "config/demo/axes.yaml")
    assert len(d.axes) == 8
    assert d.placeholders() == []


def test_fetch_refuses_placeholders(tmp_path):
    s = load_settings(ROOT / "config/settings.yaml")
    p = tmp_path / "axes.yaml"
    p.write_text(yaml.safe_dump({"tables": {"TBD_x": {"year": 2020}}, "axes": [_axis(stats_data_id="TBD_x")]},
                                allow_unicode=True), encoding="utf-8")
    with pytest.raises(RuntimeError, match="TBD"):
        fetch.run(s, load_axes(p))


# ---------------------------------------------------------------- 取得・整形

class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params, timeout):
        self.calls.append(params)
        return FakeResp(self.pages[len(self.calls) - 1])


def _page(values, next_key=None):
    ri = {"NEXT_KEY": next_key} if next_key else {}
    return {"GET_STATS_DATA": {"RESULT": {"STATUS": 0}, "STATISTICAL_DATA": {"RESULT_INF": ri, "DATA_INF": {"VALUE": values}}}}


def test_fetch_follows_next_key():
    v1 = [{"@cat01": "1", "@area": "13101", "$": "10"}]
    v2 = [{"@cat01": "2", "@area": "13101", "$": "-"}]
    sess = FakeSession([_page(v1, next_key=2), _page(v2)])
    pages = list(fetch.fetch_pages("APPID", "T1", limit=1, session=sess))
    assert [c["startPosition"] for c in sess.calls] == [1, 2]
    values, _ = fetch.pages_to_frames(pages)
    assert list(values["value"]) == ["10", "-"]


def test_stage_marks_suppressed():
    raw = pd.DataFrame({"cat01": ["1", "2", "3", "4"], "area": ["1101", "01101", "01101", "01101"],
                        "value": ["1,234", "-", "…", "X"]})
    s = stage.stage_table(raw, "T1", 2020)
    assert s["area_code"].tolist()[0] == "01101"
    assert s["value"].tolist()[0] == 1234
    assert s["suppressed"].tolist() == [False, False, True, True]  # 「-」は0、「…」「X」は欠損
    assert s["value"].tolist()[1] == 0


# ---------------------------------------------------------------- 境界の揃え込み

def test_classify():
    assert align.classify("00000") == "national"
    assert align.classify("13000") == "prefecture"
    assert align.classify("14100") == "aggregate"  # 横浜市（市全体）
    assert align.classify("13100") == "aggregate"  # 特別区部
    assert align.classify("14101") == "municipality"  # 横浜市鶴見区
    assert align.classify("13101") == "municipality"  # 千代田区
    assert align.classify("1A000") == "other"


def test_remap_merges_and_splits():
    df = pd.DataFrame({
        "stats_data_id": "T", "year": 2015, "cat01": "0",
        "area_code": ["10001", "10002", "10003"], "value": [100.0, 50.0, 80.0],
        "suppressed": False, "raw_value": "x",
    })
    changes = pd.DataFrame({"from_code": ["10002", "10003", "10003"], "to_code": ["10001", "10001", "10004"],
                            "weight": [1.0, 0.25, 0.75]})
    out = align.remap(df, changes).set_index("area_code")["value"]
    assert out["10001"] == pytest.approx(170)
    assert out["10004"] == pytest.approx(60)
    assert out.sum() == pytest.approx(230)


# ---------------------------------------------------------------- 指標計算

def _frame():
    rows = []
    for area, (a, b, c, unk, p15) in {"01101": (30, 70, 0, 5, 90), "01102": (10, 30, 10, 0, 60)}.items():
        for cat, v in zip(["1", "2", "3", "9"], [a, b, c, unk]):
            rows.append(("T1", area, cat, float(v), False))
        rows.append(("P15", area, "0", float(p15), False))
        rows.append(("T1", area, "0", float(a + b + c + unk), False))
    return pd.DataFrame(rows, columns=["stats_data_id", "area_code", "cat01", "value", "suppressed"])


def test_compute_formulas():
    df = _frame()
    r = compute.compute_axis(df, parse_axis(_axis(denominator={"cat01": ["1", "2", "3"]}, unknown={"cat01": ["9"]}))).set_index("area_code")
    assert r.at["01101", "value"] == pytest.approx(0.3)
    assert r.at["01101", "unknown_rate"] == pytest.approx(5 / 105)
    d = compute.compute_axis(df, parse_axis(_axis(formula="difference", terms=[
        {"numerator": {"cat01": ["2"]}, "denominator": {"cat01": ["1", "2", "3"]}},
        {"numerator": {"cat01": ["1"]}, "denominator": {"cat01": ["1", "2", "3"]}},
    ]))).set_index("area_code")
    assert d.at["01102", "value"] == pytest.approx(0.4)
    g = compute.compute_axis(df, parse_axis(_axis(formula="growth", current={"filter": {"cat01": ["0"]}},
                                                  base={"stats_data_id": "P15", "filter": {"cat01": ["0"]}}))).set_index("area_code")
    assert g.at["01102", "value"] == pytest.approx(50 / 60 - 1)


def test_suppressed_policy():
    df = _frame()
    df.loc[(df.area_code == "01101") & (df.cat01 == "2"), ["value", "suppressed"]] = [np.nan, True]
    ax = parse_axis(_axis())  # 分子 cat1=30、分母 cat1+cat2（cat2 が秘匿）
    assert np.isnan(compute.compute_axis(df, ax, "missing").set_index("area_code").at["01101", "value"])
    assert compute.compute_axis(df, ax, "skip").set_index("area_code").at["01101", "value"] == 1.0


def test_eb_smooth_shrinks_small_areas_more():
    num = pd.Series([50, 500, 1, 9])
    den = pd.Series([100, 1000, 10, 10])
    sm = analyze.eb_smooth(num, den)
    m = num.sum() / den.sum()
    assert abs(sm[2] - m) < abs(0.1 - m)  # 小さい地域は全体比率に寄る
    assert abs(sm[1] - 0.5) < abs(sm[2] - 0.1)


# ---------------------------------------------------------------- デモで全段階

@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("demo")
    cfg = yaml.safe_load((ROOT / "config/demo/settings.yaml").read_text(encoding="utf-8"))
    cfg["root"] = str(tmp)
    cfg["axes"] = str(ROOT / "config/demo/axes.yaml")
    cfg["paths"]["boundary_changes"] = str(ROOT / "config/demo/boundary_changes.csv")
    path = tmp / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    s = load_settings(path)
    demo.generate(s)
    overall = run_pipeline(path)
    return s, overall


def test_demo_end_to_end(demo_run):
    s, overall = demo_run
    assert overall == "warn"  # 秘匿セルによる合計の不一致と不詳率の警告のみ
    report = json.loads((s.report_dir / "validation.json").read_text(encoding="utf-8"))
    status = {c["name"]: c["status"] for c in report["checks"]}
    assert status["地域コードの照合"] == "pass"
    assert status["比率の範囲"] == "pass"
    assert status["合計の整合"] == "warn"
    assert (s.report_dir / "validation.md").exists()


def test_demo_layers(demo_run):
    s, _ = demo_run
    for p in ["raw/D0001/values.parquet", "staging/values.parquet", "staging/aligned.parquet",
              "mart/axes_wide.parquet", "mart/analysis/zscores.parquet", "mart/analysis/cluster_means.csv",
              "mart/analysis/clusters.parquet"]:
        assert (s.data_dir / p).exists(), p
    wide = pd.read_parquet(s.data_dir / "mart/axes_wide.parquet")
    codes = set(wide["area_code"])
    assert "99100" not in codes  # 区のある市の市全体は除外
    assert "99299" not in codes  # 2015年の旧町は2020年境界に組み替え済み
    assert {"99101", "99102", "99103"} <= codes


def test_demo_analysis(demo_run):
    s, _ = demo_run
    meta = json.loads((s.data_dir / "mart/analysis/analysis_meta.json").read_text(encoding="utf-8"))
    assert "pca" not in meta
    assert 4 <= meta["cluster"]["k"] <= 8
    z = pd.read_parquet(s.data_dir / "mart/analysis/zscores.parquet")
    assert z["z_clipped"].abs().max() <= 3
    assert z.loc[z["excluded"], "z"].isna().all()


def test_demo_map_outputs(demo_run):
    s, _ = demo_run
    topo = json.loads((s.web_data_dir / "municipalities.topojson").read_text())
    geoms = topo["objects"]["municipalities"]["geometries"]
    assert any(g["properties"]["code"] == "unassigned" for g in geoms)
    data = json.loads((s.web_data_dir / "data.json").read_text(encoding="utf-8"))["areas"]
    assert data["99101"]["name"] == "デモ市北区"
    meta = json.loads((s.web_data_dir / "meta.json").read_text(encoding="utf-8"))
    assert [a["id"] for a in meta["axes"]][0] == "population_aging"
    assert all(len(v) == 6 for v in data["99101"]["v"].values())


def test_rerun_from_middle_stage(demo_run):
    s, _ = demo_run
    assert run_pipeline(s.root / "settings.yaml", start="analyze", end="validate") == "warn"


def test_dotenv(tmp_path, monkeypatch):
    from polmap.config import load_dotenv

    monkeypatch.delenv("ESTAT_APP_ID", raising=False)
    monkeypatch.delenv("POLMAP_KEEP", raising=False)
    (tmp_path / ".env").write_text('# comment\nexport ESTAT_APP_ID="abc123"\nPOLMAP_KEEP=fromfile\n', encoding="utf-8")
    monkeypatch.setenv("POLMAP_KEEP", "fromenv")
    load_dotenv(tmp_path / ".env")
    import os

    assert os.environ["ESTAT_APP_ID"] == "abc123"
    assert os.environ["POLMAP_KEEP"] == "fromenv"  # 既存の環境変数が優先


# ---------------------------------------------------------------- e-Stat 検索（polmap search / meta）

class FakeGetSession:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def get(self, url, params, timeout):
        self.calls.append((url, params))
        return FakeResp(self.payload)


def test_search_tables():
    from polmap import explore

    payload = {"GET_STATS_LIST": {"RESULT": {"STATUS": 0}, "DATALIST_INF": {"TABLE_INF": {
        "@id": "0003445078", "TITLE": {"@no": "1", "$": "年齢（5歳階級）別人口"},
        "STATISTICS_NAME": "令和2年国勢調査", "SURVEY_DATE": "202010", "OVERALL_TOTAL_NUMBER": "100"}}}}
    sess = FakeGetSession(payload)
    rows = explore.search_tables("APPID", "年齢", session=sess)
    assert rows[0]["id"] == "0003445078" and rows[0]["title"] == "年齢（5歳階級）別人口"
    url, params = sess.calls[0]
    assert url.endswith("getStatsList") and params["statsCode"] == "00200521" and params["collectArea"] == 3
    assert "0003445078" in explore.format_search(rows)


def test_table_meta():
    from polmap import explore

    payload = {"GET_META_INFO": {"RESULT": {"STATUS": 0}, "METADATA_INF": {
        "TABLE_INF": {"TITLE": "年齢別人口"},
        "CLASS_INF": {"CLASS_OBJ": [
            {"@id": "cat01", "@name": "年齢", "CLASS": [{"@code": "000", "@name": "総数", "@level": "1"},
                                                     {"@code": "065", "@name": "65歳以上", "@level": "2"}]},
            {"@id": "area", "@name": "地域", "CLASS": [{"@code": f"13{i:03d}", "@name": f"区{i}", "@level": "2"} for i in range(10)]},
        ]}}}}
    meta = explore.table_meta("APPID", "0003445078", session=FakeGetSession(payload))
    text = explore.format_meta(meta)
    assert "[cat01] 年齢（2 件）" in text and "065  65歳以上" in text
    assert "…ほか 5 件" in text  # 地域は先頭だけ


def test_dump(tmp_path):
    from polmap import explore

    class Session:
        def get(self, url, params, timeout):
            if url.endswith("getStatsList"):
                return FakeResp({"GET_STATS_LIST": {"RESULT": {"STATUS": 0}, "DATALIST_INF": {"TABLE_INF": [
                    {"@id": "0003000001", "TITLE": "表1"}, {"@id": "0003000002", "TITLE": "表2"}]}}})
            return FakeResp({"GET_META_INFO": {"RESULT": {"STATUS": 0}, "METADATA_INF": {
                "TABLE_INF": {"TITLE": f"表 {params['statsDataId']}"},
                "CLASS_INF": {"CLASS_OBJ": {"@id": "cat01", "@name": "男女", "CLASS": {"@code": "0", "@name": "総数", "@level": "1"}}}}}})

    written = explore.dump("APPID", tmp_path, ["住宅"], ids=["0003999999"], per_search=1, session=Session())
    assert sorted(p.name for p in written) == ["0003000001.txt", "0003999999.txt", "search.md"]
    assert "0003000002  表2" in (tmp_path / "search.md").read_text(encoding="utf-8")
    assert "0  総数" in (tmp_path / "meta" / "0003000001.txt").read_text(encoding="utf-8")


def test_table_filters_and_skip_placeholders():
    real = load_axes(ROOT / "config/axes.yaml")
    axes, skipped = real.without_placeholders()
    assert skipped == ["educated_university", "newcomers_moved_in", "homeowners_owned"]
    assert len(axes.axes) == 16
    assert axes.placeholders() == []
    f = fetch.table_filters(axes, "0003450620")
    assert f["cdCat01"] == ["0"]
    assert f["cdCat02"] == ["1", "11", "12", "13", "2", "3", "4", "5", "6", "7"]
    # 0003445190 は全軸で cat01/cat02/cat04 を総数に絞っている
    f = fetch.table_filters(axes, "0003445190")
    assert f["cdCat01"] == ["0"] and f["cdCat04"] == ["0"] and "R3" in f["cdCat03"]
