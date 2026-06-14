# Alcala Trading Console

A Chrome extension (Manifest V3, vanilla JS — no build step) that connects to an
**Alpaca** paper-trading account with an API key + secret. Everything lives in a single
tabbed **popup** — tap the bottom tabs to move between views, no separate page to open:

- **Home** — portfolio value, today's P&L, unrealized P&L, buying power, cash, and a live
  **equity curve** (1D / 1W / 1M / 3M / 1Y).
- **Positions** — your open positions with P&L.
- **Orders** — a **timeline** that tags each order 🤖 **AI** or ✋ **Manual**, with AI stats and filters.
- **Trade** — a manual order ticket (market/limit, buy/sell) that posts straight to your account.

> Built for the IFX hackathon. "Paper" by default, with a Live toggle included.

---

## Multiple accounts & brokers

The top-left badge in the popup is an **account switcher** (Uiverse-style dropdown). It
lists every connected account; pick one to make it active and the whole console re-streams
that account. Two brokers are supported:

- **Alpaca** — direct cloud REST (API key + secret).
- **Interactive Brokers** — *live* via your **Trader Workstation (TWS)** + a small local
  bridge (`ib_bridge.py`). TWS pulls real data from IBKR; the bridge just translates it.

The service worker routes each request to the active account's **broker adapter** and
normalizes everything to Alpaca's response shape, so the popup is broker-agnostic.

## Architecture

```
manifest.json          MV3 manifest (storage + alpaca + localhost host permissions)
background.js           Service worker: accounts registry + per-broker routing/auth gateway.
                        Alpaca → cloud REST; IBKR → local ib_bridge.py. The popup posts
                        normalized messages and never sees credentials.
ib_bridge.py            Local TWS↔extension bridge: ib_insync → Alpaca-shaped JSON (port 8788)
engine.py               (standalone) Deribit BTC-options vol-arb signal engine
src/
  theme.css             Neo-brutalist "sticker" design system (stone + yellow, hard shadows)
  shared.js             Messaging client + formatting + AI/manual tagging logic
  setup.html / .js      Accounts manager (Alpaca creds + IBKR bridge URL) — the options page
  popup.html/.css/.js   The whole console — switcher + tabs: Home, Markets, Positions, Orders, Trade
icons/                  Generated gradient icons (16/48/128)
```

Alpaca auth uses its header scheme (`APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`); IBKR auth is
held by TWS itself. Credentials live only in `chrome.storage.local`.

---

## Setup (one time)

