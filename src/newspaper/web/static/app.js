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
