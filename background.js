// Alcala Trading Console — service worker
// Holds per-account credentials and routes every request to the right broker
// adapter. Alpaca is called directly (cloud REST); IBKR goes through a local
// ib_bridge.py that mirrors the Alpaca REST surface and returns Alpaca-shaped
// JSON, so the popup UI is broker-agnostic.

const ALPACA_BASE = {
  paper: "https://paper-api.alpaca.markets",
  live: "https://api.alpaca.markets",
};
const DATA_BASE = "https://data.alpaca.markets";
const DEFAULT_IBKR_BRIDGE = "http://127.0.0.1:8788";

// ---------- accounts registry ----------

async function getState() {
  const s = await chrome.storage.local.get({
    accounts: null,
    activeAccountId: null,
    aiPrefix: "ai",
    // legacy single-account fields (pre-multi-account):
    apiKey: "",
    apiSecret: "",
    env: "paper",
    verified: false,
  });

  let { accounts, activeAccountId } = s;
  if (!accounts) {
    // migrate the old single Alpaca config into a two-account registry
    accounts = [
      {
        id: "alpaca",
        label: s.env === "live" ? "Alpaca Live" : "Alpaca Paper",
        broker: "alpaca",
        color: "#facc15",
        apiKey: s.apiKey,
        apiSecret: s.apiSecret,
        env: s.env || "paper",
        verified: !!s.verified,
      },
      {
        id: "ibkr",
        label: "IBKR · TWS",
        broker: "ibkr",
        color: "#d4232a",
        bridgeUrl: DEFAULT_IBKR_BRIDGE,
        verified: false,
      },
      DERIBIT_ACCOUNT(),
    ];
    activeAccountId = "alpaca";
    await chrome.storage.local.set({ accounts, activeAccountId, aiPrefix: s.aiPrefix });
  }
  // upgrade older registries that predate the Deribit account
  if (accounts && !accounts.find((a) => a.broker === "deribit")) {
    accounts.push(DERIBIT_ACCOUNT());
    await chrome.storage.local.set({ accounts });
  }
  // rename the old default Deribit label to VOLARB (preserves user-set labels)
  const dbAcct = accounts.find((a) => a.broker === "deribit");
  if (dbAcct && dbAcct.label === "Deribit · Sim") {
    dbAcct.label = "VOLARB";
    await chrome.storage.local.set({ accounts });
  }
  if (!activeAccountId || !accounts.find((a) => a.id === activeAccountId)) {
    activeAccountId = accounts[0].id;
  }
  return { accounts, activeAccountId, aiPrefix: s.aiPrefix };
}

function DERIBIT_ACCOUNT() {
  return {
    id: "deribit",
    label: "VOLARB",
    broker: "deribit",
    color: "#10b981",
    bridgeUrl: "http://127.0.0.1:8789",
    verified: false,
  };
}

async function getActive() {
  const { accounts, activeAccountId } = await getState();
  return accounts.find((a) => a.id === activeAccountId) || accounts[0];
}

async function patchAccount(id, patch) {
  const { accounts } = await getState();
  const i = accounts.findIndex((a) => a.id === id);
  if (i === -1) return;
  accounts[i] = { ...accounts[i], ...patch };
  await chrome.storage.local.set({ accounts });
  return accounts[i];
}

const BRIDGE_BROKERS = new Set(["ibkr", "deribit"]);

function isConfigured(a) {
  return BRIDGE_BROKERS.has(a.broker) ? !!a.bridgeUrl : !!(a.apiKey && a.apiSecret);
}

// ---------- adapters ----------

function alpacaHeaders(a) {
  return { "APCA-API-KEY-ID": a.apiKey, "APCA-API-SECRET-KEY": a.apiSecret };
}

async function alpacaFetch(a, { method = "GET", path, query, body, dataApi = false }) {
  if (!a.apiKey || !a.apiSecret) throw new Error("NOT_AUTHENTICATED");
  const base = dataApi ? DATA_BASE : ALPACA_BASE[a.env] || ALPACA_BASE.paper;
  let url = base + path;
  if (query && Object.keys(query).length) url += "?" + new URLSearchParams(query).toString();

  const resp = await fetch(url, {
    method,
    headers: { ...alpacaHeaders(a), "Content-Type": "application/json", Accept: "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401 || resp.status === 403) {
    await patchAccount(a.id, { verified: false });
    throw new Error("NOT_AUTHENTICATED");
  }
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) throw new Error(data && data.message ? data.message : text || `HTTP ${resp.status}`);
  return data;
}

function bridgeName(a) {
  return a.broker === "deribit"
    ? "Deribit bridge (deribit_bridge.py)"
    : "IBKR bridge (ib_bridge.py + TWS)";
}

async function bridgeFetch(a, { method = "GET", path, query, body }) {
  const base = (a.bridgeUrl || "").replace(/\/+$/, "");
  let url = base + path;
  if (query && Object.keys(query).length) url += "?" + new URLSearchParams(query).toString();

  let resp;
  try {
    resp = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error(`${bridgeName(a)} unreachable — is it running?`);
  }
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) throw new Error(data && data.message ? data.message : text || `Bridge HTTP ${resp.status}`);
  return data;
}

