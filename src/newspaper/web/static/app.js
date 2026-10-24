// Reading without the PC (#20a): the worker served a saved copy of this page.
const copy = document.body.hasAttribute("data-sin-pc");
// One registration for the whole app: the copy and the queue (tailnet only) and the notices.
const worker = "serviceWorker" in navigator && isSecureContext
  ? navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready)
  : null;
worker?.catch(() => {});  // a refused worker: the app reads live as before
// The first visit loads before the worker takes over; once it does, it saves the copy (and
// sends the queue) without waiting for another page.
if (worker && !navigator.serviceWorker.controller) {
  navigator.serviceWorker.addEventListener("controllerchange", ask, { once: true });
}
// The installed app asks to keep the copy under storage pressure; the answer is only logged.
if (matchMedia("(display-mode: standalone)").matches) {
  navigator.storage?.persist?.().then((kept) => console.info("Newspaper: almacenamiento persistente:", kept), () => {});
}

// Poll until today's edition is ready (it may be starting in the background); announce it.
const shown = document.body.dataset.today;
if (shown !== "ready") {
  const timer = setInterval(async () => {
    const res = await fetch("/estado").catch(() => null);
    if (!res || !res.ok) return;
    const { today } = await res.json();
    if (today === "ready") {
      document.querySelector(".banner.ready").hidden = false;
      clearInterval(timer);
    }
  }, 30000);
}

// Reading toggles without a reload, so the scroll, the focus and Back stay where they were.
// Without JS the form posts and the server redirects back (to the source's anchor).
const live = document.getElementById("live");
document.addEventListener("submit", async (event) => {
  const form = event.target.closest(".toggles form");
  if (!form) return;
  event.preventDefault();
  if (copy) {  // marks without the PC are #20b
    // The caption moves under the row tapped: a source's or Guardado's would show nothing.
    const caption = document.getElementById("espera");
    form.closest(".toggles").after(caption);
    caption.hidden = false;
    live.textContent = "Las marcas esperan al PC.";
    return;
  }
  const button = form.querySelector("button");
  // Send the state the button promises, not a toggle: a mark landing meanwhile (the automatic
  // one at the sources) can't turn the press into its opposite.
  const url = new URL(form.action);
  url.searchParams.set("value", String(button.getAttribute("aria-pressed") !== "true"));
  const res = await fetch(url, {
    method: "POST",
    headers: { Accept: "application/json" },
  }).catch(() => null);
  if (!res || !res.ok) {
    live.textContent = "No se pudo guardar el cambio. Recarga la página.";
    return;
  }
  const { value } = await res.json();
  button.setAttribute("aria-pressed", String(value));
  if (new URL(form.action).searchParams.get("flag") === "read") {
    form.closest("li")?.classList.toggle("is-read", value);  // an original's title styling
    // Unmarking the note takes back the "Leída · el sol subió" line the auto-mark showed.
    // Only the note's own toggle: an original's sits in the same article.
    const own = form.closest("article.note > .toggles");
    if (!value && own) own.parentElement.querySelector(".done")?.setAttribute("hidden", "");
  }
  live.textContent = `${button.lastChild.textContent.trim()}: ${value ? "sí" : "no"}`;
});

// Opening an original marks it read on the server; show it now, not on the next visit.
document.addEventListener("click", (event) => {
  const item = event.target.closest('a[href^="/a/"]')?.closest("li");
  item?.classList.add("is-read");
  item?.querySelector('form[action$="flag=read"] button')?.setAttribute("aria-pressed", "true");
});

