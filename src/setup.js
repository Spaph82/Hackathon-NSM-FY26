import { send } from "./shared.js";

const $ = (id) => document.getElementById(id);

function toast(msg, kind = "ok") {
  const t = $("toast");
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  setTimeout(() => (t.className = "toast"), 3000);
}

async function init() {
  // touch the worker so the accounts registry is migrated/created, then read it
  await send({ type: "authStatus" });
  const s = await chrome.storage.local.get({ accounts: [], aiPrefix: "ai" });

  const al = s.accounts.find((a) => a.broker === "alpaca") || {};
  const ib = s.accounts.find((a) => a.broker === "ibkr") || {};
  const db = s.accounts.find((a) => a.broker === "deribit") || {};

  $("al_label").value = al.label || "Alpaca Paper";
  $("al_key").value = al.apiKey || "";
  $("al_secret").value = al.apiSecret || "";
  $("al_env").value = al.env || "paper";

  $("ib_label").value = ib.label || "IBKR · TWS";
  $("ib_url").value = ib.bridgeUrl || "http://127.0.0.1:8788";

  $("db_label").value = db.label || "VOLARB";
  $("db_url").value = db.bridgeUrl || "http://127.0.0.1:8789";

  $("aiPrefix").value = s.aiPrefix || "ai";

  $("save").addEventListener("click", save);
}

async function save() {
  const s = await chrome.storage.local.get({ accounts: [], aiPrefix: "ai" });
  const accounts = s.accounts.slice();

  const upsert = (id, broker, patch) => {
    const i = accounts.findIndex((a) => a.id === id);
    const prev = i === -1 ? { id, broker } : accounts[i];
    const next = { ...prev, ...patch };
    if (i === -1) accounts.push(next);
    else accounts[i] = next;
  };

  const alKey = $("al_key").value.trim();
  const alSecret = $("al_secret").value.trim();
  const alEnv = $("al_env").value;
  // changing creds invalidates the verified flag
  const alPrev = accounts.find((a) => a.id === "alpaca") || {};
  const alChanged = alPrev.apiKey !== alKey || alPrev.apiSecret !== alSecret || alPrev.env !== alEnv;
  upsert("alpaca", "alpaca", {
    label: $("al_label").value.trim() || "Alpaca",
    color: alPrev.color || "#facc15",
    apiKey: alKey,
    apiSecret: alSecret,
    env: alEnv,
    verified: alChanged ? false : !!alPrev.verified,
  });

  const ibUrl = ($("ib_url").value.trim() || "http://127.0.0.1:8788").replace(/\/+$/, "");
  const ibPrev = accounts.find((a) => a.id === "ibkr") || {};
  upsert("ibkr", "ibkr", {
    label: $("ib_label").value.trim() || "IBKR · TWS",
    color: ibPrev.color || "#d4232a",
    bridgeUrl: ibUrl,
    verified: ibPrev.bridgeUrl !== ibUrl ? false : !!ibPrev.verified,
  });

  const dbUrl = ($("db_url").value.trim() || "http://127.0.0.1:8789").replace(/\/+$/, "");
  const dbPrev = accounts.find((a) => a.id === "deribit") || {};
  upsert("deribit", "deribit", {
    label: $("db_label").value.trim() || "VOLARB",
    color: dbPrev.color || "#10b981",
    bridgeUrl: dbUrl,
    verified: dbPrev.bridgeUrl !== dbUrl ? false : !!dbPrev.verified,
  });

  await chrome.storage.local.set({
    accounts,
    aiPrefix: ($("aiPrefix").value.trim() || "ai").toLowerCase(),
  });
  $("saved").textContent = "✓ saved locally";
  toast("Accounts saved. Connect from the console.");
}

init();
