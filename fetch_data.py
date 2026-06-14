#!/usr/bin/env python3
"""One-shot downloader: pulls Deribit history into data/1d/ and data/7d/."""
import sys

from backtest import DEFAULT_DAYS, DEFAULT_RESOLUTION, fetch_and_cache, run_backtest

if __name__ == "__main__":
    resolution = DEFAULT_RESOLUTION
    days = DEFAULT_DAYS
    if "--resolution" in sys.argv:
        i = sys.argv.index("--resolution")
        resolution = sys.argv[i + 1]
    if "--days" in sys.argv:
        i = sys.argv.index("--days")
        days = int(sys.argv[i + 1])
    try:
        if "--all" in sys.argv:
            for d in (1, 7):
                fetch_and_cache(days=d, resolution=resolution)
        else:
            fetch_and_cache(days=days, resolution=resolution)
        if "--no-run" not in sys.argv:
            from backtest import load_from_cache
            ticks, perp, opt, _, _ = load_from_cache(days=days)
            run_backtest(ticks, perp, opt, days=days, resolution=resolution)
    except RuntimeError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)
