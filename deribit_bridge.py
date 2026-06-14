"""
deribit_bridge.py
=================
Runs the Hackathon BTC-options volatility-arbitrage engine (real live Deribit
market data + simulated, fee-aware paper trading) and re-exposes its state as
the same Alpaca-shaped REST endpoints the Alcala extension already speaks.

So the "Deribit" account in the extension shows a *simulated* live balance,
positions, an order timeline of the engine's trades (tagged 🤖 AI), and a live
P&L curve — all driven by REAL Deribit prices. No real orders are placed.

Run:
    pip install -r engine/Hackathon-NSM-FY26/requirements.txt aiohttp
    python deribit_bridge.py            # serves http://127.0.0.1:8789

Env: DERIBIT_SIM_EQUITY (default 100000), DERIBIT_BRIDGE_PORT (default 8789).
"""

import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

from aiohttp import web

# --- load the Hackathon engine module by path (avoids clashing with ./engine.py) ---
ENGINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine", "Hackathon-NSM-FY26")
sys.path.insert(0, ENGINE_DIR)
_spec = importlib.util.spec_from_file_location("deribit_engine", os.path.join(ENGINE_DIR, "engine.py"))
deribit_engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deribit_engine)
ArbitrageEngine = deribit_engine.ArbitrageEngine
EngineState = deribit_engine.EngineState

START_EQUITY = float(os.getenv("DERIBIT_SIM_EQUITY", "100000"))
BRIDGE_PORT = int(os.getenv("DERIBIT_BRIDGE_PORT", "8789"))
EQUITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deribit_equity_history.json")
RANGE_SECONDS = {
    "1D": 86400, "1W": 7 * 86400, "1M": 31 * 86400,
    "3M": 93 * 86400, "1Y": 366 * 86400, "1A": 366 * 86400,
}

# --- shared engine state + bridge-maintained derived state ---
state = EngineState()
orders = []          # Alpaca-shaped order log (engine trades), newest first
prev_open = {}       # instrument -> position dict, to diff opens/closes
latest = {"realized": 0.0, "unrealized": 0.0, "total_pnl": 0.0,
          "connected": False, "spot": None, "model_mae": None, "open": []}


def start_engine():
    def _run():
        eng = ArbitrageEngine(state=state, verbose=False)
        asyncio.run(eng.run())
    threading.Thread(target=_run, daemon=True).start()


def read_state():
    try:
        return json.loads(state.to_json())
    except Exception:
        return {}


# --------------------------------------------------------------------------
# equity curve (persisted so the balance line grows across runs)
# --------------------------------------------------------------------------
def _load_equity_log():
    try:
        with open(EQUITY_FILE) as f:
            return [[int(t), float(v)] for t, v in json.load(f)]
    except Exception:
        return []


equity_log = _load_equity_log()


def record_equity(eq):
    if eq is None:
        return
    now = int(time.time())
    if equity_log and now - equity_log[-1][0] < 4:
        return
    equity_log.append([now, float(eq)])
    if len(equity_log) > 8000:
        del equity_log[:-8000]
    try:
        with open(EQUITY_FILE, "w") as f:
            json.dump(equity_log, f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def short_sym(instr):
    return instr.replace("BTC-", "") if isinstance(instr, str) else instr


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_order(side, instr, price):
    oid = f"ai-{instr}-{int(time.time() * 1000) % 1000000}"
    return {
        "id": oid, "client_order_id": oid,
        "symbol": short_sym(instr), "side": side, "qty": 1, "filled_qty": 1,
        "type": "market", "status": "filled", "limit_price": None,
        "filled_avg_price": float(price or 0.0),
        "submitted_at": now_iso(), "created_at": now_iso(),
    }


def ingest(snap):
    """Diff the engine's open positions into an order timeline + record equity."""
    port = snap.get("portfolio") or {}
    open_list = port.get("open_positions", []) or []
    open_map = {p["instrument"]: p for p in open_list}

    for instr, p in open_map.items():
        if instr not in prev_open:
            orders.insert(0, make_order("buy", instr, p.get("entry")))
    for instr, p in prev_open.items():
        if instr not in open_map:
            exit_price = (p.get("entry") or 0.0) + (p.get("option_leg") or 0.0)
            orders.insert(0, make_order("sell", instr, exit_price))
    del orders[200:]
    prev_open.clear()
    prev_open.update(open_map)

    latest.update({
        "realized": port.get("realized", 0.0),
        "unrealized": port.get("unrealized", 0.0),
        "total_pnl": port.get("total_pnl", 0.0),
        "connected": snap.get("connected", False),
        "spot": snap.get("spot"),
        "model_mae": snap.get("model_mae"),
        "open": open_list,
    })
    record_equity(START_EQUITY + port.get("total_pnl", 0.0))


# --------------------------------------------------------------------------
# endpoint handlers (Alpaca-shaped)
# --------------------------------------------------------------------------
LIVE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine_live.html")


async def health(_req):
    return web.json_response({"ok": True, "connected": bool(latest.get("connected"))})


async def state_json(_req):
    # raw engine state — feeds the live VOLARB dashboard (engine_live.html)
    return web.Response(text=state.to_json(), content_type="application/json")


async def log_json(_req):
    # per-tick engine output ([TICK]/[ARB] lines) for the live log drawer
    try:
        with state.lock:
            items = list(state.tick_log)
    except Exception:
        items = []
    return web.json_response({"log": items})


async def live_page(_req):
    try:
        with open(LIVE_HTML, "r") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="engine_live.html not found next to deribit_bridge.py", status=404)


