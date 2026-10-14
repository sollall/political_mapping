"""設定ファイル（settings.yaml / axes.yaml）の読み込みと検証。

軸定義は仕様書第5章の項目に対応する。コードを変えずに軸を追加できるよう、
すべての軸は axes.yaml に書く。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STAGES = ["fetch", "stage", "align", "compute", "analyze", "map", "validate"]
FORMULAS = {"ratio", "difference", "growth"}
AXIS_ID_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


class ConfigError(ValueError):
    pass


@dataclass
class Quantity:
    """統計表の中で filter に一致するセルの合計値。"""

    stats_data_id: str
    filter: dict[str, list[str]]

    @classmethod
    def parse(cls, raw: Any, default_table: str | None, where: str) -> "Quantity":
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: マッピングで指定してください")
        raw = dict(raw)
        table = raw.pop("stats_data_id", default_table)
        if not table:
            raise ConfigError(f"{where}: stats_data_id がありません")
        flt = raw.pop("filter", raw)
        if not isinstance(flt, dict) or not flt:
            raise ConfigError(f"{where}: filter（カテゴリーコード）が空です")
        norm = {}
        for dim, vals in flt.items():
            vals = vals if isinstance(vals, list) else [vals]
            norm[str(dim)] = [str(v) for v in vals]
        return cls(str(table), norm)


@dataclass
class Ratio:
    numerator: Quantity
    denominator: Quantity
    unknown: Quantity | None = None


@dataclass
class Axis:
    axis_id: str
    name: str
    group: str
    formula: str
    terms: list[Ratio]  # ratio: 1件、difference: 2件（terms[0] - terms[1]）
    growth: tuple[Quantity, Quantity] | None = None  # growth: (current, base)
    small_area: bool | None = None
    known_bias: str = ""
    updatable_2025: bool | None = None

    @property
    def left(self) -> str:
        return self.name.split("↔")[0].strip()

    @property
    def right(self) -> str:
        parts = self.name.split("↔")
        return parts[1].strip() if len(parts) > 1 else ""

    def quantities(self) -> list[Quantity]:
        out: list[Quantity] = []
        for t in self.terms:
            out += [t.numerator, t.denominator] + ([t.unknown] if t.unknown else [])
        if self.growth:
            out += list(self.growth)
        return out

    def weight_quantity(self) -> Quantity:
        """人口加重・小人口判定に使う分母。"""
        return self.growth[1] if self.growth else self.terms[0].denominator


@dataclass
class Table:
    stats_data_id: str
    year: int
    title: str = ""


PLACEHOLDER = "TBD"


@dataclass
class AxesConfig:
    tables: dict[str, Table]
    axes: list[Axis]

    def without_placeholders(self) -> tuple["AxesConfig", list[str]]:
        """未記入（TBD）の統計表・カテゴリーを使う軸を除いた定義と、除いた軸のIDを返す。"""
        def todo(a: Axis) -> bool:
            return any(
                q.stats_data_id.startswith(PLACEHOLDER) or any(c.startswith(PLACEHOLDER) for v in q.filter.values() for c in v)
                for q in a.quantities()
            )

        keep = [a for a in self.axes if not todo(a)]
        used = {q.stats_data_id for a in keep for q in a.quantities()}
        tables = {k: t for k, t in self.tables.items() if k in used}
        return AxesConfig(tables=tables, axes=keep), [a.axis_id for a in self.axes if todo(a)]

    def placeholders(self) -> list[str]:
        """「TBD」で始まる未記入の統計表ID・カテゴリーコードの一覧。"""
        out = [f"統計表 {t}" for t in self.tables if t.startswith(PLACEHOLDER)]
        for a in self.axes:
            codes = {c for q in a.quantities() for v in q.filter.values() for c in v if c.startswith(PLACEHOLDER)}
            if codes:
                out.append(f"{a.axis_id}: カテゴリーコード {len(codes)} 件")
        return out


@dataclass
class Settings:
    root: Path
    raw: dict[str, Any]
    axes_path: Path
    data_dir: Path
    web_data_dir: Path
    report_dir: Path
    boundary_path: Path
    boundary_changes_path: Path | None
    prefectures: list[str] = field(default_factory=list)

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def app_id(self) -> str | None:
        env = self.get("estat", "app_id_env", default="ESTAT_APP_ID")
        return os.environ.get(env) or None

    def layer(self, name: str) -> Path:
        p = self.data_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p


def _resolve(root: Path, p: str | None) -> Path | None:
    if p is None:
        return None
    path = Path(p)
    return path if path.is_absolute() else (root / path).resolve()


def load_dotenv(path: Path) -> None:
    """KEY=VALUE 形式の .env を読み、まだ設定されていない環境変数だけを設定する。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_settings(path: str | Path) -> Settings:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # パスはプロジェクトルート（settings の root、既定は設定ファイルの2階層上）からの相対
    root = _resolve(path.parent, raw.get("root", "..")) or path.parent
    load_dotenv(root / ".env")  # アプリケーションIDなどの秘密情報（Git管理外）
    paths = raw.get("paths", {})
    prefs = [str(p).zfill(2) for p in (raw.get("scope", {}).get("prefectures") or [])]
    return Settings(
        root=root,
        raw=raw,
        axes_path=_resolve(path.parent, raw.get("axes", "axes.yaml")),
        data_dir=_resolve(root, paths.get("data", "data")),
        web_data_dir=_resolve(root, paths.get("web_data", "web/data")),
        report_dir=_resolve(root, paths.get("reports", "reports")),
        boundary_path=_resolve(root, paths.get("boundary", "data/boundary/N03-20200101.geojson")),
        boundary_changes_path=_resolve(root, paths.get("boundary_changes")),
        prefectures=prefs,
    )


