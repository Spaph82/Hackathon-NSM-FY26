import {
  api, authStatus, login, send,
  classifyOrder, manualClientOrderId,
  money, signedMoney, pct, num, fmtTime, timeAgo, gainClass,
} from "./shared.js";

const $ = (id) => document.getElementById(id);

let state = {
  aiPrefix: "ai",
  broker: "alpaca",
  orders: [],
  filter: "all",
  range: "1M",
  history: null,
  liveEquity: null,
  mkt: { symbol: "", range: "1D", closes: [], baseline: null },
  timer: null,
  wired: false,
};

const QUICK = ["AAPL", "TSLA", "NVDA", "SPY", "BTC/USD", "ETH/USD"];
const CRYPTO_SHORT = { BTC: "BTC/USD", ETH: "ETH/USD", SOL: "SOL/USD", DOGE: "DOGE/USD", LTC: "LTC/USD" };
const POLL_MS = 15000;

function toast(msg, kind = "ok") {
  const t = $("toast");
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  setTimeout(() => (t.className = "toast"), 3200);
}

function showView(which) {
  $("gate").classList.toggle("hidden", which !== "gate");
  $("loading").classList.toggle("hidden", which !== "loading");
  $("main").classList.toggle("hidden", which !== "main");
}

function activeTab() {
  const b = [...$("tabs").children].find((x) => x.classList.contains("active"));
  return b ? b.dataset.v : "home";
}

// ---------------- boot ----------------

async function boot() {
  if (!state.wired) {
    wireControls();
    renderChips();
    state.wired = true;
  }
  if (state.timer) clearInterval(state.timer);
  showView("loading");
  loadAccounts(); // populate the switcher menu (non-blocking)

  let status;
  try {
    status = await authStatus();
  } catch (e) {
    toast(e.message, "err");
    showView("gate");
    return;
  }
  state.aiPrefix = status.aiPrefix || "ai";
  state.broker = status.broker;
  renderSwitcher(status);

  const venue = status.broker === "ibkr" ? "tws" : status.env;
  $("dot").className = "dot " + (status.authenticated ? "on" : "off");
  $("envLabel").textContent = status.authenticated ? venue : "offline";

  if (!status.authenticated) {
    const name = status.label || (status.broker === "ibkr" ? "IBKR" : "Alpaca");
    if (status.broker === "deribit") {
      $("gateMsg").textContent = "Start deribit_bridge.py — the VOLARB vol-arb engine trades a simulated account on live Deribit data.";
      $("connect").textContent = "⟁ Connect VOLARB";
    } else if (status.broker === "ibkr") {
      $("gateMsg").textContent = "Start TWS + ib_bridge.py, then connect to stream this account.";
      $("connect").textContent = "⟁ Connect IBKR";
    } else {
      $("gateMsg").textContent = status.configured
        ? `Connect to ${name} to start streaming your account.`
        : "First time here? Add your Alpaca API key & secret, then connect.";
      $("connect").textContent = "⟁ Connect Alpaca";
    }
    $("connect").disabled = !status.configured;
    showView("gate");
    return;
  }
  showView("main");
  await loadAll();
  state.timer = setInterval(tick, POLL_MS); // keep it live while the popup is open
}

// ---------------- account switcher ----------------

function brokerBadge(broker) {
  if (broker === "ibkr") return { cls: "badge-ibkr", text: "IB" };
  if (broker === "deribit") return { cls: "badge-deribit", text: "V" };
  return { cls: "badge-alpaca", text: "A" };
}

function renderSwitcher(status) {
  const b = brokerBadge(status.broker);
  const badge = $("acctBadge");
  badge.className = "acct-badge " + b.cls;
  badge.textContent = b.text;
  $("acctName").textContent = status.label || "Account";
}

