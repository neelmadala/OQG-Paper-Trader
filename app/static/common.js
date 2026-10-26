// Shared helpers for both panels. All server-provided text goes through esc() or textContent.
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
const usd = (n) => (n == null ? "-" : n.toLocaleString(undefined, { style: "currency", currency: "USD" }));
const pct = (n) => (n == null ? "-" : (n > 0 ? "+" : "") + n.toFixed(2) + "%");
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString();

async function api(path, headers, options = {}) {
  const res = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...headers } });
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) {
    const d = body && body.detail;
    const err = new Error(typeof d === "string" ? d : d ? JSON.stringify(d) : "HTTP " + res.status);
    err.status = res.status;
    throw err;
  }
  return body;
}

function safeGet(store, key) { try { return store.getItem(key) || ""; } catch (_) { return ""; } }
function safeSet(store, key, val) { try { store.setItem(key, val); } catch (_) {} }
function safeDel(store, key) { try { store.removeItem(key); } catch (_) {} }

const PALETTE = ["#2557d6", "#e07b00", "#0f8a4f", "#c62f2f", "#8a3ffc", "#007d79", "#b28600", "#d12771", "#4d5358", "#1192e8"];

// ---------- column glossary + tooltips ----------
// Mark any element with data-g="key" and it gets a hover/focus/tap tooltip from this table.
const GLOSSARY = {
  rank: "Rank by total return, best first.",
  portfolio: "A portfolio is a separate account with its own cash, positions, orders and equity curve. You always have a Main one; you can make extras (for example a practice account), and each competition you join gives you a fresh one.",
  join_code: "An admin gives you a short code for each competition. Enter it to join: you get a new portfolio with that competition's starting cash and rules. It doesn't touch your other portfolios.",
  comp_status: "upcoming = trading hasn't opened yet, active = trading is open, ended = trading is closed and the result is frozen at the final value.",
  trader: "Account username.",
  joined: "When this account was really created. On the chart, a late joiner is drawn as a dashed line at 0% back to the oldest account's start, so everyone shares one timeline.",
  equity: "Cash plus the market value of your longs, minus what it would cost to buy back your shorts. This is your account value.",
  return: "Total return since the account started: (equity / starting cash - 1). Not annualized.",
  pnl: "Profit and loss in dollars: equity minus starting cash. Includes open positions and trading costs.",
  cash: "Cash balance. Includes the proceeds of short sales, which stay locked as collateral (see Free cash). Slippage and commissions come out of it.",
  free_cash: "Cash you can spend on new buys: cash minus the collateral your shorts lock up. Opening a short needs part of this as margin. Negative means your shorts are under-collateralized.",
  max_dd: "Largest peak-to-trough drop in equity, as a % of the peak. Measured from the saved equity points (taken after every trade and once a minute during market hours), so it can miss dips between points.",
  trades: "Number of filled orders, buys and sells both.",
  win_rate: "Share of your closing orders (sells and covers) that made a profit. Shows - until you have closed something.",
  positions: "Open holdings: symbol and number of shares. A negative number means a short position.",
  symbol: "Ticker symbol.",
  qty: "Number of shares. Fractions are allowed. A negative number means you are short. For options it is the number of contracts, 100 shares each.",
  avg_cost: "Average entry price of what you still hold, weighted by quantity: what you paid for longs, what you sold at for shorts. Does not include fees.",
  price_pos: "Latest quote used to value the position. Quotes are shared and cached for about 15 seconds. Marked (stale) if the data provider could not be reached.",
  value: "Market value of the position: quantity times latest price. Negative for shorts, because it is what you owe.",
  unrealized: "Paper profit or loss on what you still hold. Longs gain when the price rises above your average cost, shorts gain when it falls below your average entry price. Becomes realized when you close.",
  unrealized_pct: "Unrealized P&L as a percentage of the position's cost (or, for a short, of the money it was sold for).",
  time: "When the order was filled, in your local time.",
  side: "Buy or Sell trade longs. Short sells borrowed shares to open a short, Cover buys them back to close it.",
  price_fill: "Price the order filled at, after slippage. Buys and covers fill slightly above the quote, sells and shorts slightly below.",
  stop: "Optional stop price per share. If a long falls to it, or a short rises to it, the app closes the whole position at its next one-minute check during market hours. It fills at the live price (with slippage and fees), not at your stop, so a fast move can fill worse.",
  limit_order: "A limit order only fills at your price or better: a buy at or below it, a sell at or above it. If the market is already there it fills now (with the usual slippage, never worse than your limit). Otherwise it waits and is checked about once a minute during market hours, up to 30 days. Cash and shares are not set aside while it waits, so it is rejected if you no longer have them when it fills.",
  limit_price: "Your limit price per share. A buy or cover fills at this price or lower, a sell or short at this price or higher.",
  limit_status: "open = still waiting, filled = traded, cancelled = you cancelled it, expired = it waited too long, rejected = the price was reached but you no longer had the cash, shares or margin (hover for the reason).",
  limit_fill: "Filled from a limit order. Resting limit orders fill at the live price when it reaches your limit, with no slippage.",
  ex_date: "The first day the stock trades without the dividend or split. You get it if you held the shares before this day.",
  event: "Dividend: cash paid per share you held (owed by you if you were short). Split: your share count and cost per share are adjusted so your position's value doesn't change.",
  event_detail: "What happened to your account: cash paid or owed for a dividend, shares added for a split.",
  chain: "All the calls and puts for one stock and expiry date. A call gains when the stock goes up, a put when it goes down. One contract covers 100 shares, so a $3.00 option costs $300. You can buy options and sell ones you own.",
  opt_call: "A call gains when the stock goes up. Buying one is a bet on a rise.",
  opt_put: "A put gains when the stock goes down. Buying one is a bet on a fall (or protection for shares you own).",
  opt_be: "The stock price at expiry where buying this contract at the ask comes out even: strike plus what you pay for a call, strike minus it for a put. The % is how far the stock must move from where it is now to get there. Above it a call profits, below it a put profits.",
  opt_money: "How far the strike is from the stock price. ITM (in the money) means the option already has value if it expired now. OTM (out of the money) means it doesn't yet and needs the stock to move.",
  opt_strike: "The price the stock has to pass for the option to be worth anything at expiry. A call is in the money when the stock is above the strike, a put when it is below.",
  opt_bid: "What buyers are offering. This is what you get when you sell. Click it to sell a contract you own.",
  opt_ask: "What sellers want. This is what you pay when you buy. Click it to buy a contract.",
  opt_iv: "Implied volatility: how big a move the market expects, as a yearly percentage. Higher means pricier options.",
  expiry_fill: "This option expired. If it finished in the money you were paid its value in cash (stock price minus strike for a call, strike minus stock price for a put), otherwise it ended worthless. No shares change hands.",
  stop_fill: "Closed automatically because the price hit your stop. It fills at the live price at the time, not at the stop price itself.",
  forced: "Liquidation: the app covered part of this short for you because your cash fell below the maintenance level. It fills like any cover, with slippage and fees.",
  realized: "Profit or loss locked in by a closing order (sell or cover), minus the order fee. Orders that open a position show -.",
};

