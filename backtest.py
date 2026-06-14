"""
backtest.py
===========
Replays the VOLARB engine over the PAST WEEK of real Deribit history, to show
"what the end state would look like if we'd switched it on a week ago."

Honest scope:
  - 1-minute historical candles (Deribit get_tradingview_chart_data) by default.
    Use --resolution 5 for coarser/faster runs. Hedging rebalances on each bar.
  - Historical feed has no order-book depth, so the ML model uses price-only
    features (market IV, realized vol, momentum). Real, just fewer inputs.
  - Frictionless: marked to the candle close, no spread, no fees (same basis
    as the live dashboard).

Run:
  python backtest.py                      # 7-day replay from data/7d/ (1-min)
  python backtest.py --days 1             # 24-hour quick replay from data/1d/
  python backtest.py --fetch-all          # download both 1d + 7d caches
  python backtest.py --fetch --days 1     # refresh 24h cache only
  python backtest.py --api --days 7       # live API, no cache

Output: prints a summary + writes backtest_result.html (open it in a browser).
"""

import argparse
import csv
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
import requests

from engine import (parse_expiry_years, black_scholes, implied_vol,
                    LiveVolModel, PaperPortfolio, RISK_FREE_RATE,
                    EDGE_THRESHOLD_USD, TARGET_CONTRACTS)

DEFAULT_DAYS = 7
DEFAULT_RESOLUTION = "1"   # minutes per candle (Deribit: 1, 3, 5, 15, 30, 60, ...)
CHUNK_HOURS = 24           # fetch history in 24h slices (1 slice/day)
BASE_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CHART_BASE = "https://www.deribit.com/api/v2/public/get_tradingview_chart_data"
INSTR_URL = ("https://www.deribit.com/api/v2/public/get_instruments"
             "?currency=BTC&kind=option&expired=false")
INDEX_URL = ("https://www.deribit.com/api/v2/public/get_index_price"
             "?index_name=btc_usd")


def data_dir(days):
    label = "1d" if days == 1 else f"{days}d"
    return os.path.join(BASE_DATA_DIR, label)


def meta_path(days):
    return os.path.join(data_dir(days), "meta.json")


def period_label(days):
    return "24-hour" if days == 1 else f"{days}-day"


