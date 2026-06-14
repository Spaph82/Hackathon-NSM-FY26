// Shared client-side helpers for the popup app.
// All Alpaca traffic is proxied through the service worker via messages.

export function send(msg) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(msg, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!resp) {
        reject(new Error("No response from background worker."));
        return;
      }
      if (resp.ok) resolve(resp.data);
      else reject(new Error(resp.error || "Request failed"));
    });
  });
}

// Authenticated Alpaca call. `request` = { method, path, query, body }
export const api = (request) => send({ type: "api", request });
export const authStatus = () => send({ type: "authStatus" });
export const login = () => send({ type: "login" });
export const logout = () => send({ type: "logout" });

// ---------- AI vs manual tagging ----------
// Convention: an order is "AI" when its client_order_id starts with the
// configured prefix (default "ai"), e.g. "ai-7f3c". Manual orders placed by
// this extension are stamped with "manual-...". Anything else is "manual".

export function classifyOrder(order, aiPrefix) {
  const cid = (order.client_order_id || "").toLowerCase();
  const prefix = (aiPrefix || "ai").toLowerCase();
  if (cid.startsWith(prefix + "-") || cid.startsWith(prefix + "_") || cid === prefix) {
    return "ai";
  }
  return "manual";
}

export function manualClientOrderId() {
  const bytes = crypto.getRandomValues(new Uint8Array(4));
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `manual-${Date.now().toString(36)}-${hex}`;
}

// ---------- formatting ----------

export function money(n, currency = "USD") {
  const v = Number(n);
  if (!isFinite(v)) return "—";
  return v.toLocaleString("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function signedMoney(n) {
  const v = Number(n);
  if (!isFinite(v)) return "—";
  const s = money(Math.abs(v));
  return v < 0 ? `−${s}` : `+${s}`;
}

export function pct(n) {
  const v = Number(n);
  if (!isFinite(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${Math.abs(v).toFixed(2)}%`;
}

export function num(n, digits = 2) {
  const v = Number(n);
  if (!isFinite(v)) return "—";
  return v.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function timeAgo(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const diff = Date.now() - t;
  const s = Math.round(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export function fmtTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function gainClass(n) {
  const v = Number(n);
  if (!isFinite(v) || v === 0) return "flat";
  return v > 0 ? "up" : "down";
}