async function loadAccounts() {
  let data;
  try {
    data = await send({ type: "accounts" });
  } catch {
    return;
  }
  const list = $("acctList");
  list.innerHTML = "";
  data.accounts.forEach((a) => {
    const b = brokerBadge(a.broker);
    const sub = a.broker === "ibkr" ? "IBKR · TWS"
      : a.broker === "deribit" ? "Deribit · vol-arb sim"
      : `Alpaca · ${a.env || "paper"}`;
    const li = document.createElement("li");
    const active = a.id === data.activeAccountId;
    li.innerHTML = `
      <button class="acct-row ${active ? "active" : ""}" data-id="${a.id}">
        <span class="li-badge ${b.cls}">${b.text}</span>
        <span class="li-main">
          <span class="li-label">${a.label}</span>
          <span class="li-sub">${sub}${a.verified ? " · live" : ""}</span>
        </span>
        <span class="li-dot"></span>
      </button>`;
    list.appendChild(li);
  });
}

function toggleMenu(force) {
  const open = force != null ? force : !$("acctMenu").classList.contains("open");
  $("acctMenu").classList.toggle("open", open);
  $("acctSwitch").classList.toggle("open", open);
}

async function switchAccount(id) {
  toggleMenu(false);
  try {
    await send({ type: "setActiveAccount", id });
  } catch (e) {
    return toast(e.message, "err");
  }
  // wipe per-account view state so nothing leaks across the switch
  state.orders = [];
  state.history = null;
  state.liveEquity = null;
  state.mkt = { symbol: "", range: "1D", closes: [], baseline: null };
  $("mktQuotePanel").style.display = "none";
  $("mktEmpty").style.display = "";
  switchTab("home");
  await boot();
}

async function loadAll() {
  await Promise.all([loadAccountAndPositions(), loadOrders(), loadHistory(state.range)]);
}

// ---------------- account + positions ----------------

async function loadAccountAndPositions() {
  try {
    const [account, positions] = await Promise.all([
      api({ path: "/v2/account" }),
      api({ path: "/v2/positions" }),
    ]);
    state.liveEquity = Number(account.equity);
    renderHome(account, positions);
    renderPositions(positions);
    renderEquityChart();
  } catch (e) {
    handleErr(e);
  }
}

function renderHome(account, positions) {
  $("hPortfolio").textContent = money(account.portfolio_value ?? account.equity);
  $("sBuying").textContent = money(account.buying_power);
  $("sCash").textContent = money(account.cash);
  $("sPosCount").textContent = String(positions.length);

  const today = Number(account.equity) - Number(account.last_equity);
  const todayPct = account.last_equity ? (today / Number(account.last_equity)) * 100 : 0;
  const tEl = $("hToday");
  tEl.textContent = `${signedMoney(today)} (${pct(todayPct)})`;
  tEl.className = gainClass(today);

  const unreal = positions.reduce((s, p) => s + Number(p.unrealized_pl || 0), 0);
  const uEl = $("sUnreal");
  uEl.textContent = signedMoney(unreal);
  uEl.className = "s-val " + gainClass(unreal);
}

function renderPositions(positions) {
  $("posCount").textContent = positions.length ? `· ${positions.length}` : "";
  const body = $("posBody");
  body.innerHTML = "";
  if (!positions.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">No open positions.</td></tr>`;
    return;
  }
  positions
    .sort((a, b) => Math.abs(Number(b.market_value)) - Math.abs(Number(a.market_value)))
    .forEach((p) => {
      const tr = document.createElement("tr");
      const plc = gainClass(p.unrealized_pl);
      tr.innerHTML = `
        <td class="sym">${p.symbol}</td>
        <td class="r">${num(p.qty, 2)}</td>
        <td class="r">${money(p.avg_entry_price)}</td>
        <td class="r">${money(p.current_price)}</td>
        <td class="r ${plc}">${signedMoney(p.unrealized_pl)}</td>
        <td class="r ${plc}">${pct(Number(p.unrealized_plpc) * 100)}</td>`;
      body.appendChild(tr);
    });
}

// ---------------- orders / timeline ----------------

async function loadOrders() {
  try {
    const orders = await api({
      path: "/v2/orders",
      query: { status: "all", limit: "200", direction: "desc", nested: "true" },
    });
    state.orders = orders;
    renderAiStrip();
    renderTimeline();
  } catch (e) {
    handleErr(e);
  }
}