def get_candles(instrument, start_ms, end_ms, resolution=DEFAULT_RESOLUTION, verbose=True):
    """Fetch candles in 24h slices and stitch."""
    out = {}
    step = CHUNK_HOURS * 3600 * 1000
    cur = int(start_ms)
    end_ms = int(end_ms)
    n_chunks = max(1, (end_ms - cur + step - 1) // step)
    chunk_i = 0
    while cur < end_ms:
        nxt = min(cur + step, end_ms)
        chunk_i += 1
        if verbose:
            print(f"     {instrument}: chunk {chunk_i}/{n_chunks}...", flush=True)
        try:
            r = requests.get(CHART_BASE, params={
                "instrument_name": instrument, "start_timestamp": cur,
                "end_timestamp": nxt, "resolution": resolution}, timeout=10)
            d = r.json().get("result", {})
            if d.get("status") == "ok" and d.get("ticks"):
                for t, c in zip(d["ticks"], d["close"]):
                    out[int(t)] = c
        except Exception as e:
            print(f"     (skipped a slice: {type(e).__name__})", flush=True)
        time.sleep(0.25)   # gentle pace to avoid rate-limit stalls
        cur = nxt
    return out


def _safe_name(instrument):
    return instrument.replace("/", "_")


def _candle_path(instrument, days):
    return os.path.join(data_dir(days), f"{_safe_name(instrument)}.csv")


def save_candles(instrument, candles, days):
    ddir = data_dir(days)
    os.makedirs(ddir, exist_ok=True)
    path = _candle_path(instrument, days)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_ms", "close"])
        for t in sorted(candles):
            w.writerow([t, candles[t]])


def load_candles(instrument, days):
    path = _candle_path(instrument, days)
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            out[int(row["timestamp_ms"])] = float(row["close"])
    return out


def save_meta(contracts, spot, start_ms, end_ms, resolution, days):
    ddir = data_dir(days)
    os.makedirs(ddir, exist_ok=True)
    meta = {
        "contracts": contracts,
        "spot": spot,
        "days": days,
        "resolution": resolution,
        "start_ms": int(start_ms),
        "end_ms": int(end_ms),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "perp": "BTC-PERPETUAL",
    }
    with open(meta_path(days), "w") as f:
        json.dump(meta, f, indent=2)


def load_meta(days):
    path = meta_path(days)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def cache_ready(days, resolution=DEFAULT_RESOLUTION):
    meta = load_meta(days)
    if not meta:
        return False
    if str(meta.get("resolution")) != str(resolution):
        return False
    if int(meta.get("days", days)) != int(days):
        return False
    perp = _candle_path(meta.get("perp", "BTC-PERPETUAL"), days)
    if not os.path.exists(perp):
        return False
    for c in meta.get("contracts", []):
        if not os.path.exists(_candle_path(c, days)):
            return False
    return True


def pick_contracts():
    instruments = requests.get(INSTR_URL, timeout=15).json().get("result", [])
    spot = float(requests.get(INDEX_URL, timeout=15).json()["result"]["index_price"])
    now_ms = time.time()*1000
    # contracts expiring 7-30 days out -> they already have a week of history
    window = [i for i in instruments
              if now_ms + 7*86400*1000 <= i.get("expiration_timestamp", 0)
              <= now_ms + 30*86400*1000]
    if not window:
        window = sorted(instruments, key=lambda x: x.get("expiration_timestamp", 0))[-40:]
    nearest = min(e["expiration_timestamp"] for e in window)
    same = [e for e in window if e["expiration_timestamp"] == nearest]
    same.sort(key=lambda e: abs(e["strike"] - spot))
    return [e["instrument_name"] for e in same[:TARGET_CONTRACTS]], spot


def fetch_and_cache(days=DEFAULT_DAYS, resolution=DEFAULT_RESOLUTION):
    label = period_label(days)
    print(f"[INFO] fetching {label} of {resolution}-min history from Deribit...")
    contracts, spot = pick_contracts()
    print(f"[OK] spot=${spot:,.0f}  contracts: {contracts}")
    now_ms = time.time() * 1000
    start_ms = now_ms - days * 86400 * 1000

    perp = get_candles("BTC-PERPETUAL", start_ms, now_ms, resolution=resolution)
    if not perp:
        raise RuntimeError("no perp history returned (network/endpoint issue)")
    save_candles("BTC-PERPETUAL", perp, days)
    print(f"[OK] cached BTC-PERPETUAL: {len(perp)} candles")

    opt = {}
    for c in contracts:
        d = get_candles(c, start_ms, now_ms, resolution=resolution)
        if d:
            opt[c] = d
            save_candles(c, d, days)
        print(f"     cached {c}: {len(d)} candles")

    if not opt:
        raise RuntimeError("no option history returned")

    save_meta(contracts, spot, start_ms, now_ms, resolution, days)
    print(f"[OK] wrote cache to {data_dir(days)}/")
    return sorted(perp.keys()), perp, opt, contracts, spot


def load_from_cache(days=DEFAULT_DAYS):
    meta = load_meta(days)
    if not meta:
        raise RuntimeError(f"no cache found — run: python backtest.py --fetch --days {days}")
    contracts = meta["contracts"]
    spot = meta["spot"]
    perp = load_candles(meta.get("perp", "BTC-PERPETUAL"), days)
    if not perp:
        raise RuntimeError("cached BTC-PERPETUAL data is missing or empty")
    opt = {}
    for c in contracts:
        d = load_candles(c, days)
        if d:
            opt[c] = d
    if not opt:
        raise RuntimeError("cached option data is missing or empty")
    ticks = sorted(perp.keys())
    print(f"[OK] loaded cache from {data_dir(days)}/  (fetched {meta.get('fetched_at', '?')})")
    print(f"[OK] spot=${spot:,.0f}  contracts: {contracts}")
    print(f"[OK] {len(ticks)} spot candles")
    for c in contracts:
        print(f"     {c}: {len(opt.get(c, {}))} candles")
    return ticks, perp, opt, contracts, spot


def replay(spot_ticks, spot_close, option_close, verbose=True):
    model = LiveVolModel(); pf = PaperPortfolio()
    spot_hist = deque(maxlen=120)
    pnl_curve = []   # (t_ms, pnl)
    focus = None
    for instr in option_close:
        if instr.endswith("-C"): focus = instr; break
    focus = focus or (list(option_close)[0] if option_close else None)
    focus_curve = []  # (t_ms, market, theo)

    for t in spot_ticks:
        spot = spot_close.get(t)
        if spot is None: continue
        spot_hist.append(spot)
        rvol = (float(np.std(np.diff(np.log(np.array(spot_hist)))) * math.sqrt(365*24*60))
                if len(spot_hist) >= 5 else 0.50)
        momentum = ((spot_hist[-1]-spot_hist[0])/spot_hist[0]
                    if len(spot_hist) > 1 else 0.0)
        contracts = {}
        for instr, closes in option_close.items():
            cb = closes.get(t)
            if cb is None: continue
            parsed = parse_expiry_years(instr)
            if not parsed: continue
            T, K, opt = parsed
            # recompute T at THIS historical moment
            exp = instr.split("-")[1]
            from engine import MONTHS
            try:
                ex = datetime(2000+int(exp[-2:]), MONTHS[exp[-5:-2]], int(exp[:-5]),
                              8, tzinfo=timezone.utc)
                T = max((ex.timestamp()*1000 - t)/1000, 0)/(365*24*3600)
            except Exception:
                pass
            if T <= 0: continue
            ask_usd = cb * spot                 # option close in BTC -> USD
            if ask_usd <= 0: continue
            mkt_iv = implied_vol(ask_usd, spot, K, T, RISK_FREE_RATE, opt)
            if mkt_iv is None or mkt_iv > 3.0 or mkt_iv < 0.05: continue
            features = [mkt_iv, 0.0, 0.0, rvol, momentum]   # price-only features
            pred_iv = model.observe(features, mkt_iv)
            theo, delta, vega = black_scholes(spot, K, T, RISK_FREE_RATE, pred_iv, opt)
            snap = {"instrument":instr,"type":opt,"strike":K,"T_days":T*365,
                    "market_ask":ask_usd,"market_bid":ask_usd,  # no spread historically
                    "theo":theo,"market_iv":mkt_iv,"pred_iv":pred_iv,
                    "delta":delta,"vega":vega,"edge":theo-ask_usd}
            contracts[instr] = snap
            if theo > ask_usd + EDGE_THRESHOLD_USD:
                pf.maybe_open(snap, spot)
            pf.update(snap, spot)
            if instr == focus:
                focus_curve.append((t, ask_usd, theo))
        summ = pf.summary(contracts, spot)
        pnl_curve.append((t, summ["total_pnl"]))

    return {
        "pnl_curve": pnl_curve, "focus_curve": focus_curve, "focus": focus,
        "summary": pf.summary(contracts, spot) if spot_ticks else {},
        "model_mae": model.mae, "baseline_mae": model.baseline_mae,
        "n_signals": pf.closed + len(pf.positions),
    }


def write_html(res, days=DEFAULT_DAYS, resolution=DEFAULT_RESOLUTION):
    pnl = res["pnl_curve"]; fc = res["focus_curve"]
    s = res["summary"]
    pnl_js = json.dumps([[datetime.fromtimestamp(t/1000).strftime("%m-%d %H:%M"), round(v,2)] for t,v in pnl])
    fc_js = json.dumps([[round(m,2), round(th,2)] for _,m,th in fc])
    mae = res["model_mae"]; base = res["baseline_mae"]
    final = pnl[-1][1] if pnl else 0
    period = period_label(days).upper()
    res_label = f"{resolution}-min"
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>VOLARB — {period} Backtest</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>body{{background:#0A0F16;color:#E8EEF4;font-family:Segoe UI,system-ui,sans-serif;padding:30px;max-width:1100px;margin:auto}}
h1{{letter-spacing:.2em;font-size:18px}} .sub{{color:#76889A;font-size:12px;margin-bottom:24px}}
.row{{display:flex;gap:14px;margin-bottom:24px;flex-wrap:wrap}}
.k{{background:#10171F;border:1px solid #1D2A37;border-radius:4px;padding:16px 20px;flex:1;min-width:150px}}
.k .l{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#76889A}}
.k .v{{font-family:ui-monospace,Consolas,monospace;font-size:24px;margin-top:6px}}
.pos{{color:#3FC79A}} .neg{{color:#E5566D}} .ai{{color:#37C7D8}}
.panel{{background:#10171F;border:1px solid #1D2A37;border-radius:4px;padding:20px;margin-bottom:20px}}
.foot{{color:#4A5A6B;font-size:11px;margin-top:20px;line-height:1.6}}</style></head>
<body>
<h1>VOLARB · {period} BACKTEST</h1>
<div class="sub">Replayed through the live engine on real Deribit history · {res_label} candles · frictionless</div>
<div class="row">
  <div class="k"><div class="l">Final P&amp;L</div><div class="v {'pos' if final>=0 else 'neg'}">{'$'+format(final,',.2f') if final>=0 else '-$'+format(abs(final),',.2f')}</div></div>
  <div class="k"><div class="l">Model MAE</div><div class="v ai">{('%.4f'%mae) if mae is not None else '—'}</div></div>
  <div class="k"><div class="l">Naive baseline</div><div class="v">{('%.4f'%base) if base is not None else '—'}</div></div>
  <div class="k"><div class="l">Rebalances</div><div class="v">{s.get('rebalances','—')}</div></div>
  <div class="k"><div class="l">Net Delta</div><div class="v">{('%.3f'%s.get('net_delta',0))}</div></div>
</div>
<div class="panel"><div class="l" style="font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#76889A;margin-bottom:10px">{period} P&amp;L</div>
<canvas id="p" height="90"></canvas></div>
<div class="panel"><div class="l" style="font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#76889A;margin-bottom:10px">Focus contract — market vs theoretical</div>
<canvas id="f" height="80"></canvas></div>
<div class="foot">Assumptions: hedged at {res_label} intervals, price-only ML features (no historical order book), marked to candle close (no spread, no fees). Simulated — no real orders. This is an approximate replay for illustration, not a realized trading record.</div>
<script>
const pnl={pnl_js}, fc={fc_js};
new Chart(document.getElementById('p'),{{type:'line',data:{{labels:pnl.map(r=>r[0]),datasets:[{{data:pnl.map(r=>r[1]),borderColor:'#3FC79A',backgroundColor:'rgba(63,199,154,.1)',borderWidth:1.6,pointRadius:0,fill:'origin',tension:.15}}]}},options:{{plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{color:'#4A5A6B',maxTicksLimit:8,font:{{size:10}}}},grid:{{display:false}}}},y:{{position:'right',ticks:{{color:'#4A5A6B',callback:v=>'$'+v.toFixed(0)}},grid:{{color:'rgba(29,42,55,.4)'}}}}}}}}}});
new Chart(document.getElementById('f'),{{type:'line',data:{{labels:fc.map((_,i)=>i),datasets:[{{label:'Market',data:fc.map(r=>r[0]),borderColor:'#E8A13A',borderWidth:1.4,pointRadius:0,tension:.2}},{{label:'Theo',data:fc.map(r=>r[1]),borderColor:'#37C7D8',backgroundColor:'rgba(55,199,216,.1)',borderWidth:1.4,pointRadius:0,fill:'-1',tension:.2}}]}},options:{{plugins:{{legend:{{display:false}}}},scales:{{x:{{display:false}},y:{{position:'right',ticks:{{color:'#4A5A6B',callback:v=>'$'+v.toFixed(0)}},grid:{{color:'rgba(29,42,55,.4)'}}}}}}}}}});
</script></body></html>"""
    with open("backtest_result.html", "w") as f:
        f.write(html)


def run_backtest(ticks, perp, opt, days=DEFAULT_DAYS, resolution=DEFAULT_RESOLUTION):
    res = replay(ticks, perp, opt)
    s = res["summary"]
    period = period_label(days).upper()
    print(f"\n===== {period} BACKTEST RESULT =====")
    fp = res["pnl_curve"][-1][1] if res["pnl_curve"] else 0
    print(f"Final P&L:      ${fp:,.2f}")
    print(f"Model MAE:      {res['model_mae']}")
    print(f"Baseline MAE:   {res['baseline_mae']}")
    print(f"Rebalances:     {s.get('rebalances')}")
    print(f"Net delta:      {s.get('net_delta'):.3f}")
    print(f"BTC hedged:     {s.get('gross_hedge'):.3f}")
    write_html(res, days=days, resolution=resolution)
    print("\n[OK] wrote backtest_result.html — open it in a browser.")


def _load_or_fetch(days, resolution, force_fetch=False):
    if force_fetch:
        return fetch_and_cache(days=days, resolution=resolution)
    if cache_ready(days, resolution=resolution):
        return load_from_cache(days=days)
    cached = load_meta(days)
    if cached and str(cached.get("resolution")) != str(resolution):
        print(f"[INFO] {data_dir(days)}/ is {cached.get('resolution')}-min — re-fetching at {resolution}-min...")
    else:
        print(f"[INFO] no cache in {data_dir(days)}/ — fetching...")
    return fetch_and_cache(days=days, resolution=resolution)


def main():
    parser = argparse.ArgumentParser(description="VOLARB backtest (offline cache or live API)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, choices=[1, 7],
                        help="history window: 1=24 hours, 7=7 days (default: 7)")
    parser.add_argument("--resolution", default=DEFAULT_RESOLUTION,
                        choices=["1", "3", "5", "15", "30", "60"],
                        help="candle size in minutes (default: 1)")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--fetch", action="store_true",
                     help="download fresh Deribit history, then replay")
    src.add_argument("--fetch-all", action="store_true",
                     help="download both 24h and 7-day caches, then replay selected --days")
    src.add_argument("--api", action="store_true",
                     help="pull live from Deribit API (ignore cache)")
    args = parser.parse_args()
    days = args.days
    resolution = args.resolution

    try:
        if args.fetch_all:
            for d in (1, 7):
                fetch_and_cache(days=d, resolution=resolution)
            ticks, perp, opt, _, _ = load_from_cache(days=days)
        elif args.fetch:
            ticks, perp, opt, _, _ = fetch_and_cache(days=days, resolution=resolution)
        elif args.api:
            print(f"[INFO] pulling {period_label(days)} of {resolution}-min history from Deribit API...")
            contracts, spot = pick_contracts()
            print(f"[OK] spot=${spot:,.0f}  contracts: {contracts}")
            now_ms = time.time() * 1000
            start_ms = now_ms - days * 86400 * 1000
            perp = get_candles("BTC-PERPETUAL", start_ms, now_ms, resolution=resolution)
            if not perp:
                print("[FATAL] no perp history returned (network/endpoint issue).")
                return
            ticks = sorted(perp.keys())
            print(f"[OK] {len(ticks)} spot candles")
            opt = {}
            for c in contracts:
                d = get_candles(c, start_ms, now_ms, resolution=resolution)
                if d:
                    opt[c] = d
                print(f"     {c}: {len(d)} candles")
            if not opt:
                print("[FATAL] no option history returned.")
                return
        else:
            ticks, perp, opt, _, _ = _load_or_fetch(days, resolution)
        run_backtest(ticks, perp, opt, days=days, resolution=resolution)
    except RuntimeError as e:
        print(f"[FATAL] {e}")


if __name__ == "__main__":
    main()