async def get_account(_req):
    equity = START_EQUITY + latest["total_pnl"]
    return web.json_response({
        "equity": equity,
        "last_equity": START_EQUITY,           # "today" P&L == strategy P&L
        "portfolio_value": equity,
        "cash": START_EQUITY + latest["realized"],
        "buying_power": equity,
        "currency": "USD",
    })


async def get_positions(_req):
    rows = []
    for p in latest.get("open", []):
        entry = p.get("entry") or 0.0
        cur = entry + (p.get("option_leg") or 0.0)
        pnl = p.get("pnl") or 0.0
        rows.append({
            "symbol": short_sym(p["instrument"]),
            "qty": 1,
            "side": "long",
            "avg_entry_price": entry,
            "current_price": cur,
            "market_value": cur,
            "unrealized_pl": pnl,
            "unrealized_plpc": (pnl / entry) if entry else 0.0,
        })
    return web.json_response(rows)


async def get_orders(_req):
    return web.json_response(orders)


async def portfolio_history(req):
    period = (req.query.get("period") or req.query.get("range") or "1M").upper()
    window = RANGE_SECONDS.get(period, RANGE_SECONDS["1M"])
    now = int(time.time())
    pts = [p for p in equity_log if p[0] >= now - window]
    if len(pts) >= 2:
        return web.json_response({
            "timestamp": [p[0] for p in pts],
            "equity": [p[1] for p in pts],
            "base_value": START_EQUITY,
        })
    equity = START_EQUITY + latest["total_pnl"]
    return web.json_response({
        "timestamp": [now - 60, now],
        "equity": [START_EQUITY, equity],
        "base_value": START_EQUITY,
    })


def _is_btc(sym):
    return sym.upper() in ("BTC", "BTC/USD", "BTCUSD", "BTC-PERPETUAL")


async def snapshot(req):
    sym = req.match_info["sym"]
    if not _is_btc(sym):
        return web.json_response({"message": "Deribit account tracks BTC — search 'BTC'."}, status=400)
    snap = read_state()
    spot = snap.get("spot") or 0.0
    hist = [h[1] for h in (snap.get("spot_hist") or [])] or [spot]
    prev = hist[0] if hist else spot
    return web.json_response({
        "latestTrade": {"p": spot},
        "minuteBar": {"c": spot},
        "dailyBar": {"o": prev, "h": max(hist), "l": min(hist), "c": spot, "v": 0},
        "prevDailyBar": {"c": prev},
    })


async def bars(req):
    sym = req.match_info["sym"]
    if not _is_btc(sym):
        return web.json_response({"message": "Deribit account tracks BTC — search 'BTC'."}, status=400)
    snap = read_state()
    closes = [h[1] for h in (snap.get("spot_hist") or [])]
    return web.json_response({"bars": [{"c": c} for c in closes]})


async def place_order(_req):
    return web.json_response(
        {"message": "The Deribit account is autopiloted by the engine — manual orders are disabled."},
        status=400)


# --------------------------------------------------------------------------
# app wiring
# --------------------------------------------------------------------------
@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            resp = web.json_response({"message": str(e)}, status=500)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


async def sampler():
    while True:
        ingest(read_state())
        await asyncio.sleep(5)


async def on_startup(app):
    print("[deribit_bridge] starting vol-arb engine (real Deribit data, simulated trades)...")
    start_engine()
    record_equity(START_EQUITY)
    app["sampler"] = asyncio.create_task(sampler())


async def on_cleanup(app):
    task = app.get("sampler")
    if task:
        task.cancel()


def main():
    app = web.Application(middlewares=[cors_mw])
    app.router.add_get("/", live_page)
    app.router.add_get("/live", live_page)
    app.router.add_get("/state", state_json)
    app.router.add_get("/log", log_json)
    app.router.add_get("/health", health)
    app.router.add_get("/v2/account", get_account)
    app.router.add_get("/v2/positions", get_positions)
    app.router.add_get("/v2/orders", get_orders)
    app.router.add_get("/v2/account/portfolio/history", portfolio_history)
    app.router.add_get("/v2/stocks/{sym}/snapshot", snapshot)
    app.router.add_get("/v2/stocks/{sym}/bars", bars)
    app.router.add_post("/v2/orders", place_order)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"[deribit_bridge] simulated Deribit account on http://127.0.0.1:{BRIDGE_PORT}")
    print(f"[deribit_bridge] live VOLARB dashboard at  http://127.0.0.1:{BRIDGE_PORT}/")
    web.run_app(app, host="127.0.0.1", port=BRIDGE_PORT, print=None)


if __name__ == "__main__":
    main()