function renderAiStrip() {
  const ai = state.orders.filter((o) => classifyOrder(o, state.aiPrefix) === "ai");
  const manual = state.orders.filter((o) => classifyOrder(o, state.aiPrefix) === "manual");
  const filledAi = ai.filter((o) => o.status === "filled");
  const aiNotional = filledAi.reduce(
    (s, o) => s + Number(o.filled_qty || 0) * Number(o.filled_avg_price || 0), 0
  );
  const stats = [
    { label: "AI orders", val: ai.length },
    { label: "AI filled", val: filledAi.length },
    { label: "AI volume", val: money(aiNotional) },
    { label: "Manual orders", val: manual.length },
  ];
  $("aiStrip").innerHTML = stats
    .map((s) => `<div class="ai-stat"><div class="s-val mono">${s.val}</div><div class="s-label">${s.label}</div></div>`)
    .join("");
}

function renderTimeline() {
  const wrap = $("timeline");
  let rows = state.orders;
  if (state.filter !== "all") {
    rows = rows.filter((o) => classifyOrder(o, state.aiPrefix) === state.filter);
  }
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty">No ${state.filter === "all" ? "" : state.filter + " "}orders yet.</div>`;
    return;
  }
  wrap.innerHTML = "";
  rows.forEach((o) => {
    const kind = classifyOrder(o, state.aiPrefix);
    const when = o.submitted_at || o.created_at;
    const filled = o.status === "filled" || o.status === "partially_filled";
    const priceTxt = filled && o.filled_avg_price
      ? `@ <b>${money(o.filled_avg_price)}</b>`
      : o.type === "limit" && o.limit_price
        ? `lmt ${money(o.limit_price)}`
        : o.type;
    const qtyTxt = o.filled_qty && Number(o.filled_qty) > 0
      ? `${num(o.filled_qty, 2)}/${num(o.qty, 2)}`
      : num(o.qty, 2);

    const row = document.createElement("div");
    row.className = "t-row " + kind;
    row.innerHTML = `
      <span class="chip ${o.side}">${o.side}</span>
      <div class="t-main">
        <div class="t-line1">
          <span class="t-sym">${o.symbol}</span>
          <span class="chip ${kind}">${kind === "ai" ? "🤖 AI" : "✋ Manual"}</span>
        </div>
        <div class="t-detail"><b>${qtyTxt}</b> ${priceTxt}</div>
      </div>
      <div class="t-right">
        <span class="chip status ${o.status}">${o.status.replace(/_/g, " ")}</span>
        <span class="t-time" title="${fmtTime(when)}">${timeAgo(when)}</span>
      </div>`;
    wrap.appendChild(row);
  });
}

// ---------------- generic line chart ----------------