const tipEl = document.createElement("div");
tipEl.className = "tooltip"; tipEl.setAttribute("role", "tooltip");
document.addEventListener("DOMContentLoaded", () => document.body.appendChild(tipEl));

function showTip(el) {
  tipEl.textContent = el.dataset.tip;
  tipEl.style.left = "0px"; tipEl.style.top = "0px";
  tipEl.classList.add("show");
  const r = el.getBoundingClientRect(), w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8));
  const below = r.bottom + 8 + h <= window.innerHeight;
  tipEl.style.left = left + "px";
  tipEl.style.top = (below ? r.bottom + 8 : r.top - 8 - h) + "px";
}
function hideTip() { tipEl.classList.remove("show"); }
document.addEventListener("mouseover", (e) => { const t = e.target.closest && e.target.closest("[data-tip]"); t ? showTip(t) : hideTip(); });
document.addEventListener("focusin", (e) => { const t = e.target.closest && e.target.closest("[data-tip]"); t ? showTip(t) : hideTip(); });
document.addEventListener("focusout", hideTip);
document.addEventListener("scroll", hideTip, true);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });

// Attach glossary text to every [data-g] element under root. Safe to call repeatedly.
function applyGlossary(root = document) {
  root.querySelectorAll("[data-g]").forEach((el) => {
    const text = GLOSSARY[el.dataset.g];
    if (!text) return;
    el.dataset.tip = text; el.tabIndex = 0; el.classList.add("tip");
  });
}
document.addEventListener("DOMContentLoaded", () => applyGlossary());

