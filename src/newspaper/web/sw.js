// Alba's service worker: the daily notice (#17) and, on the tailnet, reading without the PC
// (#20a): a saved copy of the latest edition and Guardado, and a queue of what happened while
// the PC slept (openings, pings, sources opened). The server fills the name, the icon and the
// badge (data: URLs), so showing a notice never waits for the PC.
const NAME = __NAME__, ICON = __ICON__, BADGE = __BADGE__;
// Only the phone's origin keeps copies: on 127.0.0.1 the PC is the server, and a slow page must
// never say it needs the PC.
const TAILNET = self.location.hostname.endsWith(".ts.net");
const NAV_MS = 4000;  // a navigation or a probe; then the copy
const SEND_MS = 15000;  // a queued POST or a saved resource, body included
const ASLEEP_MS = 60000;  // after a timeout, copies at once for a minute
const SYNC_EVERY_MS = 5 * 60000;  // a live visit refreshes the copy at most this often
let asleepUntil = 0;
let lastSync = 0;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil((async () => {
  // The preload is the browser's own navigation, so the server still counts the opening;
  // fetch(event.request) would lose Sec-Fetch-Dest (w3c/ServiceWorker#1803).
  if (TAILNET && self.registration.navigationPreload) {
    await self.registration.navigationPreload.enable();
  }
  await clients.claim();
})()));

// --- Storage: the current copy ("estado") and the queue ("cola"), in IndexedDB.
function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("alba", 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore("estado");
      req.result.createObjectStore("cola", { keyPath: "seq", autoIncrement: true });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function idb(store, mode, op) {
  const conn = await openDb();
  return new Promise((resolve, reject) => {
    const t = conn.transaction(store, mode);
    const request = op(t.objectStore(store));
    t.oncomplete = () => { conn.close(); resolve(request.result); };
    t.onerror = t.onabort = () => { conn.close(); reject(t.error); };
  });
}
const getState = () => idb("estado", "readonly", (s) => s.get("actual"));
const putState = (state) => idb("estado", "readwrite", (s) => s.put(state, "actual"));

// --- Network helpers.
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// Its value, or null after ms or on an error.
const within = (ms, promise) => Promise.race([promise.catch(() => null), sleep(ms).then(() => null)]);
async function timed(url, init, ms) {  // {res, body}: the deadline covers the body too
  const res = await fetch(url, { ...init, signal: AbortSignal.timeout(ms) });
  return { res, body: await res.arrayBuffer() };
}
async function getJSON(url, ms = SEND_MS) {
  try {
    const { res, body } = await timed(url, {}, ms);
    return res.ok ? JSON.parse(new TextDecoder().decode(body)) : null;
  } catch {
    return null;
  }
}
function later(event, promise) {  // waitUntil, if the event still accepts it
  try { event.waitUntil(promise); } catch {}
}

// --- The queue: only events whose order doesn't matter (the earliest opening and ping win; a
// source opened is always read). Marks made without the PC are #20b.
async function encolar(url) {
  await idb("cola", "readwrite", (s) => s.add({ url }));
}
async function enviar() {
  if (!TAILNET) return;
  await navigator.locks.request("alba-cola", async () => {
    for (const { seq, url } of await idb("cola", "readonly", (s) => s.getAll())) {
      let status = 0;
      try {
        // JSON: a source's read answers 200, not a 303 back to the referrer (this script).
        const init = { method: "POST", headers: { Accept: "application/json" } };
        status = (await timed(url, init, SEND_MS)).res.status;
      } catch {}
      // ponytail: no answer stops the round (the spec tries every entry): the PC sleeps, and
      // each of the rest would wait its 15 s for nothing.
      if (status === 0) return;
      const done = status < 300 || (status >= 400 && status < 500 && status !== 408 && status !== 429);
      if (!done) continue;  // busy (503) or broken: next time, never dropped
      if (status >= 400) console.warn("Newspaper: el PC rechazó", url, status);
      await idb("cola", "readwrite", (s) => s.delete(seq));
    }
  });
}

// The notice's pings: queued with their time, then sent; the desktop just tries. A notice sent
// without a number (n null) pings without one, and the server counts those.
async function ping(data, event) {
  if (data.kind !== "edicion") return;
  const params = new URLSearchParams({ s: data.sid, at: new Date().toISOString() });
  if (data.n) params.set("n", data.n);
  const url = `/aviso/${data.day}/${event}?${params}`;
  if (!TAILNET) return fetch(url, { method: "POST" }).catch(() => {});
  await encolar(url);
  await enviar();
}

// --- The copy: all or nothing, one at a time, never an older day.
async function guardar(aviso) {
  if (!TAILNET) return;
  await navigator.locks.request("alba-guardar", async () => {
    let manifest = null;
    for (const wait of [0, 10000, 30000]) {  // right after the 07:30 wake the tailnet may lag
      if (wait) await sleep(wait);
      if ((manifest = await getJSON("/sin-pc"))) break;
    }
    if (!manifest) return;
    lastSync = Date.now();
    const state = await getState();
    for (let round = 0; round < 2 && manifest; round++) {
      if (state && manifest.day < state.day) return;  // a --force under way: never back a day
      if (state && state.edition === manifest.edition) await putState({ ...state, base: manifest.base });
      // Checked every round: a change undone mid-download (A → B → A) brings back the copy we
      // have, whose cache the next lines would otherwise delete.
      if (state && state.revision === manifest.revision && (await caches.has(state.cache))) {
        if (aviso && aviso.day === state.day) await ping(aviso, "guardada");
        return;
      }
      const name = "alba-" + manifest.revision;
      await caches.delete(name);
      const cache = await caches.open(name);
      if (!(await download(cache, [...manifest.pages, ...manifest.static]))) {
        await caches.delete(name);
        return;
      }
      const again = await getJSON("/sin-pc");
      if (!again || again.revision !== manifest.revision) {  // it changed meanwhile: once more
        await caches.delete(name);
        manifest = again;
        continue;
      }
      const { revision, edition, day, originals, base } = manifest;
      await putState({ revision, edition, day, originals, base, cache: name });
      for (const key of await caches.keys()) {
        if (key.startsWith("alba-") && key !== name) await caches.delete(key);
      }
      if (aviso && aviso.day === day) await ping(aviso, "guardada");
      return;
    }
  });
}
async function download(cache, urls) {  // 4 at a time, one retry each
  let next = 0, ok = true;
  const worker = async () => {
    while (ok && next < urls.length) {
      if (!(await save(cache, urls[next++]))) ok = false;
    }
  };
  await Promise.all([worker(), worker(), worker(), worker()]);
  return ok;
}
async function save(cache, url) {
  const page = !url.startsWith("/static/") && url !== "/manifest.webmanifest";
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      // X-Sin-PC: the server draws the copy without the day's passing banner.
      const { res, body } = await timed(url, { headers: { "X-Sin-PC": "1" } }, SEND_MS);
      const html = (res.headers.get("content-type") || "").startsWith("text/html");
      // .woff2 comes as application/octet-stream on this PC: only pages are checked for type.
      if (res.status === 200 && !res.redirected && (html || !page)) {
        await cache.put(url, new Response(body, { status: 200, headers: res.headers }));
        return true;
      }
    } catch {}
  }
  return false;
}

