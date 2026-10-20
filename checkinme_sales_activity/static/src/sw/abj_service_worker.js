// @odoo-module ignore
/* eslint-env serviceworker */
/* eslint-disable no-restricted-globals */

// ABJ service worker additions, prepended to Odoo's /web/service-worker.js by
// controllers/webmanifest.py (for every visitor). Plain script: no imports, no Odoo module.
//
// They only act on ABJ notifications (payload.options.data.abj, sent by checkinme.push) and stop
// the other listeners (mail's) for those; every other push or click goes through untouched.
(() => {
    const ABJ_APP_PATH = "/odoo/abj";

    function readAbjPayload(event) {
        if (!event.data) {
            return null;
        }
        let payload;
        try {
            payload = event.data.json();
        } catch {
            return null;
        }
        return payload && payload.options && payload.options.data && payload.options.data.abj
            ? payload
            : null;
    }

    self.addEventListener("push", (event) => {
        const payload = readAbjPayload(event);
        if (!payload) {
            return;
        }
        event.stopImmediatePropagation();
        // Always show it: iOS revokes the subscription of a push that displays nothing, and mail's
        // handler hides notifications while an Odoo tab is open.
        event.waitUntil(self.registration.showNotification(payload.title || "ABJ", payload.options));
    });

    async function openAbjUrl(url) {
        const target = new URL(url, self.location.origin);
        if (target.origin !== self.location.origin) {
            return;
        }
        const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        const client = windows.find((win) => {
            try {
                return new URL(win.url).pathname.startsWith(ABJ_APP_PATH);
            } catch {
                return false;
            }
        });
        if (client) {
            try {
                const focused = await client.focus();
                if (typeof (focused || client).navigate === "function") {
                    await (focused || client).navigate(target.href);
                    return;
                }
            } catch {
                // not controlled by this worker (navigate() rejects): open a window instead
            }
        }
        await self.clients.openWindow(target.href);
    }

    self.addEventListener("notificationclick", (event) => {
        const data = event.notification.data;
        const url = data && data.abj_url;
        // same-origin relative links only (the server never sends anything else)
        if (typeof url !== "string" || !url.startsWith("/") || url.startsWith("//")) {
            return;
        }
        event.stopImmediatePropagation();
        event.notification.close();
        event.waitUntil(openAbjUrl(url));
    });
})();