// A note counts as read once its sources come into view, after the reader has scrolled.
// Opening it doesn't. Touching "Leída" by hand wins for the rest of the visit.
const note = document.querySelector("article.note");
const readForm = note?.querySelector(':scope > .toggles form[action$="flag=read"]');
if (readForm && "IntersectionObserver" in window && !copy) {
  let touched = false;
  readForm.addEventListener("submit", () => (touched = true));
  const mark = new IntersectionObserver(async (entries) => {
    if (!entries.some((e) => e.isIntersecting)) return;
    mark.disconnect();
    const button = readForm.querySelector("button");
    if (touched || button.getAttribute("aria-pressed") === "true") return;
    const url = new URL(readForm.action);
    url.searchParams.set("value", "true");  // set, not toggle: a reload never unmarks it
    const res = await fetch(url, { method: "POST", headers: { Accept: "application/json" } })
      .catch(() => null);
    if (!res || !res.ok || touched) return;
    button.setAttribute("aria-pressed", "true");
    const { inCore, coreRead, coreTotal } = note.dataset;
    const text = inCore === "true" ? `Leída · el sol subió: ${+coreRead + 1} de ${coreTotal}` : "Leída";
    const done = note.querySelector(".done");
    done.lastElementChild.textContent = text;
    done.hidden = false;
    live.textContent = text;
  }, { threshold: 0.1 });
  // ponytail: waits for the first scroll, so a note that fits on one screen needs the button.
  addEventListener("scroll", () => mark.observe(document.getElementById("fuentes")), { once: true });
}

// Walking the reading path: swipe or keys go to the previous or next note. The direction rides
// along to the next page, where pagereveal (inline in base.html) picks the slide.
function rememberDirection(rel) {
  try { sessionStorage.alba_dir = rel === "next" ? "adelante" : "atras"; } catch {}
}
function walk(rel) {
  const link = document.querySelector(`link[rel="${rel}"]`);
  if (!link) return;
  rememberDirection(rel);
  location.href = link.href;
}
document.addEventListener("click", (event) => {
  const rel = event.target.closest('a[rel="next"], a[rel="prev"]')?.rel;
  if (rel) rememberDirection(rel);  // the "Siguiente" card slides like a swipe
});
if (note) {
  // Touch events, not pointer events: Chrome cancels the pointer once it decides it's a scroll.
  let start = null;
  note.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    start = e.touches.length === 1 ? { x: t.clientX, y: t.clientY, at: e.timeStamp } : null;
  }, { passive: true });
  note.addEventListener("touchend", (e) => {
    if (!start || !getSelection().isCollapsed) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - start.x, dy = t.clientY - start.y;
    const quick = Math.abs(dx) > 60 && e.timeStamp - start.at < 300;
    if ((Math.abs(dx) > innerWidth * 0.25 || quick) && Math.abs(dy) < 60) walk(dx < 0 ? "next" : "prev");
    start = null;
  }, { passive: true });
}

// Today: remember the edition (the sunrise plays once) and how far the sun rose. The inline
// script in front.html reads these before the first paint.
const rail = document.querySelector(".rail[data-day]");
if (rail) {
  try {
    localStorage["alba:visto"] = rail.dataset.day;
    localStorage["alba:" + rail.dataset.day] = rail.dataset.read;
  } catch {}
}

// Shortcuts, outside text fields: / search, g Today, ? the list; on a note, ←/→ or j/k walk
// and m, f, s press Leída, Favorita and Guardar.
document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target.closest("input, textarea, select, [contenteditable]")) return;
  if (document.querySelector("dialog[open]")) return;
  const flags = { m: "read", f: "favorite", s: "saved" };
  if (event.key === "/") location.href = "/buscar";
  else if (event.key === "g") location.href = "/";
  else if (event.key === "?") document.getElementById("atajos")?.showModal();
  else if (note && (event.key === "ArrowRight" || event.key === "j")) walk("next");
  else if (note && (event.key === "ArrowLeft" || event.key === "k")) walk("prev");
  else if (note && flags[event.key]) {
    note.querySelector(`:scope > .toggles form[action$="flag=${flags[event.key]}"] button`)?.click();
  } else return;
  event.preventDefault();
});

