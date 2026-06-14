# VOLARB — BTC Options Volatility Desk (iFX Hack 2026)

Live volatility-arbitrage engine + paper-trading bot + dashboard.
Real Deribit market data · simulated (paper) execution · no real orders.

## Setup (once, at the venue)
```bash
pip install -r requirements.txt
```
No extra setup for the dashboard — the engine serves it itself.

## Run (this is the whole demo)
```bash
python engine.py
```
Then open a browser to:  **http://localhost:8765**

The terminal keeps streaming `[TICK]`/`[ARB]` lines (proof it's real);
the browser shows the polished live dashboard for the judges.

If it can't connect to Deribit, the venue WiFi may block the WebSocket —
switch to a phone hotspot. TEST THIS EARLY.

## What's on the dashboard
- BTC spot + live connection status
- Paper P&L, fees paid, net Delta, net Vega, model accuracy, open/closed positions
- Market price vs AI fair value, with the gap shaded (the detected mispricing)
- Paper P&L curve vs cumulative fees (the honest fee-drag story)
- Forecast accuracy: our model's error vs a naive "no-change" baseline
- Open paper positions + the live signal feed

## What the paper-trading layer does (say this honestly)
On every flagged-cheap option it: buys it (paper), shorts the right amount of
BTC to go delta-neutral, rebalances the hedge as BTC moves, and charges a
realistic fee (0.05% of notional) on every rebalance. It is SIMULATED execution
on REAL prices — paper trading, the way a desk validates a strategy before
risking capital. No real orders are placed.

## The honest pitch (your edge with this audience)
> "We connect live to Deribit, back out the volatility the market implies for
> every BTC option, and our ML model — training in real time — forecasts where
> vol is heading. Black-Scholes turns that into a fair value; the shaded gap on
> screen is the mispricing we detect. We paper-trade it: buy the cheap option,
> hedge out the direction, harvest the movement — and we show the fees eating
> into P&L, because that's the real tension in volatility trading.
> We stay market-neutral [point at net Delta]. We're honest that profitability
> depends on beating the market's vol forecast, which is the hard part — so we
> measure our model against a naive baseline rather than claiming an edge we
> haven't proven."

## Anticipated judge questions — short answers
- **Is it real or a sandbox?** Real live Deribit data; execution is simulated (paper).
- **Would it make money?** Not as a weekend build — the edge (beating market vol) is
  unproven and fees are a serious drag. It's the detection front-end of a real strategy.
- **Why not deep learning?** Complex models overfit on crypto's limited data; a simple
  model + good features is the documented right call. GARCH is our next step.
- **Is your accuracy real?** We compare against a naive no-change baseline — short-horizon
  IV is very persistent, so beating it is the honest hard test.
- **Why only buy signals?** Selling options has unbounded risk and needs margin modelling;
  we scoped to the bounded-risk long side deliberately.

## Demo safety
Record a 60-second screen capture of the dashboard running cleanly as a backup,
in case the WiFi drops mid-presentation.
