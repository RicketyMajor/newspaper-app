// Poll today's edition while it is being prepared; announce it when ready.
const shown = document.body.dataset.today;
if (shown === "generating") {
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