// The daily notice (#17): one line at the foot of Today. Chrome asks for permission only after a
// tap. Activating, deactivating and the per-visit check take turns across the installed app and
// any tab (they share one registration), and a deactivation always wins.
const aviso = document.querySelector(".aviso[data-key]");
if (aviso) setUpNotices(aviso);

function setUpNotices(line) {
  const { key, name } = line.dataset;
  const unavailable = "Los avisos no están disponibles en este navegador.";
  const supported = "serviceWorker" in navigator && "PushManager" in window &&
    "Notification" in window && isSecureContext;
  if (!supported) return show(unavailable);

  // data-key is base64url without padding: atob accepts it once - and _ are back to + and /.
  const keyBytes = Uint8Array.from(atob(key.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const mobile = navigator.userAgentData?.mobile ?? /Android|iPhone|iPad/.test(navigator.userAgent);
  const locked = (fn) => (navigator.locks ? navigator.locks.request("alba-aviso", fn) : fn());
  const post = (url, body) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const sameKey = (buffer) => {
    const bytes = new Uint8Array(buffer || []);
    return bytes.length === keyBytes.length && bytes.every((b, i) => b === keyBytes[i]);
  };
  const registration = worker;
  // Every user awaits it and handles a failure; this keeps one nobody awaited yet (permission
  // denied, a browser set to refuse workers) out of the console as an unhandled rejection.
  registration.catch(() => {});

  function show(text, label, action) {
    // Each render replaces the button: a keyboard user on the old one lands on the new one, or
    // on the line itself when no button is left (blocked), which also reads it out.
    const focused = line.contains(document.activeElement);
    line.textContent = text;
    line.hidden = false;
    if (label) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", action);
      line.append(text ? " " : "", button);
      if (focused) button.focus();
    } else if (focused) {
      line.tabIndex = -1;
      line.focus();
    }
  }

  // Never rejects: it ends every attempt and the page load, where nothing else would catch it.
  async function render() {
    if (Notification.permission === "denied") {
      return show("Los avisos están bloqueados. Puedes activarlos en los ajustes de notificaciones de la app o del sitio.");
    }
    let sub;
    try {
      sub = await (await registration).pushManager.getSubscription();
    } catch {
      return show(unavailable);  // the worker never registered: nothing below can work
    }
    if (sub && Notification.permission === "granted") show(`Te aviso cuando salga ${name}.`, "Desactivar avisos", off);
    else show("", `Avisarme cuando salga ${name}`, on);
  }

  // A new subscription, with `confirm` (activating) or `old` (renewing: the server updates that row
  // in place, so its id survives).
  async function fresh(reg, extra) {
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes });
    const res = await post("/aviso/alta", { subscription: sub.toJSON(), mobile, ...extra }).catch(() => null);
    if (!res?.ok) {
      // A refusal (4xx: an unknown push service, say) is for good, and so is any failure while
      // activating, with the person right there to tap again: drop it, or the line would say
      // "Te aviso" with nothing to come. A renewal that only met no network or a 5xx (the
      // database busy with generate) keeps it: the next visit's check sends it again.
      if (extra.confirm || (res && res.status < 500)) await sub.unsubscribe();
      throw new Error("alta");
    }
    return sub;
  }

  // The announcement goes after the render: moving the focus to the new button would cut it off.
  const on = () => locked(async () => {
    let said = "Avisos activados.";
    try {
      const reg = await registration;
      if ((await Notification.requestPermission()) !== "granted") throw new Error("permiso");
      await fresh(reg, { confirm: true });
    } catch {
      said = "No se pudieron activar los avisos.";
    }
    await render();
    live.textContent = said;
  });

  const off = () => locked(async () => {
    let said = "Avisos desactivados.";
    try {
      const sub = await (await registration).pushManager.getSubscription();
      // The browser first: if the server never hears, its next send gets 404/410 and marks it gone.
      if (sub && (await sub.unsubscribe())) await post("/aviso/baja", { endpoint: sub.endpoint }).catch(() => {});
    } catch {
      said = "No se pudieron desactivar los avisos.";
    }
    await render();
    live.textContent = said;
  });

  // Each visit: the server learns the subscription again (a new or restored database). A dead
  // endpoint (gone) or a regenerated key gets a fresh one; a plain "new" row is left alone.
  const check = () => locked(async () => {
    const reg = await registration;
    const sub = await reg.pushManager.getSubscription();
    if (!sub || Notification.permission !== "granted") return;
    // A regenerated key needs no POST to know; a dead endpoint, the server's answer.
    const stale = !sameKey(sub.options.applicationServerKey) ||
      (await post("/aviso/alta", { subscription: sub.toJSON(), mobile })
        .then(async (res) => res.ok && (await res.json()).gone));
    if (stale) {
      await sub.unsubscribe();  // same key: subscribe() alone would hand back this very one
      await fresh(reg, { old: sub.endpoint });
    }
  });

  render().then(check).catch(() => {}).finally(render);
}