function drawSeries(canvasId, rawValues, opts = {}) {
  const canvas = $(canvasId);
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const hgt = canvas.clientHeight;
  if (!w || !hgt) return; // panel hidden; will redraw on tab switch
  canvas.width = w * dpr;
  canvas.height = hgt * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);

  const values = (rawValues || []).filter((v) => v != null && isFinite(v)).map(Number);
  const setV = (id, txt, cls) => { if (id) { const e = $(id); e.textContent = txt; if (cls) e.className = cls; } };

  if (values.length < 2) {
    ctx.fillStyle = "#7d8db5";
    ctx.font = "12px system-ui";
    ctx.textAlign = "center";
    ctx.fillText("Not enough data yet.", w / 2, hgt / 2);
    setV(opts.valueEl, values.length ? money(values[0]) : "—");
    setV(opts.changeEl, "—", "");
    return;
  }

  const base = opts.baseline != null && isFinite(opts.baseline) ? Number(opts.baseline) : values[0];
  const first = values[0];
  const last = values[values.length - 1];
  const change = last - first;
  const changePct = first ? (change / first) * 100 : 0;
  const rising = change >= 0;
  const line = rising ? "#2bff88" : "#ff4d6d";

  setV(opts.valueEl, money(last));
  setV(opts.changeEl, `${signedMoney(change)} (${pct(changePct)})`, gainClass(change));

  const all = values.concat(base);
  let min = Math.min(...all), max = Math.max(...all);
  if (min === max) { min -= 1; max += 1; }
  const padV = (max - min) * 0.12;
  min -= padV; max += padV;

  const padL = 6, padR = 6, padT = 8, padB = 8;
  const plotW = w - padL - padR;
  const plotH = hgt - padT - padB;
  const x = (i) => padL + (i / (values.length - 1)) * plotW;
  const y = (v) => padT + (1 - (v - min) / (max - min)) * plotH;

  // baseline
  ctx.strokeStyle = "rgba(125,141,181,0.35)";
  ctx.setLineDash([4, 4]);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, y(base));
  ctx.lineTo(w - padR, y(base));
  ctx.stroke();
  ctx.setLineDash([]);

  // area
  const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  grad.addColorStop(0, rising ? "rgba(43,255,136,0.28)" : "rgba(255,77,109,0.28)");
  grad.addColorStop(1, "rgba(43,255,136,0)");
  ctx.beginPath();
  ctx.moveTo(x(0), y(values[0]));
  values.forEach((v, i) => ctx.lineTo(x(i), y(v)));
  ctx.lineTo(x(values.length - 1), padT + plotH);
  ctx.lineTo(x(0), padT + plotH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // line + glow
  ctx.shadowColor = line;
  ctx.shadowBlur = 10;
  ctx.strokeStyle = line;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.beginPath();
  values.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.stroke();
  ctx.shadowBlur = 0;

  // end marker
  ctx.fillStyle = line;
  ctx.beginPath();
  ctx.arc(x(values.length - 1), y(last), 3, 0, Math.PI * 2);
  ctx.fill();
}

// ---------------- equity (live balance) ----------------

const RANGE_MAP = {
  "1D": { period: "1D", timeframe: "5Min", extended_hours: "true" },
  "1W": { period: "1W" },
  "1M": { period: "1M" },
  "3M": { period: "3M" },
  "1Y": { period: "1A" },
};

async function loadHistory(range) {
  state.range = range;
  try {
    const h = await api({ path: "/v2/account/portfolio/history", query: RANGE_MAP[range] });
    state.history = h;
    renderEquityChart();
  } catch (e) {
    handleErr(e);
  }
}

function renderEquityChart() {
  if (!state.history) return;
  const h = state.history;
  const values = (h.equity || []).filter((v) => v != null && isFinite(v)).map(Number);
  // append the real current balance so the curve ends at "now"
  if (state.liveEquity != null && isFinite(state.liveEquity)) values.push(Number(state.liveEquity));
  const base = Number(h.base_value) || (values.length ? values[0] : 0);
  drawSeries("chart", values, { baseline: base, valueEl: "chartValue", changeEl: "chartChange" });
}

// ---------------- markets ----------------

function normalizeSymbol(raw) {
  let s = (raw || "").trim().toUpperCase();
  if (!s) return "";
  if (!s.includes("/") && CRYPTO_SHORT[s]) return CRYPTO_SHORT[s];
  return s;
}
const isCrypto = (s) => s.includes("/");

const BAR_MAP = {
  "1D": { tf: "5Min", days: 1 },
  "1W": { tf: "1H", days: 7 },
  "1M": { tf: "1D", days: 34 },
  "3M": { tf: "1D", days: 95 },
  "1Y": { tf: "1D", days: 370 },
};

async function getSnapshot(sym) {
  if (isCrypto(sym)) {
    const d = await api({ dataApi: true, path: "/v1beta3/crypto/us/snapshots", query: { symbols: sym } });
    const s = d.snapshots && d.snapshots[sym];
    if (!s) throw new Error("No market data for " + sym);
    return s;
  }
  return api({ dataApi: true, path: `/v2/stocks/${encodeURIComponent(sym)}/snapshot`, query: { feed: "iex" } });
}