### 1. Get your Alpaca API keys
1. Sign in at [app.alpaca.markets](https://app.alpaca.markets).
2. For paper trading, switch to the **Paper** account (top-left toggle).
3. In the **Home** panel on the right, open **API Keys → Generate / View** and copy the
   **Key ID** and **Secret Key** (the secret is shown only once — regenerate if you lose it).

> Paper keys start with `PK…` and only work with the **Paper** environment; live keys
> start with `AK…` and only work with **Live**. A mismatch returns 403.

### 2. Load the extension in Chrome
1. Open `chrome://extensions`, toggle **Developer mode** (top right).
2. Click **Load unpacked** and select this folder (`ifx_hackathon`). Pin it for convenience.

### 3. Enter credentials & connect
1. Click the extension icon → open the **account switcher** (top-left badge) → **Manage accounts**
   (or right-click the icon → **Options**).
2. Under **Alpaca**, paste your **Key ID** and **Secret Key**, choose **Paper**/**Live**, set the
   **AI tag prefix** (default `ai`), and **Save accounts**.
3. Back in the popup, hit **Connect** (it calls `/v2/account` to verify), then use the tabs —
   **Home · Markets · Positions · Orders · Trade**.

### 4. (Optional) Add Interactive Brokers — live via TWS
1. In **TWS → Global Configuration → API → Settings**: enable **"ActiveX and Socket Clients"**,
   set the socket port to `7497` (paper) / `7496` (live), add `127.0.0.1` to **Trusted IPs**, and
   **uncheck "Read-Only API"** if you want to place orders. Keep TWS logged in.
2. Run the bridge:
   ```bash
   pip install ib_insync aiohttp
   python ib_bridge.py        # serves http://127.0.0.1:8788
   ```
   (Use `IB_PORT=7496` for live.)
3. In the popup's **account switcher → IBKR · TWS → Connect**. The bridge translates TWS data
   into the same shape the console already speaks, so every tab just works.

> The IBKR equity curve is a 2-point placeholder (TWS exposes no native equity history) and the
> bridge handles US stocks; everything else (account, positions, orders, quotes, trading) is live.

### 5. (Optional) Add Deribit — simulated AI options trading on live data
The `Deribit · Sim` account runs the BTC-options volatility-arbitrage **engine**
(`engine/Hackathon-NSM-FY26/engine.py`): it streams **real live Deribit prices**, finds
mispriced options, and **paper-trades** them delta-neutral with fee-aware P&L. No keys, no
real orders. The extension shows the simulated balance, the engine's positions, its trades
(tagged 🤖 AI in the timeline), and a live P&L curve.

```bash
pip install -r engine/Hackathon-NSM-FY26/requirements.txt aiohttp
python deribit_bridge.py        # serves http://127.0.0.1:8789
```
Then in the popup's **account switcher → Deribit · Sim → Connect**. Give the engine a few
seconds to connect to Deribit and start finding trades.

The same bridge also serves a **live VOLARB desk** at **http://127.0.0.1:8789/** — the
`backtest_result.html` graphs (market-ask vs AI fair value, live P&L curve, BTC spot) but
streaming in real time, styled to match the extension. It reads the engine's raw state from
`/state`.

> Use the **same Python** you installed the deps into. The bridge runs the engine in-process
> and translates its `state` into the same Alpaca-shaped JSON the console already speaks.

---

## How AI vs Manual tagging works

Alpaca doesn't store "was this order from an AI?", so the console uses the order's
**`client_order_id`** as the signal:

- An order is shown as **🤖 AI** when its `client_order_id` starts with your configured
  prefix, e.g. `ai-...` / `ai_...` (default prefix `ai`).
- Manual orders placed from this extension's ticket are stamped `manual-...` and shown as **✋ Manual**.
- Anything else falls back to Manual.

**So make your AI model set `client_order_id` accordingly when it places orders.** Example:

```python
# alpaca-py
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

trading_client.submit_order(MarketOrderRequest(
    symbol="AAPL",
    qty=10,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
    client_order_id=f"ai-{uuid4().hex[:8]}",   # <-- tags it as AI in the console
))
```

```bash
# raw REST
curl -X POST https://paper-api.alpaca.markets/v2/orders \
  -H "APCA-API-KEY-ID: $KEY" -H "APCA-API-SECRET-KEY: $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","qty":10,"side":"buy","type":"market","time_in_force":"day","client_order_id":"ai-7f3c1a2b"}'
```

If you'd rather use a different prefix (e.g. `bot`), set it in the setup page.

---

## Alpaca endpoints used

| Purpose | Call |
|---|---|
| Verify credentials / KPIs | `GET /v2/account` |
| Positions | `GET /v2/positions` |
| Order history (timeline) | `GET /v2/orders?status=all&direction=desc&nested=true` |
| Equity curve | `GET /v2/account/portfolio/history` |
| Place order | `POST /v2/orders` |

Base URL is `https://paper-api.alpaca.markets` (paper) or `https://api.alpaca.markets` (live),
selected in setup. Every call sends the `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers.

---

## Notes & limits

- **Hackathon-grade auth.** The secret lives in the extension's local storage — fine for a
  personal paper account / demo. For a published extension you'd proxy calls through a
  backend so the secret never reaches the client.
- **"Disconnect"** clears the verified flag (so the next Connect re-checks the keys); your
  saved keys stay in setup until you change them.
- No external scripts/CDNs — the equity chart is drawn on a `<canvas>`, so it complies with
  the MV3 content-security policy out of the box.