// ---------- chart time range ----------
// Renders preset buttons plus a custom From/To date picker into `el`.
// range.query() gives "?since=..&until=.." for the API; onChange fires when the user picks a range.
function makeRange(el, storeKey, onChange) {
  const PRESETS = [["1D", 1], ["1W", 7], ["1M", 30], ["3M", 90], ["All", null]];
  let sel = safeGet(localStorage, storeKey);
  if (!PRESETS.some(([l]) => l === sel)) sel = "All";
  el.classList.add("range");
  el.innerHTML = PRESETS.map(([l]) => `<button type="button" class="chip" data-r="${l}">${l}</button>`).join("") +
    '<label class="muted">From <input type="date" data-from></label><label class="muted">to <input type="date" data-to></label>';
  const from = el.querySelector("[data-from]"), to = el.querySelector("[data-to]");

  function paint() {
    el.querySelectorAll(".chip").forEach((b) => b.classList.toggle("active", b.dataset.r === sel));
  }
  function bounds() {
    if (sel === "custom") {
      let a = from.value ? new Date(from.value + "T00:00:00").getTime() / 1000 : null;
      let b = to.value ? new Date(to.value + "T23:59:59").getTime() / 1000 : null;
      if (a != null && b != null && b < a) [a, b] = [new Date(to.value + "T00:00:00").getTime() / 1000, new Date(from.value + "T23:59:59").getTime() / 1000];
      return [a, b];
    }
    const days = PRESETS.find(([l]) => l === sel)[1];
    return [days == null ? null : Date.now() / 1000 - days * 86400, null];
  }
  el.addEventListener("click", (e) => {
    const b = e.target.closest(".chip"); if (!b) return;
    sel = b.dataset.r; from.value = ""; to.value = "";
    safeSet(localStorage, storeKey, sel); paint(); onChange();
  });
  [from, to].forEach((inp) => inp.addEventListener("change", () => {
    if (!from.value && !to.value) { sel = "All"; } else { sel = "custom"; }
    paint(); onChange();
  }));
  paint();
  return {
    query() {
      const [a, b] = bounds(), q = [];
      if (a != null) q.push("since=" + a.toFixed(0));
      if (b != null) q.push("until=" + b.toFixed(0));
      return q.length ? "?" + q.join("&") : "";
    },
  };
}

