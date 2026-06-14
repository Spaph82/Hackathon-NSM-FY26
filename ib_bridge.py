"""
ib_bridge.py
============
Local bridge between Interactive Brokers' Trader Workstation (TWS) and the
Alcala Chrome extension.

TWS speaks a proprietary socket protocol that a browser can't use, so this
process connects to TWS with ib_insync and re-exposes the handful of REST
endpoints the extension already speaks for Alpaca — returning *Alpaca-shaped*
JSON. That way the extension's IBKR adapter is a thin proxy and the UI needs
no broker-specific code.

The data is live: TWS pulls real quotes/positions from IBKR's servers; only
this translator runs locally.

Setup
-----
1. In TWS: Global Configuration -> API -> Settings
     * enable "ActiveX and Socket Clients"
     * Socket port 7497 (paper) / 7496 (live)
     * add 127.0.0.1 to Trusted IPs
     * uncheck "Read-Only API" to allow order placement
2. pip install ib_insync aiohttp
3. python ib_bridge.py          # serves http://127.0.0.1:8788

Env overrides: IB_HOST, IB_PORT (default 7497), IB_CLIENT_ID (default 7),
BRIDGE_PORT (default 8788).
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from aiohttp import web
from ib_insync import IB, Stock, MarketOrder, LimitOrder, util

IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7497"))      # 7497 paper, 7496 live
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "7"))
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8788"))

ib = IB()

RANGE_BARS = {
    "1D": ("1 D", "5 mins"),
    "1W": ("1 W", "1 hour"),
    "1M": ("1 M", "1 day"),
    "3M": ("3 M", "1 day"),
    "1Y": ("1 Y", "1 day"),
}
STATUS_MAP = {
    "Filled": "filled",
    "ApiCancelled": "canceled",
    "Cancelled": "canceled",
    "PendingCancel": "pending_cancel",
    "PreSubmitted": "accepted",
    "PendingSubmit": "pending_new",
    "Submitted": "new",
    "Inactive": "rejected",
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def acct_dict():
    """tag -> value, merging the account summary and account-update streams.
    Prefers the base/USD currency row when a tag is reported per-currency."""
    d = {}
    for v in list(ib.accountSummary()) + list(ib.accountValues()):
        cur = (getattr(v, "currency", "") or "")
        if v.tag not in d or cur in ("BASE", "USD"):
            d[v.tag] = v.value
    return d


# --- live equity curve: TWS has no NAV history, so we record it ourselves ---
EQUITY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ib_equity_history.json")
RANGE_SECONDS = {
    "1D": 86400, "1W": 7 * 86400, "1M": 31 * 86400,
    "3M": 93 * 86400, "1Y": 366 * 86400, "1A": 366 * 86400,
}


def _load_equity_log():
    try:
        with open(EQUITY_FILE) as f:
            return [[int(t), float(v)] for t, v in json.load(f)]
    except Exception:
        return []


equity_log = _load_equity_log()   # [[ts_seconds, equity], ...]


def record_equity(eq):
    if not eq or eq <= 0:
        return
    now = int(time.time())
    if equity_log and now - equity_log[-1][0] < 20 and abs(eq - equity_log[-1][1]) < 1e-9:
        return
    equity_log.append([now, float(eq)])
    if len(equity_log) > 6000:
        del equity_log[:-6000]
    try:
        with open(EQUITY_FILE, "w") as f:
            json.dump(equity_log, f)
    except Exception:
        pass


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def is_crypto(sym):
    return "/" in sym


async def qualify_stock(sym):
    c = Stock(sym.upper(), "SMART", "USD")
    res = await ib.qualifyContractsAsync(c)
    if not res or not c.conId:
        raise ValueError(f"Could not find a US stock for symbol '{sym}'.")
    return c


def iso(dt):
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return None


# --------------------------------------------------------------------------
# endpoint handlers
# --------------------------------------------------------------------------
async def health(_req):
    return web.json_response({"ok": True, "connected": ib.isConnected()})


async def get_account(_req):
    av = acct_dict()
    equity = fnum(av.get("NetLiquidation"))
    last = fnum(av.get("PreviousDayEquityWithLoanValue")) or equity
    record_equity(equity)
    return web.json_response({
        "equity": equity,
        "last_equity": last,
        "portfolio_value": equity,
        "cash": fnum(av.get("TotalCashValue")),
        "buying_power": fnum(av.get("BuyingPower")) or fnum(av.get("AvailableFunds")),
        "currency": "USD",
    })


async def get_positions(_req):
    rows = []
    for p in ib.portfolio():
        qty = fnum(p.position)
        if qty == 0:
            continue
        cost = fnum(p.averageCost) * abs(qty)
        upl = fnum(p.unrealizedPNL)
        rows.append({
            "symbol": p.contract.symbol,
            "qty": qty,
            "side": "long" if qty >= 0 else "short",
            "avg_entry_price": fnum(p.averageCost),
            "current_price": fnum(p.marketPrice),
            "market_value": fnum(p.marketValue),
            "unrealized_pl": upl,
            "unrealized_plpc": (upl / cost) if cost else 0.0,
        })
    return web.json_response(rows)


async def get_orders(_req):
    try:
        await ib.reqCompletedOrdersAsync(False)
    except Exception:
        pass
    out = []
    for t in ib.trades():
        o, st = t.order, t.orderStatus
        otype = "limit" if (o.orderType or "").upper() == "LMT" else "market"
        submitted = iso(t.log[0].time) if t.log else None
        out.append({
            "id": str(o.permId or o.orderId),
            "client_order_id": o.orderRef or "",
            "symbol": t.contract.symbol,
            "side": (o.action or "").lower(),
            "qty": fnum(o.totalQuantity),
            "filled_qty": fnum(st.filled),
            "type": otype,
            "status": STATUS_MAP.get(st.status, (st.status or "new").lower()),
            "limit_price": fnum(o.lmtPrice) if otype == "limit" else None,
            "filled_avg_price": fnum(st.avgFillPrice),
            "submitted_at": submitted,
            "created_at": submitted,
        })
    out.sort(key=lambda x: x["submitted_at"] or "", reverse=True)
    return web.json_response(out)


async def portfolio_history(req):
    # serve the equity curve we've been recording while the bridge runs
    period = (req.query.get("period") or req.query.get("range") or "1M").upper()
    window = RANGE_SECONDS.get(period, RANGE_SECONDS["1M"])
    now = int(time.time())

    av = acct_dict()
    equity = fnum(av.get("NetLiquidation"))
    record_equity(equity)  # make sure "now" is captured

    pts = [p for p in equity_log if p[0] >= now - window]
    if len(pts) >= 2:
        return web.json_response({
            "timestamp": [p[0] for p in pts],
            "equity": [p[1] for p in pts],
            "base_value": pts[0][1],
        })

    # not enough history recorded yet -> prev-close -> now placeholder
    last = fnum(av.get("PreviousDayEquityWithLoanValue")) or equity
    return web.json_response({
        "timestamp": [now - 86400, now],
        "equity": [last, equity],
        "base_value": last,
    })


async def snapshot(req):
    sym = req.match_info["sym"]
    if is_crypto(sym):
        return web.json_response({"message": "This IBKR bridge supports US stocks only."}, status=400)
    c = await qualify_stock(sym)
    [tk] = await ib.reqTickersAsync(c)
    last = fnum(tk.last) or fnum(tk.close) or fnum(tk.marketPrice())
    return web.json_response({
        "latestTrade": {"p": last},
        "minuteBar": {"c": last},
        "dailyBar": {
            "o": fnum(tk.open), "h": fnum(tk.high), "l": fnum(tk.low),
            "c": last, "v": fnum(tk.volume),
        },
        "prevDailyBar": {"c": fnum(tk.close)},
    })


async def bars(req):
    sym = req.match_info["sym"]
    if is_crypto(sym):
        return web.json_response({"message": "This IBKR bridge supports US stocks only."}, status=400)
    rng = req.query.get("range") or req.query.get("timeframe") or "1M"
    duration, bar_size = RANGE_BARS.get(rng, RANGE_BARS["1M"])
    c = await qualify_stock(sym)
    data = await ib.reqHistoricalDataAsync(
        c, endDateTime="", durationStr=duration, barSizeSetting=bar_size,
        whatToShow="TRADES", useRTH=False, formatDate=1)
    return web.json_response({"bars": [{"c": fnum(b.close)} for b in data]})


async def place_order(req):
    body = await req.json()
    sym = (body.get("symbol") or "").upper()
    if not sym or is_crypto(sym):
        return web.json_response({"message": "IBKR bridge: provide a US stock symbol."}, status=400)
    qty = fnum(body.get("qty"))
    side = (body.get("side") or "buy").upper()       # BUY / SELL
    otype = (body.get("type") or "market").lower()
    tif = (body.get("time_in_force") or "day").upper()  # DAY / GTC

    if qty <= 0:
        return web.json_response({"message": "Quantity must be greater than 0."}, status=400)

    c = await qualify_stock(sym)
    if otype == "limit":
        order = LimitOrder(side, qty, fnum(body.get("limit_price")))
    else:
        order = MarketOrder(side, qty)
    order.tif = "GTC" if tif == "GTC" else "DAY"
    order.outsideRth = True                       # allow transmit outside regular hours
    order.orderRef = body.get("client_order_id") or ""
    accts = ib.managedAccounts()
    if accts:
        order.account = accts[0]

    trade = ib.placeOrder(c, order)

    # wait for an ack / fill / rejection (up to ~4s)
    GOOD = ("Filled", "Submitted", "PreSubmitted", "PendingSubmit")
    for _ in range(20):
        await asyncio.sleep(0.2)
        if trade.orderStatus.status in GOOD + ("Cancelled", "ApiCancelled", "Inactive"):
            break
        if any(e.errorCode for e in trade.log):
            break

    st = trade.orderStatus
    # informational warnings we don't treat as failures
    WARN = {399, 2109, 2137, 10167}
    errs = [e for e in trade.log if e.errorCode and e.errorCode not in WARN]

    if st.status not in GOOD:
        reason = errs[-1].message if errs else f"status={st.status or 'no acknowledgement from TWS'}"
        hint = ""
        if errs and "read-only" in errs[-1].message.lower():
            hint = " — uncheck 'Read-Only API' in TWS (Global Config → API → Settings)."
        elif not errs and not st.status:
            hint = " — check TWS for an order-confirmation popup, or enable 'Bypass Order Precautions for API Orders' (API → Precautions)."
        return web.json_response({"message": f"IBKR rejected the order: {reason}{hint}"}, status=400)

    return web.json_response({
        "id": str(trade.order.permId or trade.order.orderId),
        "client_order_id": order.orderRef,
        "symbol": sym,
        "qty": qty,
        "side": side.lower(),
        "type": otype,
        "status": STATUS_MAP.get(st.status, (st.status or "accepted").lower()),
        "filled_avg_price": fnum(st.avgFillPrice),
    })


# --------------------------------------------------------------------------
# app wiring
# --------------------------------------------------------------------------
@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    elif request.path != "/health" and not ib.isConnected():
        resp = web.json_response({
            "connected": False,
            "message": ("Bridge is running but not connected to TWS yet. In TWS open "
                        "Global Configuration → API → Settings, enable 'ActiveX and Socket "
                        f"Clients', confirm the socket port is {IB_PORT}, and add 127.0.0.1 "
                        "to Trusted IPs. Make sure TWS is logged in."),
        }, status=503)
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


async def maintain_connection():
    """Keep trying to (re)connect to TWS without taking down the HTTP server."""
    while True:
        if not ib.isConnected():
            try:
                await ib.connectAsync(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)
                ib.reqMarketDataType(3)        # delayed data works without a live subscription
                accts = ib.managedAccounts()
                ib.reqAccountUpdates(accts[0] if accts else "")  # stream values + portfolio
                await ib.reqAccountSummaryAsync()  # NetLiquidation, BuyingPower, cash, ...
                await asyncio.sleep(1)              # let the first values land
                record_equity(fnum(acct_dict().get("NetLiquidation")))
                print(f"[ib_bridge] connected to TWS. accounts: {ib.managedAccounts()}")
            except Exception as e:
                print(f"[ib_bridge] TWS not reachable on {IB_HOST}:{IB_PORT} "
                      f"({e.__class__.__name__}). Is TWS running with the API enabled on "
                      f"this port? Retrying in 3s...")
        await asyncio.sleep(3)


async def sample_equity():
    """Record NetLiquidation periodically so the balance curve fills in over time."""
    while True:
        await asyncio.sleep(30)
        if ib.isConnected():
            try:
                record_equity(fnum(acct_dict().get("NetLiquidation")))
            except Exception:
                pass


async def on_startup(app):
    print(f"[ib_bridge] will connect to TWS at {IB_HOST}:{IB_PORT} (clientId={IB_CLIENT_ID})")
    app["conn_task"] = asyncio.create_task(maintain_connection())
    app["sample_task"] = asyncio.create_task(sample_equity())


async def on_cleanup(app):
    for key in ("conn_task", "sample_task"):
        task = app.get(key)
        if task:
            task.cancel()
    if ib.isConnected():
        ib.disconnect()


def main():
    util.patchAsyncio()
    app = web.Application(middlewares=[cors_mw])
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
    print(f"[ib_bridge] serving Alpaca-shaped IBKR data on http://127.0.0.1:{BRIDGE_PORT}")
    web.run_app(app, host="127.0.0.1", port=BRIDGE_PORT, print=None)


if __name__ == "__main__":
    main()
