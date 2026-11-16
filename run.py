from __future__ import annotations
import argparse
import logging
import os
import sys
from freshness.config import ROOT, load_config


def setup_logging():
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("freshness")
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(ROOT, "logs", "collect.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def main(argv=None):
    p = argparse.ArgumentParser(description="HTTP freshness study", formatter_class=argparse.RawTextHelpFormatter, epilog=__doc__)
    p.add_argument("command")
    p.add_argument("extra", nargs="?")
    p.add_argument("--db", help="database file (default: db_path in config.yaml)")
    p.add_argument("--config", help="config file (default: config.yaml)")
    p.add_argument("--n", type=int, help="number of sites for 'sample' (default: n_sites)")
    p.add_argument("--hourly", action="store_true", help="hourly round instead of a main round")
    p.add_argument("--out", help="output folder (analyze/simulate/export)")
    p.add_argument("--rebuild", action="store_true", help="pairs: delete and rebuild all pairs")
    args = p.parse_args(argv)

    setup_logging()
    log = logging.getLogger("freshness")
    cfg = load_config(args.config)
    if args.db:
        cfg.db_path = os.path.abspath(args.db)
    cmd = args.command

    if cmd == "test":
        import pytest
        sys.exit(pytest.main(["-v", os.path.join(ROOT, "tests")]))
    if cmd == "testserver":
        from freshness.testserver import serve_forever
        print("Test server on http://127.0.0.1:8080  (press Ctrl+C to stop)")
        serve_forever(8080)
        return

    from freshness.db import connect
    con = connect(cfg.db_path)

    if cmd == "sample":
        from freshness.sampler import sample_sites
        sample_sites(cfg, con, args.n)
        from freshness.tools import status
        status(cfg, con)
    elif cmd == "collect":
        from freshness.fetcher import run_round
        from freshness.tools import RunLock
        kind = "hourly" if args.hourly else "main"
        lock_path = os.path.join(os.path.dirname(cfg.db_path), f"collect-{kind}.lock")
        with RunLock(lock_path, stale_h=0.9 if kind == "hourly" else 3.5) as lock:
            if not lock.acquired:
                log.warning("another %s round is still running - skipping this one", kind)
                return
            if kind == "main" and os.path.exists(os.path.join(os.path.dirname(cfg.db_path), "collect-hourly.lock")):
                log.info("an hourly round is running; the main round will share the database")
            run_round(cfg, con, kind)
    elif cmd == "status":
        from freshness.tools import status
        status(cfg, con)
    elif cmd == "pairs":
        from freshness.pairs import build_pairs
        build_pairs(cfg, con, rebuild=args.rebuild)
    elif cmd == "analyze":
        from freshness.analysis import run_analysis
        run_analysis(cfg, con, args.out)
    elif cmd == "simulate":
        from freshness.simulator import simulate
        simulate(cfg, con, args.out)
    elif cmd == "all":
        from freshness.analysis import run_analysis
        from freshness.pairs import build_pairs
        from freshness.simulator import simulate
        build_pairs(cfg, con)
        run_analysis(cfg, con, args.out)
        simulate(cfg, con, args.out)
    elif cmd == "spotcheck":
        from freshness.tools import spotcheck_export
        path = args.extra or os.path.join(cfg.results_dir, "spot_check.csv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        spotcheck_export(cfg, con, path)
    elif cmd == "spotcheck-score":
        from freshness.tools import spotcheck_score
        spotcheck_score(args.extra or os.path.join(cfg.results_dir, "spot_check.csv"))
    elif cmd == "export":
        from freshness.tools import export
        export(cfg, con, args.out or os.path.join(ROOT, "export"))
    elif cmd == "backup":
        import sqlite3
        import time as _t
        dest = args.extra or os.path.join(os.path.dirname(cfg.db_path), "backup-" + _t.strftime("%Y%m%d-%H%M") + ".db")
        with sqlite3.connect(dest) as out:
            con.backup(out)
    elif cmd == "demo":
        from freshness.analysis import run_analysis
        from freshness.demo import make_demo
        from freshness.pairs import build_pairs
        from freshness.simulator import simulate
        if con.execute("SELECT COUNT(*) FROM fetch").fetchone()[0]:
            sys.exit("An empty database for the demo")
        out = args.out or os.path.join(cfg.results_dir, "demo")
        make_demo(cfg, con)
        build_pairs(cfg, con)
        run_analysis(cfg, con, out)
        simulate(cfg, con, out)
        print(f"\nDemo outputs are in {out}  (FAKE data)")
    else:
        p.print_help()
        sys.exit(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
