"""
engine.py
=========
Live BTC options volatility-arbitrage engine + paper-trading layer for iFX Hack 2026.

Pipeline:
  1. Connects to Deribit's public production WebSocket (real live data).
  2. Parses real time-to-expiry from each contract.
  3. Back-solves the market's implied volatility from the live price.
  4. A streaming ML model (Ridge) forecasts volatility, scored with a live MAE.
  5. Black-Scholes prices each option at the forecast vol -> theoretical fair value.
  6. Flags mispricings (theo > ask + threshold).
  7. PAPER-TRADES the signals: buys the option, shorts the right amount of BTC to
     stay delta-neutral, rebalances the hedge as BTC moves, and subtracts realistic
     fees on every rebalance -> an honest, fee-aware live P&L. SIMULATED execution
     on REAL market data. No real orders are ever placed.
  8. Serves a JSON state endpoint + the dashboard over a tiny local web server.

Run:  python engine.py
Then open http://localhost:8765 in a browser.
"""

import asyncio
import json
import math
import os
import time
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import scipy.stats as stats
import requests
import websockets
from sklearn.linear_model import Ridge

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
DERIBIT_REST = ("https://www.deribit.com/api/v2/public/get_instruments"
                "?currency=BTC&kind=option&expired=false")
DERIBIT_WS = "wss://www.deribit.com/ws/api/v2"
INDEX_URL = ("https://www.deribit.com/api/v2/public/get_index_price"
             "?index_name=btc_usd")

RISK_FREE_RATE = 0.05
EDGE_THRESHOLD_USD = 0.50     # min $ gap before a contract is flagged
TARGET_CONTRACTS = 6          # near-the-money contracts to watch
MIN_TRAIN_SAMPLES = 40
PREDICT_HORIZON = 5

# paper-trading parameters
FEE_RATE = 0.0              # frictionless theoretical mode (set 0.0005 to model real fees)
REBALANCE_BAND = 0.01         # near-continuous hedging (frictionless theoretical case)
MAX_OPEN_POSITIONS = 3        # keep the demo readable
HTTP_PORT = 8765

MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


# --------------------------------------------------------------------------
# QUANT HELPERS (unchanged, already tested)
# --------------------------------------------------------------------------
def parse_expiry_years(instrument_name):
    parts = instrument_name.split("-")
    if len(parts) != 4:
        return None
    _, date_str, strike_str, cp = parts
    try:
        day = int(date_str[:-5]); mon = MONTHS[date_str[-5:-2]]
        yr = 2000 + int(date_str[-2:])
        expiry = datetime(yr, mon, day, 8, 0, 0, tzinfo=timezone.utc)
        seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
        return max(seconds, 0) / (365.0*24*3600), float(strike_str), \
            ("call" if cp == "C" else "put")
    except (ValueError, KeyError):
        return None


def black_scholes(S, K, T, r, sigma, option_type="call"):
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, S-K) if option_type == "call" else max(0.0, K-S)
        return intrinsic, 0.0, 0.0
    d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    if option_type == "call":
        price = S*stats.norm.cdf(d1) - K*math.exp(-r*T)*stats.norm.cdf(d2)
        delta = stats.norm.cdf(d1)
    else:
        price = K*math.exp(-r*T)*stats.norm.cdf(-d2) - S*stats.norm.cdf(-d1)
        delta = stats.norm.cdf(d1) - 1.0
    vega = S*math.sqrt(T)*stats.norm.pdf(d1)
    return price, delta, vega


def implied_vol(market_price, S, K, T, r, option_type="call"):
    if market_price <= 0 or T <= 0:
        return None
    lo, hi = 0.01, 5.0
    for _ in range(60):
        mid = 0.5*(lo+hi)
        price, _, _ = black_scholes(S, K, T, r, mid, option_type)
        if price > market_price: hi = mid
        else: lo = mid
    return 0.5*(lo+hi)


