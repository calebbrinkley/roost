"use strict";

// Roost's service worker: it shows notifications and opens what one is about when it is tapped.
// It caches nothing; every page and API request still goes to Roost.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Browsers require every push to show a notification, so a malformed one still shows something.
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { /* Shown with the defaults below. */ }
  const text = (value, fallback) => typeof value === "string" && value ? value : fallback;
  event.waitUntil(self.registration.showNotification(text(data.title, "Roost"), {
    body: text(data.body, ""), tag: text(data.tag, undefined), icon: "/icon-192.png",
    data: { url: text(data.url, "/") },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Only Roost's own pages: a notification can never send the app elsewhere.
  let url = new URL(event.notification.data?.url || "/", self.location.origin);
  if (url.origin !== self.location.origin) url = new URL("/", self.location.origin);
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (open) {
      // A conversation (?c=) or a job (?j=).
      if (url.search) open.postMessage({ link: url.search });
      return open.focus();
    }
    return self.clients.openWindow(url.href);
  })());
});
