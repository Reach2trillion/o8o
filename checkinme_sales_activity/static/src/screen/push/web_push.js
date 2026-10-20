/** @odoo-module **/

import { browser } from "@web/core/browser/browser";
import { isDisplayStandalone, isIOS } from "@web/core/browser/feature_detection";

/**
 * Web push for the installed web app (PWA): subscribes this browser with Odoo's VAPID key and
 * registers it as a ``mail.push.device`` exactly like the mail module does, so both Odoo's own
 * notifications and the ABJ ones reach the phone. The Android app uses FCM instead (abj_native).
 */

const DEVICE_MODEL = "mail.push.device";
// same key as the mail module (mail/static/src/webclient/web/webclient.js)
const ENDPOINT_KEY = `${DEVICE_MODEL}_endpoint`;
const SW_SCOPE = "/odoo";
const UNREGISTER_TIMEOUT_MS = 5000;

/** base64url (VAPID public key) -> bytes */
export function b64urlToUint8(value) {
    const padding = "=".repeat((4 - (value.length % 4)) % 4);
    const raw = atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

/** bytes -> base64url without padding (format of the VAPID key stored by Odoo) */
export function uint8ToB64url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (const byte of bytes) {
        binary += String.fromCharCode(byte);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** True when this browser can receive web push (false in the Android app WebView). */
export function webPushSupported() {
    return Boolean(
        browser.navigator.serviceWorker && "PushManager" in window && "Notification" in window
    );
}

/**
 * @returns {"unsupported"|"ios-install-first"|"prompt"|"granted"|"denied"}
 */
export function webPushStatus() {
    if (isIOS() && !isDisplayStandalone()) {
        // iOS only offers web push to web apps added to the Home Screen (iOS 16.4+)
        return "ios-install-first";
    }
    if (!webPushSupported()) {
        return "unsupported";
    }
    switch (window.Notification.permission) {
        case "granted":
            return "granted";
        case "denied":
            return "denied";
        default:
            return "prompt";
    }
}

async function getRegistration() {
    const container = browser.navigator.serviceWorker;
    const registration = (await container.getRegistration(SW_SCOPE)) || (await container.getRegistration());
    if (!registration) {
        throw new Error("No service worker is registered for /odoo.");
    }
    return registration;
}

/**
 * Prefetch what the subscription needs (service worker registration + VAPID key), so that the
 * click handler can call ``pushManager.subscribe()`` as its very first await: Safari only shows
 * the permission prompt when subscribe() is called directly from the user gesture.
 *
 * @returns {Promise<{registration: ServiceWorkerRegistration, applicationServerKey: Uint8Array, vapidKey: string}|null>}
 */
export async function prefetchWebPush(orm) {
    if (!webPushSupported()) {
        return null;
    }
    try {
        const [registration, vapidKey] = await Promise.all([
            getRegistration(),
            orm.call(DEVICE_MODEL, "get_web_push_vapid_public_key", []),
        ]);
        return { registration, vapidKey, applicationServerKey: b64urlToUint8(vapidKey) };
    } catch (error) {
        console.warn("ABJ push: prefetch failed", error);
        return null;
    }
}

async function subscribe(registration, applicationServerKey) {
    try {
        return await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
    } catch (error) {
        if (error?.name !== "InvalidStateError") {
            throw error;
        }
        // subscribed with another key (VAPID keys were regenerated): start over
        const existing = await registration.pushManager.getSubscription();
        await existing?.unsubscribe();
        return registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
    }
}

/**
 * Enable web push from a click handler. Call it synchronously from the handler (no await before):
 * with a prefetched registration the first awaited call is ``pushManager.subscribe()``, which
 * shows the permission prompt; otherwise ``Notification.requestPermission()`` is.
 *
 * @param {object} orm
 * @param {object|null} prefetched result of ``prefetchWebPush``
 * @returns {Promise<"granted"|"denied"|"prompt"|"unsupported">}
 */
export async function enableWebPush(orm, prefetched) {
    if (!webPushSupported()) {
        return "unsupported";
    }
    let subscription;
    let vapidKey;
    if (prefetched) {
        try {
            subscription = await subscribe(prefetched.registration, prefetched.applicationServerKey);
            vapidKey = prefetched.vapidKey;
        } catch (error) {
            if (window.Notification.permission === "denied") {
                return "denied";
            }
            if (error?.name === "NotAllowedError") {
                return "prompt"; // prompt dismissed
            }
            throw error;
        }
    } else {
        const permission = await window.Notification.requestPermission();
        if (permission !== "granted") {
            return permission === "denied" ? "denied" : "prompt";
        }
        const registration = await getRegistration();
        vapidKey = await orm.call(DEVICE_MODEL, "get_web_push_vapid_public_key", []);
        subscription = await subscribe(registration, b64urlToUint8(vapidKey));
    }
    const kwargs = subscription.toJSON();
    const previousEndpoint = readPushEndpoint();
    if (previousEndpoint && previousEndpoint !== subscription.endpoint) {
        kwargs.previousEndpoint = previousEndpoint;
    }
    const key = subscription.options?.applicationServerKey;
    kwargs.vapid_public_key = key ? uint8ToB64url(key) : vapidKey;
    await orm.call(DEVICE_MODEL, "register_devices", [], kwargs);
    try {
        browser.localStorage.setItem(ENDPOINT_KEY, subscription.endpoint);
    } catch {
        // storage unavailable (private mode): mail re-registers on the next load
    }
    return "granted";
}

/** Web push endpoint of this browser as registered on the server (null when unknown). */
export function readPushEndpoint() {
    try {
        return browser.localStorage.getItem(ENDPOINT_KEY);
    } catch {
        return null;
    }
}

/**
 * Logout: remove this browser's ``mail.push.device`` so the server stops sending to it. The
 * browser subscription is kept: mail registers it again for whoever logs in next (no new
 * permission prompt). Never takes more than UNREGISTER_TIMEOUT_MS.
 */
export async function unregisterWebPush(orm) {
    const endpoint = readPushEndpoint();
    if (!endpoint) {
        return;
    }
    let timer;
    const timeout = new Promise((resolve, reject) => {
        timer = browser.setTimeout(() => reject(new Error("unregister_devices timed out")), UNREGISTER_TIMEOUT_MS);
    });
    try {
        await Promise.race([orm.call(DEVICE_MODEL, "unregister_devices", [], { endpoint }), timeout]);
    } finally {
        browser.clearTimeout(timer);
    }
}