// x-axis tick label that shows the time of day only when the visible span is short
function timeTick(v) {
  const span = this.max - this.min, d = new Date(v);
  return span <= 2 * 86400000
    ? d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// ---------- shared helpers for the trader pages ----------

const OPT_RE = /^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
// SPY261218C00450000 -> "SPY Dec 18 '26 $450 Call"; anything else is returned as is
function symLabel(sym) {
  const m = OPT_RE.exec(sym || "");
  if (!m) return sym;
  const strike = parseInt(m[6], 10) / 1000;
  return `${m[1]} ${MONTHS[parseInt(m[3], 10) - 1] || "?"} ${parseInt(m[4], 10)} '${m[2]} $${+strike.toFixed(3)} ${m[5] === "C" ? "Call" : "Put"}`;
}
const symCell = (sym) => `<span title="${esc(sym)}">${esc(symLabel(sym))}</span>`;
const byId = (id) => document.getElementById(id);

function orderTag(r) {
  if (r.reason === "stop") return ' <span class="short-tag" data-g="stop_fill">Stop</span>';
  if (r.reason === "liquidation") return ' <span class="short-tag" data-g="forced">Forced</span>';
  if (r.reason === "expiry") return ' <span class="short-tag" data-g="expiry_fill">Expired</span>';
  if (r.reason === "limit") return ' <span class="short-tag" data-g="limit_fill">Limit</span>';
  return "";
}

function eventText(r) {
  if (r.kind === "dividend") {
    const verb = r.amount >= 0 ? "Dividend" : "Dividend owed (short)";
    return [verb, `${usd(Math.abs(r.amount)).replace(/^/, r.amount >= 0 ? "+" : "-")} (${Math.abs(r.qty)} sh x ${usd(r.value)})`];
  }
  const ratio = r.value >= 1 ? `${+r.value.toFixed(4)}-for-1 split` : `1-for-${+(1 / r.value).toFixed(4)} reverse split`;
  return [ratio, `${r.amount >= 0 ? "+" : ""}${+r.amount.toFixed(4)} shares; your cost per share was adjusted to match`];
}

function compWhen(c) {
  if (c.status === "upcoming") return `opens ${fmtTime(c.start_ts)}`;
  if (c.status === "ended") return `ended ${fmtTime(c.end_ts)}`;
  return c.end_ts ? `open until ${fmtTime(c.end_ts)}` : "open, no end date";
}
function compRules(c) {
  const r = [];
  if (!c.allow_options) r.push("no options");
  if (!c.allow_shorting) r.push("no short selling");
  const t = r.length ? r.join(", ") : "stocks, ETFs, options and shorts allowed";
  return t.charAt(0).toUpperCase() + t.slice(1);
}

// The parts every trader page shares: API key sign-in, the portfolio switcher, the competition banner,
// the account stats, and a once-a-minute refresh. A page calls Shell.start({ onData(ctx), onTick, onSwitch }).
// ctx = { p: the viewed portfolio, pfs: all your portfolios, comps: your competitions }.
const Shell = {
  key: safeGet(localStorage, "pt_api_key"),
  pf: safeGet(localStorage, "pt_portfolio"),   // id of the portfolio being viewed; empty = Main
  ctx: null, timer: null, opts: {},
  headers() { return { "X-API-Key": this.key, ...(this.pf ? { "X-Portfolio": this.pf } : {}) }; },
  setPortfolio(id) {
    this.pf = id ? String(id) : "";
    if (this.pf) safeSet(localStorage, "pt_portfolio", this.pf); else safeDel(localStorage, "pt_portfolio");
  },
  status(text, isErr) {
    const el = byId("status"); el.hidden = !text; el.textContent = text || ""; el.className = "msg" + (isErr ? " err" : "");
  },
  signed(on) {
    byId("app").hidden = !on; byId("logout").hidden = !on; byId("connect").hidden = on; byId("key").hidden = on;
    const su = byId("signup"); if (su) su.hidden = on;
    const side = byId("side"); if (side) side.hidden = !on;
    const hu = byId("hdrUser"); if (hu && !on) hu.hidden = true;
    this.adminLink(false);
  },
  // "Go to Admin Menu" shows for admins, and for anyone who already signed in to the admin pages in this tab.
  adminLink(isAdmin) {
    const a = byId("toAdmin"); if (a) a.hidden = !(isAdmin || safeGet(sessionStorage, "pt_admin_token"));
  },
  renderSide({ p, pfs }) {
    const list = byId("sideList"); if (!list) return;
    list.innerHTML = "";
    for (const x of pfs) {
      const sub = x.is_default ? "Main portfolio" : x.competition_id ? "Competition" : "Practice";
      const label = x.is_default ? "Main" : x.portfolio;
      list.insertAdjacentHTML("beforeend",
        `<button type="button" class="side-item${x.portfolio_id === p.portfolio_id ? " current" : ""}" data-pf="${esc(x.portfolio_id)}">` +
        `<span class="side-name">${esc(label)}</span><span class="side-ret ${cls(x.return_pct)}">${esc(pct(x.return_pct))}</span>` +
        `<span class="side-sub">${esc(sub)}${x.frozen ? " (closed)" : ""}</span><span class="side-eq">${esc(usd(x.equity))}</span></button>`);
    }
  },
  async refresh() {
    if (!this.key) return;
    try {
      let p;
      try { p = await api("/api/portfolio", this.headers()); }
      catch (e) {                               // the saved portfolio is gone (deleted, or removed from a competition)
        if (e.status === 404 && this.pf) { this.setPortfolio(""); p = await api("/api/portfolio", this.headers()); } else throw e;
      }
      const [pfs, comps] = await Promise.all([api("/api/portfolios", this.headers()), api("/api/competitions", this.headers())]);
      this.status(""); this.signed(true);
      this.ctx = { p, pfs, comps };
      this.paint(this.ctx);
      if (this.opts.onData) await this.opts.onData(this.ctx);
    } catch (e) {
      if (/API key/.test(e.message)) this.disconnect();
      this.status(e.message, true);
    }
  },
  paint({ p, pfs }) {
    const set = (id, text, klass) => { const el = byId(id); if (el) { el.textContent = text; if (klass != null) el.className = klass; } };
    const hu = byId("hdrUser"); if (hu) { hu.textContent = p.username; hu.hidden = false; }
    this.adminLink(!!p.is_admin);
    this.renderSide({ p, pfs });
    set("who", p.is_default ? p.username : `${p.username} - ${p.portfolio}`);
    set("equity", usd(p.equity));
    set("ret", pct(p.return_pct), "value " + cls(p.return_pct));
    set("pnl", usd(p.pnl), "value " + cls(p.pnl));
    set("cash", usd(p.cash));
    set("freeCash", usd(p.free_cash), "value " + (p.free_cash < 0 ? "down" : ""));
    set("dd", p.max_drawdown_pct.toFixed(2) + "%");
    const mw = byId("marginWarn"); if (mw) mw.hidden = !p.margin_call;
    set("liqNote", p.auto_liquidate ? " Auto-liquidation is on: the app will cover shorts for you at the next one-minute check during market hours." : "");
    const sel = byId("pfSel");
    if (sel) {
      sel.innerHTML = "";
      for (const x of pfs) {
        const label = x.is_default ? "Main" : x.competition_id ? `${x.portfolio} (competition)` : x.portfolio;
        sel.insertAdjacentHTML("beforeend", `<option value="${esc(x.portfolio_id)}">${esc(label)}</option>`);
      }
      sel.value = String(p.portfolio_id);
      sel.hidden = pfs.length < 2;
    }
    const b = byId("compBanner"), c = p.competition;
    if (b) {
      b.hidden = !c;
      if (c) {
        b.className = "comp-banner " + c.status;
        b.innerHTML = `<strong>${esc(c.name)}</strong> - ${esc(compWhen(c))}. ${esc(compRules(c))}.` +
          (p.frozen ? " Trading is closed and your result is frozen." : c.status === "upcoming" ? " You can't trade yet." : "");
      }
    }
  },
  connect() {
    const k = byId("key").value.trim() || this.key;
    if (!k) return this.status("Enter your API key first.", true);
    this.key = k; safeSet(localStorage, "pt_api_key", k); byId("key").value = "";
    this.refresh();
    clearInterval(this.timer);
    this.timer = setInterval(() => { this.refresh(); if (this.opts.onTick) this.opts.onTick(); }, 60000);
  },
  disconnect() {
    safeDel(localStorage, "pt_api_key"); safeDel(localStorage, "pt_portfolio"); this.key = ""; this.pf = ""; this.ctx = null;
    clearInterval(this.timer); this.signed(false);
    if (this.opts.onDisconnect) this.opts.onDisconnect();
  },
  start(opts) {
    this.opts = opts || {};
    byId("connect").onclick = () => this.connect();
    byId("key").addEventListener("keydown", (e) => { if (e.key === "Enter") this.connect(); });
    byId("logout").onclick = () => { this.disconnect(); this.status(""); };
    const sel = byId("pfSel");
    if (sel) sel.addEventListener("change", () => { this.setPortfolio(sel.value); if (this.opts.onSwitch) this.opts.onSwitch(); this.refresh(); });
    const list = byId("sideList");
    if (list) list.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-pf]"); if (!b) return;
      this.setPortfolio(b.dataset.pf); if (this.opts.onSwitch) this.opts.onSwitch(); this.refresh();
    });
    const side = byId("side"), tg = byId("sideToggle");
    if (side && tg) {
      const apply = (collapsed) => {
        side.classList.toggle("collapsed", collapsed);
        tg.innerHTML = collapsed ? "&raquo;" : "&laquo;";
        tg.title = tg.ariaLabel = collapsed ? "Show portfolios" : "Collapse sidebar";
      };
      const stored = safeGet(localStorage, "pt_sidebar");
      apply(stored ? stored === "closed" : window.matchMedia("(max-width: 800px)").matches);
      tg.onclick = () => { const c = !side.classList.contains("collapsed"); apply(c); safeSet(localStorage, "pt_sidebar", c ? "closed" : "open"); };
    }
    if (this.key) this.connect();
  },
};