async function getCloses(sym, range) {
  const { tf, days } = BAR_MAP[range];
  const start = new Date(Date.now() - days * 86400000).toISOString();
  if (isCrypto(sym)) {
    const d = await api({ dataApi: true, path: "/v1beta3/crypto/us/bars", query: { symbols: sym, timeframe: tf, start, limit: "1000" } });
    return ((d.bars && d.bars[sym]) || []).map((b) => Number(b.c));
  }
  const d = await api({ dataApi: true, path: `/v2/stocks/${encodeURIComponent(sym)}/bars`, query: { timeframe: tf, start, limit: "1000", feed: "iex" } });
  return (d.bars || []).map((b) => Number(b.c));
}

async function loadMarket(rawSym, range) {
  const sym = normalizeSymbol(rawSym);
  if (!sym) return;
  state.mkt.symbol = sym;
  state.mkt.range = range;
  $("mSymbol").value = sym;
  try {
    const snap = await getSnapshot(sym);
    renderQuote(sym, snap);
    const closes = await getCloses(sym, range);
    state.mkt.closes = closes;
    const prevClose = (snap.prevDailyBar && snap.prevDailyBar.c) ??
      (snap.dailyBar && snap.dailyBar.o) ?? (closes.length ? closes[0] : null);
    state.mkt.baseline = prevClose;
    $("mktEmpty").style.display = "none";
    $("mktQuotePanel").style.display = "";
    renderPriceChart();
  } catch (e) {
    if (e.message === "NOT_AUTHENTICATED") return handleErr(e);
    toast(e.message, "err");
  }
}

function renderQuote(sym, snap) {
  const price = (snap.latestTrade && snap.latestTrade.p) ??
    (snap.minuteBar && snap.minuteBar.c) ?? (snap.dailyBar && snap.dailyBar.c);
  const prev = snap.prevDailyBar && snap.prevDailyBar.c;
  const daily = snap.dailyBar || {};
  $("mqSym").textContent = sym;
  $("mqPrice").textContent = price != null ? money(price) : "—";
  const cEl = $("mqChange");
  if (price != null && prev) {
    const ch = price - prev, chp = (ch / prev) * 100;
    cEl.textContent = `${signedMoney(ch)} (${pct(chp)}) · today`;
    cEl.className = "mq-change " + gainClass(ch);
  } else {
    cEl.textContent = "";
  }
  $("mqStats").innerHTML = `
    <div>O <b>${daily.o != null ? money(daily.o) : "—"}</b></div>
    <div>H <b>${daily.h != null ? money(daily.h) : "—"}</b></div>
    <div>L <b>${daily.l != null ? money(daily.l) : "—"}</b></div>
    <div>Vol <b>${daily.v != null ? num(daily.v, 0) : "—"}</b></div>`;
}

function renderPriceChart() {
  if (!state.mkt.symbol) return;
  drawSeries("mktChart", state.mkt.closes, { baseline: state.mkt.baseline });
}

function renderChips() {
  $("mChips").innerHTML = "";
  QUICK.forEach((s) => {
    const b = document.createElement("button");
    b.className = "mkt-chip";
    b.textContent = s;
    b.dataset.sym = s;
    $("mChips").appendChild(b);
  });
}

// ---------------- order placement ----------------

