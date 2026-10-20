/** @odoo-module **/

import { reactive } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";

/**
 * Bridge to the ABJ Android app (Capacitor shell loading this Odoo server).
 *
 * In the app, ``window.Capacitor`` exposes the native plugins: ``AbjNative`` (printers, app info)
 * and ``PushNotifications`` (FCM). On start the service registers the phone's FCM token on the
 * server (``checkinme.push.device``) and opens the URL of a tapped notification; on logout (user
 * menu, ABJ settings) it unregisters the token first.
 * In a normal browser every helper is a no-op (isNative() is false, info() resolves to null).
 */

const NATIVE_PLUGIN = "AbjNative";
const PUSH_PLUGIN = "PushNotifications";
// same name and description as MainActivity's channel (createChannel overwrites them)
const PUSH_CHANNEL = {
    id: "abj_default",
    name: "ABJ",
    description: "Planned visits, follow-up reminders and Odoo messages",
    importance: 4,
};
const UNREGISTER_TIMEOUT_MS = 5000;

function capacitor() {
    return window.Capacitor || null;
}

/** Only same-origin relative URLs ("/odoo/abj/12") are opened from a notification. */
function isSafeUrl(url) {
    return typeof url === "string" && url.startsWith("/") && !url.startsWith("//");
}

/** "Pixel 7" from the WebView user agent, or "Android". */
function deviceName() {
    const match = /Android [^;)]*;\s*([^;)]+?)(?:\s+Build\/|\)|;)/.exec(browser.navigator.userAgent || "");
    const name = match && match[1].trim();
    return name && name !== "K" && name !== "wv" ? name : "Android";
}

