"""コマンドライン: polmap run / check / demo

各段階の出力はファイルに保存されるので、--from / --to でどの段階からでも再実行できる。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import requests

from . import align, analyze, compute, fetch, mapdata, stage, validate
from .config import STAGES, ConfigError, load_axes, load_settings

log = logging.getLogger("polmap")

RUNNERS = {
    "fetch": fetch.run,
    "stage": stage.run,
    "align": align.run,
    "compute": compute.run,
    "analyze": analyze.run,
    "map": mapdata.run,
    "validate": validate.run,
}


def run_pipeline(config: str | Path, start: str = "fetch", end: str = "validate", force_fetch: bool = False) -> str | None:
    settings = load_settings(config)
    axes, skipped = load_axes(settings.axes_path).without_placeholders()
    if skipped:
        log.warning("未記入（TBD）があるため次の軸を除いて実行します: %s", ", ".join(skipped))
    i, j = STAGES.index(start), STAGES.index(end)
    if i > j:
        raise ValueError(f"--from {start} は --to {end} より後の段階です")
    result = None
    for name in STAGES[i : j + 1]:
        log.info("== %s ==", name)
        if name == "fetch":
            fetch.run(settings, axes, force=force_fetch)
        else:
            result = RUNNERS[name](settings, axes)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="polmap", description="国勢調査2020 政治地理マップのパイプライン")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="パイプラインを実行する")
    r.add_argument("-c", "--config", default="config/settings.yaml")
    r.add_argument("--from", dest="start", choices=STAGES, default="fetch")
    r.add_argument("--to", dest="end", choices=STAGES, default="validate")
    r.add_argument("--force-fetch", action="store_true", help="取得済みの統計表も取り直す")
    r.add_argument("--strict", action="store_true", help="検証で fail があれば終了コード1")

    c = sub.add_parser("check", help="設定ファイルと軸定義を検証する")
    c.add_argument("-c", "--config", default="config/settings.yaml")

    d = sub.add_parser("demo", help="合成データでパイプライン全体を動かす（ネットワーク不要）")
    d.add_argument("-c", "--config", default="config/demo/settings.yaml")

    se = sub.add_parser("search", help="国勢調査の統計表を e-Stat でキーワード検索する（統計表IDを調べる）")
    se.add_argument("words", help="検索語（空白区切りで AND。例: 年齢 5歳階級）")
    se.add_argument("--year", default="2020", help="調査年（既定 2020）")
    se.add_argument("--area", default="municipality", choices=["municipality", "prefecture", "national", "all"],
                    help="集計地域（既定 municipality＝市区町村）")
    se.add_argument("--limit", type=int, default=50)
    se.add_argument("-c", "--config", default="config/settings.yaml")

    me = sub.add_parser("meta", help="統計表の次元とカテゴリーコードを一覧する")
    me.add_argument("stats_data_id", nargs="+", help="統計表ID（statsDataId）。複数可")
    me.add_argument("-c", "--config", default="config/settings.yaml")

    du = sub.add_parser("dump", help="検索結果とカテゴリーコードを explore/ に書き出す（コミットして共有する用）")
    du.add_argument("words", nargs="*", help="検索語。省略すると追加の3軸用の語（最終卒業学校・5年前の常住地・住宅の所有の関係）")
    du.add_argument("--ids", nargs="*", default=[], help="検索とは別にカテゴリーコードを書き出す統計表ID")
    du.add_argument("--per-search", type=int, default=8, help="検索語ごとにカテゴリーコードを書き出す件数（既定 8）")
    du.add_argument("--area", default="municipality", choices=["municipality", "prefecture", "national", "all"])
    du.add_argument("--out", default="explore", help="出力先（既定 explore/）")
    du.add_argument("-c", "--config", default="config/settings.yaml")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.cmd == "check":
            s = load_settings(args.config)
            a = load_axes(s.axes_path)
            print(f"OK: 統計表 {len(a.tables)} 件、軸 {len(a.axes)} 件")
            for ax in a.axes:
                print(f"  {ax.axis_id:<32} {ax.formula:<10} {ax.group:<8} {ax.name}")
            todo = a.placeholders()
            if todo:
                print(f"未記入（TBD）{len(todo)} 件:")
                for t in todo:
                    print(f"  {t}")
            return 0
        if args.cmd in ("search", "meta", "dump"):
            from . import explore

            app_id = load_settings(args.config).app_id  # .env も読み込まれる
            if not app_id:
                raise RuntimeError("ESTAT_APP_ID がありません。.env に ESTAT_APP_ID=... を書いてください")
            if args.cmd == "dump":
                written = explore.dump(app_id, Path(args.out), args.words or explore.DEFAULT_DUMP_WORDS,
                                       args.ids, args.per_search, area=args.area)
                print(f"{len(written)} 件を {args.out}/ に書き出しました")
            elif args.cmd == "search":
                print(explore.format_search(explore.search_tables(app_id, args.words, args.year, args.area, args.limit)))
            else:
                for i, tid in enumerate(args.stats_data_id):
                    print(("\n" + "=" * 60 + "\n" if i else "") + explore.format_meta(explore.table_meta(app_id, tid)))
            return 0
        if args.cmd == "demo":
            from . import demo

            s = load_settings(args.config)
            demo.generate(s)
            overall = run_pipeline(args.config)
            print(f"検証: {overall} → {s.report_dir / 'validation.md'}")
            print(f"地図: python -m http.server -d {s.web_data_dir.parent} 8000 → http://localhost:8000/")
            return 0
        overall = run_pipeline(args.config, args.start, args.end, args.force_fetch)
        if overall is not None:
            print(f"検証: {overall}")
        return 1 if args.strict and overall == "fail" else 0
    except (ConfigError, FileNotFoundError, RuntimeError, ValueError, requests.RequestException) as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
