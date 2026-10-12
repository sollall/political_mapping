"""コマンドライン: polmap run / check / demo

各段階の出力はファイルに保存されるので、--from / --to でどの段階からでも再実行できる。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

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
    axes = load_axes(settings.axes_path)
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

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.cmd == "check":
            s = load_settings(args.config)
            a = load_axes(s.axes_path)
            print(f"OK: 統計表 {len(a.tables)} 件、軸 {len(a.axes)} 件")
            for ax in a.axes:
                print(f"  {ax.axis_id:<28} {ax.formula:<10} {ax.group:<8} {ax.name}")
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
    except (ConfigError, FileNotFoundError, RuntimeError, ValueError) as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