async function apiFetch(request) {
  const a = await getActive();
  if (!isConfigured(a)) throw new Error("NOT_AUTHENTICATED");
  return BRIDGE_BROKERS.has(a.broker) ? bridgeFetch(a, request) : alpacaFetch(a, request);
}

// ---------- connect / verify ----------

async function connect() {
  const a = await getActive();

  if (BRIDGE_BROKERS.has(a.broker)) {
    const base = (a.bridgeUrl || "").replace(/\/+$/, "");
    let resp;
    try {
      resp = await fetch(base + "/health");
    } catch {
      throw new Error(`${bridgeName(a)} unreachable — run it first.`);
    }
    if (!resp.ok) throw new Error(`Bridge responded ${resp.status}.`);
    const h = await resp.json().catch(() => ({}));
    if (h && h.connected === false) {
      throw new Error(a.broker === "deribit"
        ? "Deribit bridge is up but the engine hasn't connected to Deribit yet — give it a few seconds and retry."
        : "Bridge is up but not connected to TWS. Open TWS, enable the API on port 7497, then retry.");
    }
    await patchAccount(a.id, { verified: true });
    return { broker: a.broker, label: a.label };
  }

  // alpaca
  if (!a.apiKey || !a.apiSecret) {
    throw new Error("Missing Alpaca credentials. Open Accounts and add your API key & secret.");
  }
  const base = ALPACA_BASE[a.env] || ALPACA_BASE.paper;
  const resp = await fetch(`${base}/v2/account`, { headers: alpacaHeaders(a) });
  if (resp.status === 401 || resp.status === 403) {
    await patchAccount(a.id, { verified: false });
    throw new Error("Alpaca rejected these credentials (401/403). Check the key, secret, and Paper/Live setting.");
  }
  if (!resp.ok) {
    const t = await resp.text();
    throw new Error(`Could not reach Alpaca (${resp.status}): ${t}`);
  }
  await patchAccount(a.id, { verified: true });
  return { broker: "alpaca", env: a.env, label: a.label };
}

// ---------- message router ----------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      switch (msg.type) {
        case "authStatus": {
          const { accounts, activeAccountId, aiPrefix } = await getState();
          const a = accounts.find((x) => x.id === activeAccountId) || accounts[0];
          const configured = isConfigured(a);
          sendResponse({
            ok: true,
            data: {
              authenticated: configured && !!a.verified,
              configured,
              env: a.env || null,
              aiPrefix,
              broker: a.broker,
              label: a.label,
              color: a.color,
              activeAccountId,
            },
          });
          break;
        }
        case "accounts": {
          const { accounts, activeAccountId } = await getState();
          sendResponse({
            ok: true,
            data: {
              activeAccountId,
              accounts: accounts.map((a) => ({
                id: a.id,
                label: a.label,
                broker: a.broker,
                color: a.color,
                env: a.env || null,
                configured: isConfigured(a),
                verified: !!a.verified,
              })),
            },
          });
          break;
        }
        case "setActiveAccount": {
          const { accounts } = await getState();
          if (!accounts.find((a) => a.id === msg.id)) throw new Error("Unknown account");
          await chrome.storage.local.set({ activeAccountId: msg.id });
          sendResponse({ ok: true });
          break;
        }
        case "login": {
          sendResponse({ ok: true, data: await connect() });
          break;
        }
        case "logout": {
          const a = await getActive();
          await patchAccount(a.id, { verified: false });
          sendResponse({ ok: true });
          break;
        }
        case "api": {
          sendResponse({ ok: true, data: await apiFetch(msg.request) });
          break;
        }
        default:
          sendResponse({ ok: false, error: `Unknown message type: ${msg.type}` });
      }
    } catch (e) {
      sendResponse({ ok: false, error: e.message || String(e) });
    }
  })();
  return true; // async response
});
