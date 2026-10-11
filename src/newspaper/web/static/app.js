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
  const res = await fetch(form.action, {
    method: "POST",
    headers: { Accept: "application/json" },
  }).catch(() => null);
  if (!res || !res.ok) {
    // Never re-post: the server may have applied it already and a second POST would undo it.
    live.textContent = "No se pudo guardar el cambio. Recarga la página.";
    return;
  }
  const { value } = await res.json();
  button.setAttribute("aria-pressed", String(value));
  if (new URL(form.action).searchParams.get("flag") === "read") {
    form.closest("li")?.classList.toggle("is-read", value);  // an original's title styling
  }
  live.textContent = `${button.lastChild.textContent.trim()}: ${value ? "sí" : "no"}`;
});

// Opening an original marks it read on the server; show it now, not on the next visit.
document.addEventListener("click", (event) => {
  const item = event.target.closest('a[href^="/a/"]')?.closest("li");
  item?.classList.add("is-read");
  item?.querySelector('form[action$="flag=read"] button')?.setAttribute("aria-pressed", "true");
});