def _parse_ratio(raw: dict, default_table: str | None, where: str) -> Ratio:
    unk = raw.get("unknown")
    return Ratio(
        numerator=Quantity.parse(raw.get("numerator"), default_table, f"{where}.numerator"),
        denominator=Quantity.parse(raw.get("denominator"), default_table, f"{where}.denominator"),
        unknown=Quantity.parse(unk, default_table, f"{where}.unknown") if unk else None,
    )


def parse_axis(raw: dict) -> Axis:
    where = f"axis {raw.get('axis_id', '?')}"
    for key in ("axis_id", "name", "group", "formula"):
        if not raw.get(key):
            raise ConfigError(f"{where}: 必須項目 {key} がありません")
    axis_id = raw["axis_id"]
    if not AXIS_ID_RE.match(axis_id):
        raise ConfigError(f"{where}: axis_id は英小文字のスラッグにしてください（例: group_name）")
    if "↔" not in raw["name"]:
        raise ConfigError(f"{where}: 軸名は「A ↔ B」形式にしてください")
    formula = raw["formula"]
    if formula not in FORMULAS:
        raise ConfigError(f"{where}: formula は {sorted(FORMULAS)} のいずれか")
    table = raw.get("stats_data_id")
    terms: list[Ratio] = []
    growth = None
    if formula == "ratio":
        terms = [_parse_ratio(raw, table, where)]
    elif formula == "difference":
        items = raw.get("terms") or []
        if len(items) != 2:
            raise ConfigError(f"{where}: difference は terms に比率を2つ書きます（terms[0] - terms[1]）")
        terms = [_parse_ratio(t, t.get("stats_data_id", table), f"{where}.terms[{i}]") for i, t in enumerate(items)]
    else:
        growth = (
            Quantity.parse(raw.get("current"), table, f"{where}.current"),
            Quantity.parse(raw.get("base"), table, f"{where}.base"),
        )
    return Axis(
        axis_id=axis_id,
        name=raw["name"],
        group=raw["group"],
        formula=formula,
        terms=terms,
        growth=growth,
        small_area=raw.get("small_area"),
        known_bias=raw.get("known_bias", "") or "",
        updatable_2025=raw.get("updatable_2025"),
    )


def load_axes(path: str | Path) -> AxesConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tables = {}
    for tid, t in (raw.get("tables") or {}).items():
        if not isinstance(t, dict) or "year" not in t:
            raise ConfigError(f"table {tid}: year（2020 / 2015 など）が必要です")
        tables[str(tid)] = Table(str(tid), int(t["year"]), t.get("title", ""))
    axes = [parse_axis(a) for a in (raw.get("axes") or [])]
    seen = set()
    for a in axes:
        if a.axis_id in seen:
            raise ConfigError(f"axis_id が重複しています: {a.axis_id}")
        seen.add(a.axis_id)
        for q in a.quantities():
            if q.stats_data_id not in tables:
                raise ConfigError(f"axis {a.axis_id}: 統計表 {q.stats_data_id} が tables に登録されていません")
    return AxesConfig(tables=tables, axes=axes)