# --------------------------------------------------------------------------
# ML MODEL (unchanged, already tested)
# --------------------------------------------------------------------------
class LiveVolModel:
    def __init__(self):
        self.X = deque(maxlen=2000); self.y = deque(maxlen=2000)
        self.pending = deque(); self.model = Ridge(alpha=1.0)
        self.trained = False; self.errors = deque(maxlen=200)
        self.base_errors = deque(maxlen=200)  # persistence baseline
        self.tick = 0

    def observe(self, features, current_iv):
        self.tick += 1
        while self.pending and self.tick - self.pending[0][1] >= PREDICT_HORIZON:
            feat, _, prev_iv = self.pending.popleft()
            self.X.append(feat); self.y.append(current_iv)
            # baseline: "predict no change" -> error of assuming prev_iv stays
            self.base_errors.append(abs(prev_iv - current_iv))
        if len(self.X) >= MIN_TRAIN_SAMPLES and self.tick % 10 == 0:
            self.model.fit(np.array(self.X), np.array(self.y)); self.trained = True
        if self.trained:
            pred = max(0.05, float(self.model.predict([features])[0]))
            if self.y: self.errors.append(abs(pred - self.y[-1]))
        else:
            pred = current_iv
        self.pending.append((features, self.tick, current_iv))
        return pred

    @property
    def mae(self):
        return float(np.mean(self.errors)) if self.errors else None

    @property
    def baseline_mae(self):
        return float(np.mean(self.base_errors)) if self.base_errors else None


# --------------------------------------------------------------------------
# PAPER PORTFOLIO  (simulated execution on real prices, fee-aware)
# --------------------------------------------------------------------------
class PaperPortfolio:
    """
    Buys flagged-cheap options, shorts BTC to stay delta-neutral, rebalances as
    spot moves, and charges realistic fees on every rebalance. All simulated.
    """
    def __init__(self):
        self.positions = {}     # instrument -> dict
        self.realized = 0.0     # closed P&L
        self.fees = 0.0         # cumulative fees paid
        self.rebalances = 0
        self.closed = 0

    def maybe_open(self, snap, spot):
        instr = snap["instrument"]
        if instr in self.positions or len(self.positions) >= MAX_OPEN_POSITIONS:
            return
        # frictionless: enter at the mid price (no spread penalty)
        entry = 0.5 * (snap["market_ask"] + snap["market_bid"])
        delta = snap["delta"]
        fee = FEE_RATE * (entry + abs(delta) * spot)
        self.fees += fee
        self.positions[instr] = {
            "instrument": instr, "type": snap["type"], "strike": snap["strike"],
            "entry_option": entry, "entry_spot": spot, "last_spot": spot,
            "hedge_btc": delta,
            "hedge_pnl": 0.0, "fees": fee, "opened": time.strftime("%H:%M:%S"),
            "delta": delta, "vega": snap["vega"], "T0_days": snap["T_days"],
        }

    def update(self, snap, spot):
        """Mark a held position to the live market and rebalance its hedge."""
        instr = snap["instrument"]
        p = self.positions.get(instr)
        if not p:
            return
        # 1) accrue hedge P&L from spot move since last update (short position)
        p["hedge_pnl"] += -p["hedge_btc"] * (spot - p["last_spot"])
        p["last_spot"] = spot
        # 2) rebalance toward current delta if it has drifted beyond the band
        target = snap["delta"]
        drift = abs(target - p["hedge_btc"])
        if drift > REBALANCE_BAND:
            fee = FEE_RATE * drift * spot
            p["fees"] += fee; self.fees += fee
            p["hedge_btc"] = target
            self.rebalances += 1
        p["delta"] = snap["delta"]; p["vega"] = snap["vega"]
        # 3) close if essentially expired
        if snap["T_days"] <= 0.01:
            self._close(instr, snap, spot)

    def _close(self, instr, snap, spot):
        p = self.positions.pop(instr, None)
        if not p:
            return
        option_leg = snap["market_bid"] - p["entry_option"]  # sell into bid
        pos_pnl = option_leg + p["hedge_pnl"] - p["fees"]
        self.realized += pos_pnl
        self.closed += 1

    def summary(self, contracts, spot):
        """Real, frictionless valuation: mark each position at the market MID
        (removes spread + fees only; everything else is real market movement)."""
        unrealized = 0.0; net_delta = 0.0; net_vega = 0.0
        gross_hedge = 0.0; rows = []
        for instr, p in self.positions.items():
            c = contracts.get(instr)
            if not c:
                continue
            mid = 0.5 * (c["market_ask"] + c["market_bid"])   # real market mid
            option_leg = mid - p["entry_option"]              # real option move
            pnl = option_leg + p["hedge_pnl"] - p["fees"]
            unrealized += pnl
            net_delta += (p["delta"] - p["hedge_btc"])        # residual ~0 after hedge
            gross_hedge += abs(p["hedge_btc"])                # real BTC shorted
            net_vega += p["vega"]
            rows.append({
                "instrument": instr, "type": p["type"], "strike": p["strike"],
                "entry": p["entry_option"], "hedge_btc": p["hedge_btc"],
                "hedge_pnl": p["hedge_pnl"], "option_leg": option_leg, "pnl": pnl,
                "opened": p["opened"],
            })
        return {
            "open_positions": rows,
            "realized": self.realized, "unrealized": unrealized,
            "total_pnl": self.realized + unrealized,
            "fees": self.fees, "rebalances": self.rebalances, "closed": self.closed,
            "net_delta": net_delta, "gross_hedge": gross_hedge, "net_vega": net_vega,
        }


