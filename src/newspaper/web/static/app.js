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
if (readForm && "IntersectionObserver" in window) {
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