// Back in the app: it resumes the page it left without asking for it. On the latest edition's
// pages (data-live), the worker checks the PC (and sends the queue, and refreshes the copy): a
// newer edition reloads a live Today and offers itself elsewhere; with the PC away, Today says
// so. A copy never reloads by itself.
const liveDay = document.body.dataset.live;
if (liveDay) {
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState !== "visible") return;
    const answer = await ask();
    if (!answer) return;
    const today = document.querySelector(".rail");
    if (!answer.pc) {
      if (today) showSinPc(!copy);  // read out on a live Today; a copy's line is the page's
      return;
    }
    if (sinPc && !copy) sinPc.hidden = true;  // the PC is back: a live Today's marks don't wait
    // ISO dates (YYYY-MM-DD) compare as text. Later, not just different: a --force under way
    // briefly says yesterday or null.
    if (!answer.servidor || answer.servidor <= liveDay) return;
    if (today && !copy) location.replace("/");  // a live front page: Today itself
    else document.querySelector(".banner.ready").hidden = false;
  });
}

// The worker's view of the PC; without a worker, or with no answer in 5 s, the old check.
async function ask() {
  const control = navigator.serviceWorker?.controller;
  if (control) {
    const answer = await new Promise((resolve) => {
      const { port1, port2 } = new MessageChannel();
      const timer = setTimeout(() => resolve(null), 5000);
      port1.onmessage = (event) => { clearTimeout(timer); resolve(event.data); };
      control.postMessage("sincronizar", [port2]);
    });
    if (answer) return answer;
  }
  const res = await fetch("/estado").catch(() => null);
  if (!res || !res.ok) return null;
  return { pc: true, servidor: (await res.json()).latest };
}

// Without the PC (#20a): the line at the foot of Today, and the copy's marks as of the last
// sync (the base), with the buttons waiting for the PC.
const sinPc = document.querySelector(".sin-pc");
function showSinPc(announce) {
  if (!sinPc) return;
  const day = document.body.dataset.copia || document.querySelector(".rail")?.dataset.day;
  const today = new Date().toLocaleDateString("sv");  // YYYY-MM-DD, local time
  const notice = sinPc.dataset.aviso;
  let text = "Sin el PC: las marcas esperan a que vuelva.";
  if (day && day !== today) {
    const long = new Date(`${day}T12:00`).toLocaleDateString("es", { day: "numeric", month: "long" });
    const before = notice && new Date().toTimeString().slice(0, 5) < notice;
    text = `Es la edición del ${long}. ` + (before
      ? `La de hoy llega con el aviso de las ${notice}.`
      : "La de hoy se guarda cuando el teléfono alcance al PC.");
  }
  if (!announce || !sinPc.hidden) {
    sinPc.textContent = text;
    sinPc.hidden = false;
    return;
  }
  // A status that appears already filled isn't read out: it appears empty, and the text lands
  // once a frame has drawn it.
  sinPc.textContent = "";
  sinPc.hidden = false;
  requestAnimationFrame(() => requestAnimationFrame(() => (sinPc.textContent = text)));
}

