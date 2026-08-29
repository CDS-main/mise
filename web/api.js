/* Mise API shim.
 *
 * The prototype kept everything in localStorage behind load()/save(). That was
 * deliberate — it means going multi-device touches exactly those two functions
 * and nothing else. This file is what they call now.
 *
 * Design notes worth keeping:
 *  - The app stays SYNCHRONOUS against an in-memory cache. Only boot is async.
 *    Making every render await the network would have meant rewriting the whole
 *    UI for no user-visible benefit.
 *  - Whole-state writes are debounced and carry a `rev`. A 409 means another
 *    device wrote first: we refetch, replay our local settings, and retry once.
 *  - Cook logs and pantry decrements do NOT go through the whole-state write.
 *    They have their own atomic endpoints so a race can never eat a logged cook.
 *  - Everything degrades to localStorage if the server is unreachable, so the
 *    app still works on a laptop with the Pi switched off.
 */
(function (global) {
  "use strict";

  const BASE = (global.MISE_API_BASE || "").replace(/\/$/, "");
  const LS_MIRROR = "mise.v3";
  const SAVE_DEBOUNCE = 700;

  let cache = null;          // the object the app mutates directly
  let rev = 0;
  let online = false;
  let timer = null;
  let inflight = false;
  const listeners = [];

  const url = (p) => BASE + p;
  const notify = (ev, d) => listeners.forEach((f) => { try { f(ev, d); } catch (_) {} });

  async function j(method, path, body) {
    const r = await fetch(url(path), {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      const e = new Error("HTTP " + r.status);
      e.status = r.status;
      try { e.body = await r.json(); } catch (_) {}
      throw e;
    }
    return r.status === 204 ? null : r.json();
  }

  function mirror() {
    try { localStorage.setItem(LS_MIRROR, JSON.stringify(cache)); } catch (_) {}
  }
  function fromMirror() {
    try { const r = localStorage.getItem(LS_MIRROR); return r ? JSON.parse(r) : null; }
    catch (_) { return null; }
  }

  /* ── boot ────────────────────────────────────────────────────────────── */
  async function boot(defaults) {
    try {
      const st = await j("GET", "/api/state");
      online = true;
      rev = st.rev || 0;
      const empty = !st.pantry || Object.keys(st.pantry).length === 0;
      if (empty && defaults) {
        // first run against a fresh database — push the seed up
        await j("POST", "/api/seed", defaults);
        const again = await j("GET", "/api/state");
        rev = again.rev || 0;
        cache = merge(defaults, again);
      } else {
        cache = merge(defaults || {}, st);
      }
      notify("online", { rev });
    } catch (err) {
      online = false;
      cache = fromMirror() || defaults || null;
      notify("offline", { error: String(err) });
    }
    mirror();
    return cache;
  }

  function merge(defaults, server) {
    const out = Object.assign({}, defaults, server);
    out.pantry = server.pantry && Object.keys(server.pantry).length
      ? server.pantry : (defaults.pantry || {});
    out.log = server.log || [];
    out.custom = server.custom || [];
    delete out.rev;
    return out;
  }

  /* ── whole-state write, debounced + optimistic concurrency ───────────── */
  function queueSave(state) {
    cache = state;
    mirror();
    if (!online) return;
    clearTimeout(timer);
    timer = setTimeout(flush, SAVE_DEBOUNCE);
  }

  async function flush(retry) {
    if (inflight) { clearTimeout(timer); timer = setTimeout(flush, SAVE_DEBOUNCE); return; }
    inflight = true;
    const settingKeys = ["reels", "pins", "themeOther", "effort", "medium", "mode",
      "intakeMode", "basket", "intake", "appliances", "kit"];
    const settings = {};
    settingKeys.forEach((k) => { if (cache[k] !== undefined) settings[k] = cache[k]; });
    try {
      const res = await j("PUT", "/api/state", {
        rev,
        settings,
        pantry: Object.values(cache.pantry || {}),
        custom: cache.custom || [],
      });
      rev = res.rev;
      notify("saved", { rev });
    } catch (err) {
      if (err.status === 409 && !retry) {
        // someone else wrote first. take their world, re-apply our settings, retry once.
        const st = await j("GET", "/api/state");
        rev = st.rev;
        cache.pantry = st.pantry;
        cache.log = st.log;
        cache.custom = st.custom;
        notify("merged", { rev });
        inflight = false;
        return flush(true);
      }
      online = err.status ? online : false;
      notify("saveFailed", { error: String(err) });
    } finally {
      inflight = false;
    }
  }

  /* ── atomic operations ───────────────────────────────────────────────── */
  async function logCook(cook) {
    if (!online) return { ok: false, offline: true };
    try {
      const res = await j("POST", "/api/cooks", cook);
      rev = res.rev;
      notify("cookLogged", res);
      return res;
    } catch (err) { notify("saveFailed", { error: String(err) }); return { ok: false }; }
  }

  async function adjustQty(id, delta, extra) {
    if (!online) return null;
    try {
      const item = await j("PATCH", "/api/pantry/" + encodeURIComponent(id),
        Object.assign({ delta }, extra || {}));
      if (cache && cache.pantry && cache.pantry[id]) cache.pantry[id] = item;
      return item;
    } catch (_) { return null; }
  }

  /* ── assistant ───────────────────────────────────────────────────────── */
  const importRecipe = (body) => j("POST", "/api/import", body);
  const adapt = (body) => j("POST", "/api/assist/adapt", body);
  const assistHealth = () => j("GET", "/api/assist/health");
  const saveRecipe = (r) => j("POST", "/api/recipes", r);
  const lookupBarcode = (code) => j("GET", "/api/barcode/" + encodeURIComponent(code));

  /* ── live scale ──────────────────────────────────────────────────────── */
  function connectScale(onSample) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const host = BASE ? BASE.replace(/^https?:\/\//, "") : location.host;
    let ws, dead = false;
    const open = () => {
      try { ws = new WebSocket(`${proto}://${host}/ws/scale`); } catch (_) { return; }
      ws.onmessage = (e) => { try { onSample(JSON.parse(e.data)); } catch (_) {} };
      ws.onclose = () => { if (!dead) setTimeout(open, 1500); };
      ws.onerror = () => { try { ws.close(); } catch (_) {} };
    };
    open();
    return {
      tare: () => ws && ws.readyState === 1 && ws.send(JSON.stringify({ op: "tare" })),
      track: (ing) => ws && ws.readyState === 1 && ws.send(JSON.stringify({ op: "track", ingredient: ing })),
      sim: (g) => ws && ws.readyState === 1 && ws.send(JSON.stringify({ op: "sim", grams: g })),
      close: () => { dead = true; try { ws.close(); } catch (_) {} },
    };
  }

  global.MiseAPI = {
    boot, cachedState: () => cache, queueSave, flush: () => flush(false),
    logCook, adjustQty, importRecipe, adapt, assistHealth, saveRecipe,
    lookupBarcode, connectScale,
    isOnline: () => online, rev: () => rev,
    on: (f) => listeners.push(f),
  };
})(window);