// The same idea for the admin pages: token sign-in and a once-a-minute refresh.
const AdminShell = {
  token: safeGet(sessionStorage, "pt_admin_token"), timer: null, opts: {}, role: "", name: "",
  // The owner signs in with the admin token; an admin signs in with their own API key (pt_...).
  headers() { return this.token.startsWith("pt_") ? { "X-API-Key": this.token } : { "X-Admin-Token": this.token }; },
  status(text, isErr) { const el = byId("status"); el.hidden = !text; el.textContent = text || ""; el.className = "msg" + (isErr ? " err" : ""); },
  signed(on) { byId("app").hidden = !on; byId("logout").hidden = !on; byId("connect").hidden = on; byId("token").hidden = on; },
  async refresh() {
    if (!this.token) return;
    try {
      const me = await api("/api/admin/me", this.headers());
      this.role = me.role; this.name = me.name;
      const w = byId("adminWho"); if (w) w.textContent = me.role === "owner" ? "Signed in as owner" : `Signed in as ${me.name} (admin)`;
      await this.opts.onData(); this.status(""); this.signed(true);
    }
    catch (e) { if (e.status === 401 || /not an admin|disabled/.test(e.message)) this.disconnect(); this.status(e.message, true); }
  },
  connect() {
    const t = byId("token").value.trim() || this.token;
    if (!t) return this.status("Enter the admin token or your admin API key first.", true);
    this.token = t; safeSet(sessionStorage, "pt_admin_token", t); byId("token").value = "";
    this.refresh(); clearInterval(this.timer); this.timer = setInterval(() => this.refresh(), 60000);
  },
  disconnect() { safeDel(sessionStorage, "pt_admin_token"); this.token = ""; this.role = ""; clearInterval(this.timer); this.signed(false); },
  start(opts) {
    this.opts = opts;
    byId("connect").onclick = () => this.connect();
    byId("token").addEventListener("keydown", (e) => { if (e.key === "Enter") this.connect(); });
    byId("logout").onclick = () => { this.disconnect(); this.status(""); };
    if (this.token) this.connect();
  },
};

// Copy text to the clipboard (works on plain http too). Returns true if it worked.
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) {}
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch (_) {}
  ta.remove(); return ok;
}