// --- Requests: navigations live first and the copy when the PC doesn't answer; statics from
// the copy first. Everything else goes to the network as if there were no worker.
self.addEventListener("fetch", (event) => {
  if (!TAILNET) return;
  const { request } = event;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || request.method !== "GET") return;
  if (request.mode === "navigate") event.respondWith(navigate(event, url));
  else if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(asset(request));
  }
});

async function navigate(event, url) {
  const asleep = Date.now() < asleepUntil;
  const live = await within(asleep ? 0 : NAV_MS, network(event, url));
  if (live && live.status < 500) {  // redirects and 404s too, as they are
    asleepUntil = 0;
    if (live.ok) later(event, afterVisit());
    return live;
  }
  // No answer: the PC sleeps. Only a wait that ran out starts the minute; a copy served at once
  // must not stretch it, or the phone would keep reading the copy with the PC back.
  if (!live && !asleep) asleepUntil = Date.now() + ASLEEP_MS;
  return fromCopy(event, url);  // a 5xx too: tailscale serve with the web down, a locked db
}
async function network(event, url) {
  let res = await event.preloadResponse;
  if (!res) {  // no preload yet (the first activation): this one won't count as an opening
    res = await fetch(event.request);
    // ponytail: "/" is left out, since abrir() would credit it to the copy's day, maybe
    // yesterday's: a rare first navigation around the activation loses Today's opening.
    if (res.ok && url.pathname !== "/") later(event, abrir(url).then(enviar));
  }
  const html = (res.headers.get("content-type") || "").startsWith("text/html");
  if (res.type === "opaqueredirect" || !html) return res;
  const body = await res.text();  // inside the deadline: a stalled body falls back to the copy
  return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
}
async function afterVisit() {
  await enviar();
  if (Date.now() - lastSync > SYNC_EVERY_MS) await guardar(null);
}
async function fromCopy(event, url) {
  const state = await getState();
  if (url.pathname.startsWith("/a/")) return original(url, state);
  const key = url.pathname === "/" && state ? `/e/${state.day}` : url.pathname + url.search;
  const saved = state && (await caches.match(key, { cacheName: state.cache }));
  if (!saved) return minimal();
  // ponytail: queued without enviar() (the spec sends in the same waitUntil): the PC just
  // didn't answer, and the next live visit or return to the app sends it.
  later(event, abrir(url));
  const marks = JSON.stringify(state.base || {}).replace(/&/g, "&amp;").replace(/'/g, "&#39;");
  const html = (await saved.text()).replace(
    "<body",
    `<body data-sin-pc data-copia="${state.day}" data-marcas='${marks}'`,
  );
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}
async function abrir(url) {  // the opening, with its real time, for the funnel
  const at = encodeURIComponent(new Date().toISOString());
  const note = url.pathname.match(/^\/n\/(\d+)$/);
  const day = url.pathname === "/"
    ? (await getState())?.day
    : url.pathname.match(/^\/e\/(\d{4}-\d{2}-\d{2})$/)?.[1];
  if (note) await encolar(`/n/${note[1]}/abierta?at=${at}`);
  else if (day) await encolar(`/e/${day}/abierta?at=${at}`);
}
async function original(url, state) {  // a source without the PC: its outlet, read later
  const id = url.pathname.split("/")[2];
  const originals = state?.originals || {};
  const target = Object.hasOwn(originals, id) && originals[id];  // never /a/__proto__
  if (!target) return minimal();
  await encolar(`/a/${id}/estado?flag=read&value=true`);  // ponytail: queued, not sent, like a copy's opening
  try {
    return Response.redirect(target, 302);
  } catch {  // not a URL the browser accepts
    return minimal();
  }
}
function minimal() {
  const name = NAME.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
  const html = `<!doctype html><html lang="es"><head><meta charset="utf-8">`
    + `<meta name="viewport" content="width=device-width, initial-scale=1">`
    + `<meta name="color-scheme" content="light dark"><title>Sin el PC · ${name}</title></head>`
    + `<body><main style="font:1.1rem/1.5 system-ui,sans-serif;max-width:32rem;margin:20vh auto;padding:0 20px">`
    + `<h1 style="font-size:1.4rem">Esta página necesita el PC.</h1>`
    + `<p>El PC está dormido o fuera de alcance.</p>`
    + `<p><a href="/" style="display:inline-flex;align-items:center;min-height:44px">Volver a Hoy</a></p>`
    + `</main></body></html>`;
  return new Response(html, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
}
async function asset(request) {
  const state = await getState();
  const hit = state && (await caches.match(request, { cacheName: state.cache }));
  return hit || (await within(NAV_MS, fetch(request))) || Response.error();
}

// --- A page asks (back in the app): is the PC there, and which edition does it have? Then the
// queue goes and the copy catches up.
self.addEventListener("message", (event) => {
  const [port] = event.ports;
  if (event.data !== "sincronizar" || !port) return;
  event.waitUntil((async () => {
    const estado = await getJSON("/estado", NAV_MS);
    asleepUntil = estado ? 0 : Date.now() + ASLEEP_MS;
    const copia = (await getState())?.day ?? null;
    port.postMessage({ pc: !!estado, servidor: estado?.latest ?? null, copia });
    if (estado) {
      await enviar();
      await guardar(null);
    }
  })());
});

// --- The notice (#17).
self.addEventListener("push", (event) => {
  let data;
  try {
    data = event.data.json();
  } catch {
    data = { title: NAME, body: "" };  // DevTools' test push sends plain text
  }
  const daily = data.kind === "edicion";
  event.waitUntil(
    self.registration
      .showNotification(data.title, {
        body: data.body,
        icon: ICON,
        badge: BADGE,
        data,
        // The day's notice replaces yesterday's if it's still in the tray, and alerts again;
        // the test has its own tag, so it never eats the day's notice.
        tag: daily ? "edicion" : "confirmacion",
        renotify: daily,
      })
      // The copy is saved while the PC is awake for the notice (#20a); the ping doesn't wait.
      .then(() => Promise.all([ping(data, "entregado"), daily ? guardar(data) : null])),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const open = async () => {
    const [win] = await clients.matchAll({ type: "window", includeUncontrolled: true });
    if (win) {
      try {
        await win.focus();  // first: Chrome allows focusing only briefly after the tap
        await win.navigate("/");
        return;
      } catch {}  // closed meanwhile, focus refused, or a window this worker doesn't control
    }
    await clients.openWindow("/");
  };
  event.waitUntil(Promise.all([open(), ping(event.notification.data || {}, "tocado")]));
});

// Chrome 138+ tells the worker when the browser replaced the subscription.
self.addEventListener("pushsubscriptionchange", (event) => {
  const renew = async () => {
    const old = event.oldSubscription;
    const sub =
      event.newSubscription || (old && (await self.registration.pushManager.subscribe(old.options)));
    if (!sub) return;  // both null (permission granted again in the settings): Today shows the button
    await fetch("/aviso/alta", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: sub.toJSON(), old: old ? old.endpoint : null }),
    });
  };
  event.waitUntil(renew().catch(() => {}));  // the next visit to Today fixes what this missed
});