function applyMarks(base) {
  const has = (kind, id, letter) => (base[kind]?.[id] || "").includes(letter);
  const letters = { read: "r", favorite: "f", saved: "s" };
  for (const form of document.querySelectorAll(".toggles form")) {
    const [, kind, id, flag] =
      form.getAttribute("action").match(/^\/([na])\/(\d+)\/estado\?flag=(\w+)/) || [];
    if (!kind) continue;
    const on = has(kind, id, letters[flag]);
    const button = form.querySelector("button");
    button.setAttribute("aria-pressed", String(on));
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("aria-describedby", "espera");
    if (kind === "a" && flag === "read") form.closest("li")?.classList.toggle("is-read", on);
  }
  // One caption for every waiting button: under the note's own row, elsewhere hidden (it still
  // describes them) until a tap moves it under the row tapped.
  const caption = Object.assign(document.createElement("p"), {
    id: "espera", className: "espera", textContent: "Las marcas esperan al PC.",
  });
  const own = note?.querySelector(":scope > .toggles");
  caption.hidden = !own;
  (own || document.querySelector(".toggles"))?.after(caption);
  // Today's cards and Guardado's lines, read or not as the base says, with the check the server
  // draws (read_mark): the copy is usually saved at the notice, before any reading. A read card
  // loses "Sigue aquí", which doesn't move to the next one.
  for (const link of document.querySelectorAll('a[href^="/n/"]')) {
    const card = link.closest(".big, .line");
    if (!card) continue;
    const on = has("n", link.getAttribute("href").slice(3), "r");
    card.classList.toggle("done", on);
    const title = card === link ? link.firstElementChild : link;  // a line's headline is its span
    if (!on) title.querySelectorAll(".check, .check + .sr-only").forEach((mark) => mark.remove());
    else if (!title.querySelector(".check")) {
      title.insertAdjacentHTML("afterbegin", '<span class="check" aria-hidden="true">✓</span><span class="sr-only">Leída: </span>');
    }
    const kicker = card.querySelector(".k");
    if (on && kicker?.firstChild?.textContent.startsWith("Sigue aquí")) kicker.firstChild.textContent = "";
  }
  const core = (document.body.dataset.nucleo || "").split(" ").filter(Boolean);
  const read = core.filter((id) => has("n", id, "r"));
  const rail = document.querySelector(".rail");
  if (rail && core.length) {  // the sun and the line above it
    rail.dataset.read = read.length;
    rail.style.setProperty("--sol", read.length / core.length);
    try { localStorage["alba:" + rail.dataset.day] = read.length; } catch {}
    const prog = document.querySelector(".prog");
    prog.textContent = `Lo esencial: ${read.length} de ${core.length}.`;
    if (read.length === core.length) {
      const link = document.createElement("a");
      link.href = `/fin/${rail.dataset.day}`;
      link.textContent = "Pleno día.";
      prog.append(" ", link);
    } else {
      prog.append(" El sol sube con cada una.");
    }
  }
  const lead = document.querySelector(".noon-wrap .lead");
  if (lead && core.length) {  // Pleno día, saved either way: the base may have moved both ways
    const left = core.length - read.length;
    const back = document.querySelector(".noon-wrap .left");
    if (left) {
      lead.textContent = `Te faltan ${left} de las ${core.length}\u00a0notas esenciales.`;
      if (!back) {
        const edition = document.querySelector(".noon-wrap .cta .s").getAttribute("href");
        lead.insertAdjacentHTML("afterend", `<p class="left"><a href="${edition}">Volver a ellas</a></p>`);
      }
    } else if (back) {  // finished since it was saved; a page saved finished keeps its own line
      lead.textContent = "Terminaste lo esencial.";
      back.remove();
    }
  }
}

if (copy) {
  showSinPc();
  applyMarks(JSON.parse(document.body.dataset.marcas || "{}"));
}