async function placeOrder(side) {
  const symbol = $("oSymbol").value.trim().toUpperCase();
  const qty = $("oQty").value.trim();
  const type = $("oType").value;
  const tif = $("oTif").value;
  const limit = $("oLimit").value.trim();

  if (!symbol) return toast("Enter a symbol.", "err");
  if (!qty || Number(qty) <= 0) return toast("Enter a quantity.", "err");
  if (type === "limit" && (!limit || Number(limit) <= 0)) return toast("Enter a limit price.", "err");

  const body = {
    symbol, qty, side, type,
    time_in_force: tif,
    client_order_id: manualClientOrderId(),
  };
  if (type === "limit") body.limit_price = limit;

  const btn = side === "buy" ? $("buy") : $("sell");
  btn.disabled = true;
  try {
    const o = await api({ method: "POST", path: "/v2/orders", body });
    toast(`${side.toUpperCase()} ${qty} ${symbol} submitted (${o.status}).`, "ok");
    $("oSymbol").value = "";
    $("oQty").value = "";
    $("oLimit").value = "";
    await Promise.all([loadOrders(), loadAccountAndPositions()]);
    switchTab("orders");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

// ---------------- polling tick ----------------

async function tick() {
  if ($("main").classList.contains("hidden")) return;
  try {
    const [account, positions] = await Promise.all([
      api({ path: "/v2/account" }),
      api({ path: "/v2/positions" }),
    ]);
    state.liveEquity = Number(account.equity);
    renderHome(account, positions);
    renderPositions(positions);
    renderEquityChart();
    await loadOrders();
    if (activeTab() === "markets" && state.mkt.symbol) {
      const snap = await getSnapshot(state.mkt.symbol);
      renderQuote(state.mkt.symbol, snap);
      if (state.mkt.range === "1D") {
        state.mkt.closes = await getCloses(state.mkt.symbol, "1D");
        renderPriceChart();
      }
    }
  } catch (e) {
    if (e.message === "NOT_AUTHENTICATED") handleErr(e);
  }
}

// ---------------- tabs / controls ----------------

function switchTab(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  [...$("tabs").children].forEach((b) => b.classList.toggle("active", b.dataset.v === name));
  if (name === "home") renderEquityChart();
  if (name === "markets") renderPriceChart();
}

function wireControls() {
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) switchTab(b.dataset.v);
  });
  $("refresh").addEventListener("click", loadAll);
  $("openSetup").addEventListener("click", () => chrome.runtime.openOptionsPage());

  // account switcher
  $("acctSwitch").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleMenu();
  });
  $("acctList").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-id]");
    if (b) switchAccount(b.dataset.id);
  });
  $("mManage").addEventListener("click", () => chrome.runtime.openOptionsPage());
  $("mDisconnect").addEventListener("click", async () => {
    toggleMenu(false);
    await send({ type: "logout" });
    boot();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".switch-wrap")) toggleMenu(false);
  });

  $("connect").addEventListener("click", async () => {
    $("connect").disabled = true;
    try {
      await login();
      await boot();
      toast("Connected", "ok");
    } catch (e) {
      toast(e.message, "err");
      $("connect").disabled = false;
    }
  });

  // trade ticket
  $("oType").addEventListener("change", (e) => {
    $("limitField").style.display = e.target.value === "limit" ? "" : "none";
  });
  $("buy").addEventListener("click", () => placeOrder("buy"));
  $("sell").addEventListener("click", () => placeOrder("sell"));

  // home range
  $("range").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    [...$("range").children].forEach((c) => c.classList.toggle("active", c === b));
    loadHistory(b.dataset.p);
  });

  // orders filter
  $("filters").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    [...$("filters").children].forEach((c) => c.classList.toggle("active", c === b));
    state.filter = b.dataset.f;
    renderTimeline();
  });

  // markets
  $("mGo").addEventListener("click", () => loadMarket($("mSymbol").value, state.mkt.range));
  $("mSymbol").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadMarket($("mSymbol").value, state.mkt.range);
  });
  $("mChips").addEventListener("click", (e) => {
    const b = e.target.closest(".mkt-chip");
    if (b) loadMarket(b.dataset.sym, state.mkt.range);
  });
  $("mktRange").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    [...$("mktRange").children].forEach((c) => c.classList.toggle("active", c === b));
    if (state.mkt.symbol) loadMarket(state.mkt.symbol, b.dataset.p);
  });
}

function handleErr(e) {
  if (e.message === "NOT_AUTHENTICATED") {
    if (state.timer) clearInterval(state.timer);
    $("dot").className = "dot off";
    $("envLabel").textContent = "offline";
    $("gateMsg").textContent = "Session ended. Reconnect to Alpaca.";
    $("connect").disabled = false;
    showView("gate");
  } else {
    toast(e.message, "err");
  }
}

boot();