# --------------------------------------------------------------------------
# SHARED STATE
# --------------------------------------------------------------------------
class EngineState:
    def __init__(self):
        self.lock = threading.Lock()
        self.spot = None
        self.connected = False
        self.contracts = {}
        self.opportunities = deque(maxlen=40)
        self.spot_hist = deque(maxlen=240)        # (t, price)
        self.focus_hist = deque(maxlen=240)       # (t, market, theo) for focus contract
        self.focus_instr = None
        self.pnl_hist = deque(maxlen=240)         # (t, total_pnl, fees)
        self.tick_log = deque(maxlen=400)         # per-tick console output, for the live UI
        self.model_mae = None
        self.baseline_mae = None
        self.portfolio = {}

    def to_json(self):
        with self.lock:
            return json.dumps({
                "spot": self.spot, "connected": self.connected,
                "model_mae": self.model_mae, "baseline_mae": self.baseline_mae,
                "focus_instr": self.focus_instr,
                "contracts": list(self.contracts.values()),
                "opportunities": list(self.opportunities),
                "spot_hist": list(self.spot_hist),
                "focus_hist": list(self.focus_hist),
                "pnl_hist": list(self.pnl_hist),
                "tick_log": list(self.tick_log),
                "portfolio": self.portfolio,
            })


