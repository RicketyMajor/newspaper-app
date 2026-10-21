// Alba's service worker: only the daily notice (#17). No fetch handler and no cache: offline
// reading is #20. The server fills the name, the icon and the badge (data: URLs), so showing a
// notice never waits for the PC over the tailnet.
const NAME = __NAME__, ICON = __ICON__, BADGE = __BADGE__;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(clients.claim()));

// Best effort: if the tailnet is down the funnel loses a mark, never the notice.
function ping(data, event) {
  if (data.kind !== "edicion") return Promise.resolve();
  return fetch(`/aviso/${data.day}/${event}?s=${data.sid}`, { method: "POST" }).catch(() => {});
}

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
      .then(() => ping(data, "entregado"))
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
