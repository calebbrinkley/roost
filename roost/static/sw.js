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
  const approval = data.approval;
  const choices = approval && Array.isArray(approval.choices) ? approval.choices : [];
  const actions = [{action: "once", title: "Allow once"}, {action: "deny", title: "Deny"}]
    .filter(item => choices.includes(item.action)).slice(0, self.Notification?.maxActions || 0);
  event.waitUntil(self.registration.showNotification(text(data.title, "Roost"), {
    body: text(data.body, ""), tag: text(data.tag, undefined), icon: "/icon-192.png",
    actions, data: { url: text(data.url, "/"), approval },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Only Roost's own pages: a notification can never send the app elsewhere.
  let url = new URL(event.notification.data?.url || "/", self.location.origin);
  if (url.origin !== self.location.origin) url = new URL("/", self.location.origin);
  event.waitUntil((async () => {
    const approval = event.notification.data?.approval;
    if (["once", "deny"].includes(event.action) && approval && typeof approval.conversation === "string"
        && typeof approval.request_id === "string") {
      try {
        const response = await fetch(`/api/conversations/${encodeURIComponent(approval.conversation)}/approvals`, {
          method: "POST", credentials: "same-origin",
          headers: {"Content-Type": "application/json", "X-Roost-Request": "1"},
          body: JSON.stringify({request_id: approval.request_id, choice: event.action}),
        });
        if (!response.ok) throw new Error("Decision not confirmed");
        return self.registration.showNotification("Roost", {
          body: event.action === "once" ? "Allowed once." : "Denied.",
          tag: event.notification.tag, data: {url: url.pathname + url.search}, icon: "/icon-192.png",
        });
      } catch {
        // Login expired, offline or already resolved: open the card, never replay a decision.
        await self.registration.showNotification("Review this request in Roost", {
          body: "Your decision was not confirmed. Open Roost to check the request.",
          tag: event.notification.tag, data: {url: url.pathname + url.search}, icon: "/icon-192.png",
        });
      }
    }
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((client) => new URL(client.url).origin === self.location.origin);
    if (open) {
      // A conversation (?c=), a job (?j=) or a message the agent sent (?d=).
      if (url.search) open.postMessage({ link: url.search });
      return open.focus();
    }
    return self.clients.openWindow(url.href);
  })());
});