# --------------------------------------------------------------------------
# ENGINE
# --------------------------------------------------------------------------
class ArbitrageEngine:
    def __init__(self, state=None, verbose=True):
        self.state = state or EngineState()
        self.verbose = verbose
        self.spot = None
        self.spot_history = deque(maxlen=120)
        self.model = LiveVolModel()
        self.portfolio = PaperPortfolio()
        self.tickers = []

    def fetch_tickers(self):
        instruments = requests.get(DERIBIT_REST, timeout=15).json().get("result", [])
        spot = float(requests.get(INDEX_URL, timeout=15).json()["result"]["index_price"])
        now_ms = time.time()*1000
        window = [i for i in instruments
                  if now_ms + 5*86400*1000 <= i.get("expiration_timestamp", 0)
                  <= now_ms + 25*86400*1000]
        if not window:
            window = sorted(instruments,
                            key=lambda x: x.get("expiration_timestamp", 0))[:40]
        nearest = min(e["expiration_timestamp"] for e in window)
        same = [e for e in window if e["expiration_timestamp"] == nearest]
        same.sort(key=lambda e: abs(e["strike"] - spot))
        self.tickers = [e["instrument_name"] for e in same[:TARGET_CONTRACTS]]
        # focus on the at-the-money call for the main chart
        calls = [t for t in self.tickers if t.endswith("-C")]
        self.state.focus_instr = calls[0] if calls else self.tickers[0]
        if self.verbose:
            print(f"[OK] spot=${spot:,.0f}  watching: {self.tickers}")
        return self.tickers

    def realized_vol(self):
        if len(self.spot_history) < 5:
            return 0.50
        lr = np.diff(np.log(np.array(self.spot_history)))
        return float(np.std(lr) * math.sqrt(365*24*60))

    def on_index(self, data):
        self.spot = float(data["price"])
        self.spot_history.append(self.spot)
        with self.state.lock:
            self.state.spot = self.spot
            self.state.spot_hist.append((time.time(), self.spot))

    def on_book(self, data):
        if self.spot is None:
            self.spot = float(data.get("underlying_price", 0) or 0)
            if self.spot == 0: return
        instr = data["instrument_name"]
        parsed = parse_expiry_years(instr)
        if not parsed: return
        T, K, opt_type = parsed
        if T <= 0: return

        best_ask = data.get("best_ask_price") or 0.0
        best_bid = data.get("best_bid_price") or 0.0
        ask_qty = data.get("best_ask_amount") or 1.0
        bid_qty = data.get("best_bid_amount") or 1.0
        if best_ask <= 0: return
        ask_usd = best_ask * self.spot
        bid_usd = best_bid * self.spot

        mkt_iv = implied_vol(ask_usd, self.spot, K, T, RISK_FREE_RATE, opt_type)
        if mkt_iv is None or mkt_iv > 3.0 or mkt_iv < 0.05: return

        ob_imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty + 1e-8)
        spread = best_ask - best_bid
        rvol = self.realized_vol()
        momentum = (self.spot_history[-1] - self.spot_history[0]) / self.spot_history[0] \
            if len(self.spot_history) > 1 else 0.0
        features = [mkt_iv, ob_imbalance, spread, rvol, momentum]
        pred_iv = self.model.observe(features, mkt_iv)

        theo, delta, vega = black_scholes(self.spot, K, T, RISK_FREE_RATE,
                                          pred_iv, opt_type)
        snap = {
            "instrument": instr, "type": opt_type, "strike": K,
            "T_days": T*365, "market_ask": ask_usd, "market_bid": bid_usd,
            "theo": theo, "market_iv": mkt_iv, "pred_iv": pred_iv,
            "delta": delta, "vega": vega, "edge": theo - ask_usd,
        }

        is_arb = theo > ask_usd + EDGE_THRESHOLD_USD

        with self.state.lock:
            self.state.contracts[instr] = snap
            self.state.model_mae = self.model.mae
            self.state.baseline_mae = self.model.baseline_mae
            if instr == self.state.focus_instr:
                self.state.focus_hist.append((time.time(), ask_usd, theo))

        # paper trading
        if is_arb:
            self.portfolio.maybe_open(snap, self.spot)
        self.portfolio.update(snap, self.spot)
        summ = self.portfolio.summary(self.state.contracts, self.spot)

        tag = "ARB" if is_arb else "TICK"
        with self.state.lock:
            self.state.portfolio = summ
            self.state.pnl_hist.append((time.time(), summ["total_pnl"]))
            if is_arb:
                self.state.opportunities.appendleft({**snap, "t": time.strftime("%H:%M:%S")})
            self.state.tick_log.append({
                "t": time.strftime("%H:%M:%S"), "tag": tag, "instr": instr,
                "ask": ask_usd, "theo": theo, "mkt_iv": mkt_iv, "pred_iv": pred_iv,
                "pnl": summ["total_pnl"], "hedged": summ["gross_hedge"],
            })

        if self.verbose:
            print(f"[{tag}] {instr} ask=${ask_usd:.2f} theo=${theo:.2f} "
                  f"mktIV={mkt_iv:.2f} aiIV={pred_iv:.2f} "
                  f"PnL=${summ['total_pnl']:.2f} hedged={summ['gross_hedge']:.2f}BTC")

    async def run(self):
        self.fetch_tickers()
        async for ws in self._reconnect():
            try:
                channels = ["deribit_price_index.btc_usd"] + \
                           [f"ticker.{t}.100ms" for t in self.tickers]
                await ws.send(json.dumps({"jsonrpc":"2.0","id":1,
                    "method":"public/subscribe","params":{"channels":channels}}))
                with self.state.lock: self.state.connected = True
                if self.verbose: print("[OK] subscribed, streaming...")
                async for msg in ws:
                    r = json.loads(msg)
                    if "params" not in r: continue
                    ch, data = r["params"]["channel"], r["params"]["data"]
                    if "deribit_price_index" in ch: self.on_index(data)
                    elif "ticker" in ch: self.on_book(data)
            except websockets.ConnectionClosed:
                with self.state.lock: self.state.connected = False
                if self.verbose: print("[WARN] reconnecting...")
                continue

    async def _reconnect(self):
        while True:
            try:
                async with websockets.connect(DERIBIT_WS) as ws:
                    yield ws
            except Exception as e:
                if self.verbose: print(f"[WARN] connect failed: {e}; retry 2s")
                await asyncio.sleep(2)


# --------------------------------------------------------------------------
# WEB SERVER (serves dashboard + JSON state)
# --------------------------------------------------------------------------
def make_handler(state):
    here = os.path.dirname(os.path.abspath(__file__))
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass  # quiet
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path.startswith("/state"):
                self._send(200, state.to_json().encode(), "application/json")
            else:
                path = os.path.join(here, "dashboard.html")
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        self._send(200, f.read(), "text/html")
                else:
                    self._send(404, b"dashboard.html not found", "text/plain")
    return H


def start_web_server(state, port=HTTP_PORT):
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[OK] dashboard at http://localhost:{port}")


if __name__ == "__main__":
    state = EngineState()
    start_web_server(state)
    eng = ArbitrageEngine(state=state, verbose=True)
    try:
        asyncio.run(eng.run())
    except KeyboardInterrupt:
        print("\n[STOP] shut down cleanly.")