export const abjNativeService = {
    dependencies: ["orm", "notification"],

    start(env, { orm, notification }) {
        const pushStatus = reactive({
            // FCM usable in this app build (google-services.json present)
            available: false,
            // "unknown" | "prompt" | "prompt-with-rationale" | "granted" | "denied"
            permission: "unknown",
            registered: false,
            // archived by a manager in ABJ App Devices: the server ignores this phone
            blocked: false,
            error: "",
        });
        let infoPromise = null;
        // FCM token of this phone (from the "registration" listener)
        let pushToken = null;

        const service = {
            pushStatus,

            /** True inside the ABJ Android app. */
            isNative() {
                const cap = capacitor();
                try {
                    return Boolean(cap?.isNativePlatform?.() === true && cap.isPluginAvailable?.(NATIVE_PLUGIN));
                } catch {
                    return false;
                }
            },

            /** A native plugin proxy, or null outside the app / when the plugin is missing. */
            plugin(name) {
                const cap = capacitor();
                try {
                    if (!cap?.isNativePlatform?.() || !cap.isPluginAvailable?.(name)) {
                        return null;
                    }
                    return cap.Plugins?.[name] || null;
                } catch {
                    return null;
                }
            },

            /**
             * App information (cached): {platform, appVersion, appBuild, firebase, bluetooth, bluetoothEnabled}
             * or null outside the app.
             */
            info() {
                if (!infoPromise) {
                    const plugin = service.isNative() ? service.plugin(NATIVE_PLUGIN) : null;
                    infoPromise = plugin
                        ? plugin.getInfo().catch((error) => {
                              console.warn("ABJ app: getInfo failed", error);
                              infoPromise = null;
                              return null;
                          })
                        : Promise.resolve(null);
                }
                return infoPromise;
            },

            /** Ask for the notification permission again (settings screen button). */
            async requestPush() {
                try {
                    await registerPush({ prompt: true });
                } catch (error) {
                    pushStatus.error = error?.message || String(error);
                    console.warn("ABJ app: push permission request failed", error);
                }
                return pushStatus;
            },

            /** Open the Android settings page of the app (to allow a denied permission). */
            async openAppSettings() {
                try {
                    await service.plugin(NATIVE_PLUGIN)?.openAppSettings();
                } catch (error) {
                    console.warn("ABJ app: cannot open the app settings", error);
                }
            },

            /** FCM token of this phone, or null (no Firebase, permission denied, not received yet). */
            pushToken() {
                return pushToken;
            },

            /**
             * Before logging out: stop the notifications of this phone (server device and FCM
             * token), so the next user of a shared phone does not get them. Never throws and
             * never takes more than UNREGISTER_TIMEOUT_MS.
             */
            async unregisterPush() {
                const push = service.plugin(PUSH_PLUGIN);
                const token = pushToken;
                const steps = [];
                if (token) {
                    steps.push(orm.call("checkinme.push.device", "unregister_device", [token]));
                }
                if (pushStatus.available && push) {
                    // deletes the FCM token: the next login gets a new one (never without Firebase)
                    steps.push(Promise.resolve().then(() => push.unregister()));
                }
                let timer;
                const timeout = new Promise((resolve) => {
                    timer = browser.setTimeout(() => resolve([{ status: "rejected", reason: "timeout" }]), UNREGISTER_TIMEOUT_MS);
                });
                try {
                    const results = await Promise.race([Promise.allSettled(steps), timeout]);
                    for (const result of results) {
                        if (result.status === "rejected") {
                            console.warn("ABJ app: push not unregistered", result.reason);
                        }
                    }
                } finally {
                    browser.clearTimeout(timer);
                }
                pushToken = null;
                pushStatus.registered = false;
            },
        };

        function openUrl(url) {
            if (isSafeUrl(url)) {
                browser.location.assign(url);
            }
        }

        async function registerPush({ prompt }) {
            const push = service.plugin(PUSH_PLUGIN);
            if (!pushStatus.available || !push) {
                return;
            }
            let { receive } = await push.checkPermissions();
            if (prompt && (receive === "prompt" || receive === "prompt-with-rationale")) {
                ({ receive } = await push.requestPermissions());
            }
            pushStatus.permission = receive || "unknown";
            if (receive === "granted") {
                // the token arrives in the "registration" listener
                await push.register();
            }
        }

        async function setupPush() {
            const info = await service.info();
            const push = service.plugin(PUSH_PLUGIN);
            // Never call register() without Firebase: the native SDK would crash the app
            pushStatus.available = Boolean(info?.firebase && push);
            if (!pushStatus.available) {
                return;
            }
            try {
                await push.createChannel(PUSH_CHANNEL);
            } catch (error) {
                console.warn("ABJ app: notification channel not created", error);
            }
            await push.addListener("registration", async ({ value }) => {
                pushToken = value || null;
                try {
                    const result = await orm.call("checkinme.push.device", "register_device", [value], {
                        platform: "android",
                        device_name: deviceName(),
                        app_version: info.appVersion ? `${info.appVersion} (${info.appBuild || "?"})` : "",
                    });
                    pushStatus.blocked = result?.active === false;
                    pushStatus.registered = !pushStatus.blocked;
                    pushStatus.error = "";
                } catch (error) {
                    pushStatus.registered = false;
                    pushStatus.error = error?.data?.message || error?.message || String(error);
                    console.warn("ABJ app: device registration failed", error);
                }
            });
            await push.addListener("registrationError", (error) => {
                pushStatus.registered = false;
                pushStatus.error = error?.error || String(error);
                console.warn("ABJ app: FCM registration error", error);
            });
            await push.addListener("pushNotificationActionPerformed", (action) => {
                openUrl(action?.notification?.data?.abj_url);
            });
            // In the foreground the plugin already shows a system notification for messages with a
            // title / body (presentationOptions "alert"), whose tap fires the listener above. Only a
            // data-only message would go unnoticed: show it as a toast.
            await push.addListener("pushNotificationReceived", (message) => {
                if (message?.title || message?.body) {
                    return;
                }
                const data = message?.data || {};
                const url = data.abj_url;
                notification.add(data.body || "", {
                    title: data.title || "ABJ",
                    type: "info",
                    buttons: isSafeUrl(url) ? [{ name: _t("Open"), primary: true, onClick: () => openUrl(url) }] : [],
                });
            });
            await registerPush({ prompt: true });
        }

        /** User menu > Log out (full Odoo in the app): unregister this phone first. */
        function patchLogOutItem() {
            const items = registry.category("user_menuitems");
            const logOutItem = items.get("log_out", null);
            if (!logOutItem) {
                return;
            }
            items.add(
                "log_out",
                (env) => {
                    const item = logOutItem(env);
                    return {
                        ...item,
                        callback: async () => {
                            await service.unregisterPush();
                            item.callback();
                        },
                    };
                },
                { force: true }
            );
        }

        if (service.isNative()) {
            patchLogOutItem();
            setupPush().catch((error) => {
                pushStatus.error = error?.message || String(error);
                console.warn("ABJ app: push setup failed", error);
            });
        }
        return service;
    },
};

registry.category("services").add("abj_native", abjNativeService);
