/** @odoo-module **/

import { Component, onWillDestroy, onWillStart, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { isDisplayStandalone, isIOS } from "@web/core/browser/feature_detection";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useSetupAction } from "@web/search/action_hook";
import {
    loadPrinterSettings,
    PAPER_WIDTHS,
    ReceiptPrinter,
    savePrinterSettings,
} from "@checkinme_sales_activity/screen/printing/printer_service";
import { PrintError, removePrintArea } from "@checkinme_sales_activity/screen/printing/transports";
import {
    enableWebPush,
    prefetchWebPush,
    readPushEndpoint,
    unregisterWebPush,
    webPushStatus,
    webPushSupported,
} from "@checkinme_sales_activity/screen/push/web_push";

const GEO_OPTIONS = { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 };
const SEARCH_DEBOUNCE_MS = 300;
const PHOTO_MAX_SIDE = 1600;
const PHOTO_QUALITY = 0.8;
const PUSH_BANNER_KEY = "abj.push_banner_dismissed";
const INSTALL_URL = "/scoped_app?app_id=checkinme_sales_activity&app_name=ABJ&path=odoo/abj";
// back to the app (not the backend) after the next login
const LOGOUT_URL = `/web/session/logout?redirect=${encodeURIComponent("/odoo/abj")}`;
// business errors already explained to the user by the toast (not bugs)
const EXPECTED_ERRORS = [
    "odoo.exceptions.UserError",
    "odoo.exceptions.AccessError",
    "odoo.exceptions.MissingError",
    "odoo.exceptions.ValidationError",
];

function formatDistance(meters) {
    if (meters === null || meters === undefined) {
        return "";
    }
    return meters >= 1000 ? `${(meters / 1000).toFixed(1)} km` : `${Math.round(meters)} m`;
}

function formatCoord(value) {
    return Number(value || 0).toFixed(5);
}

function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
    });
}

/**
 * Downscale a camera photo (long side <= 1600 px, JPEG 0.8) before upload: phone photos are
 * 3-6 MB, which is slow on mobile data. Falls back to the original file on any failure.
 *
 * @returns {Promise<{base64: string, mimetype: string}>}
 */
async function compressPhoto(file) {
    const original = async () => ({ base64: await readFileAsBase64(file), mimetype: file.type || "image/jpeg" });
    const url = URL.createObjectURL(file);
    try {
        const img = new Image();
        img.src = url;
        await img.decode(); // the browser applies the EXIF orientation
        const ratio = Math.min(1, PHOTO_MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(img.naturalWidth * ratio));
        canvas.height = Math.max(1, Math.round(img.naturalHeight * ratio));
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#fff"; // transparent PNG -> white JPEG background
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        const base64 = canvas.toDataURL("image/jpeg", PHOTO_QUALITY).split(",")[1] || "";
        // already small JPEG: keep the original
        if (!base64 || (ratio === 1 && file.type === "image/jpeg" && file.size <= (base64.length * 3) / 4)) {
            return await original();
        }
        return { base64, mimetype: "image/jpeg" };
    } catch (error) {
        console.warn("Sale Check: photo not compressed", error);
        return await original();
    } finally {
        URL.revokeObjectURL(url);
    }
}

function parseServerDatetime(value) {
    // "YYYY-MM-DD HH:MM:SS" in UTC
    return value ? new Date(value.replace(" ", "T") + "Z") : null;
}

function readFlag(key) {
    try {
        return browser.localStorage.getItem(key) === "1";
    } catch {
        return false;
    }
}

function writeFlag(key) {
    try {
        browser.localStorage.setItem(key, "1");
    } catch {
        // storage unavailable: the flag only lasts for this page
    }
}

/**
 * Sale Check phone screen: a single-page flow for field salespeople.
 * Home (today, open visit, targets) -> Check in (GPS -> customer -> details) -> Check out -> Result.
 * Every step calls a regular ORM method, so access rights, GPS verification and Telegram
 * notifications are handled by the server exactly as in the backend views.
 * The same screen is the ABJ phone app (installed web app at /odoo/abj, or the Android app):
 * settings for notifications and receipt printers live in the "settings" view.
 */
export class CheckinmeScreen extends Component {
    static template = "checkinme_sales_activity.Screen";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.abjNative = useService("abj_native");
        this.printer = new ReceiptPrinter({ native: this.abjNative });
        this.nativePush = useState(this.abjNative.pushStatus);
        this.state = useState({
            view: "home",
            loading: true,
            busy: false,
            dashboard: null,
            geo: { status: "idle", latitude: 0, longitude: 0, accuracy: 0, error: "", denied: false },
            checkin: this.emptyCheckin(),
            checkout: { visit: null, outcome: "", sale_amount: "", notes: "", next_action_date: "" },
            result: null,
            resultKind: "checkin",
            history: [],
            customerQuery: "",
            customers: [],
            searching: false,
            // receipt printing (settings of this device)
            printer: loadPrinterSettings(),
            printerUi: { devices: [], loadingDevices: false, busy: false, preview: "" },
            printing: "",
            // notifications
            push: { status: webPushStatus(), busy: false, testing: false, bannerDismissed: readFlag(PUSH_BANNER_KEY) },
            appInfo: null,
            loggingOut: false,
        });
        this.searchTimer = null;
        this.webPushPrefetch = null;
        // back from a form opened with "Edit in Odoo" (breadcrumb): reopen the visit that was on
        // screen, not the one of the original deep link
        useSetupAction({ getLocalState: () => ({ abjVisitId: this.urlVisitId || false }) });
        const restored = this.props.state && "abjVisitId" in this.props.state;
        if (restored) {
            this.pendingOpenId = this.props.state.abjVisitId || null;
            this.urlVisitId = this.props.state.abjVisitId || undefined;
        } else {
            // deep links: backend "Open in phone screen" (context) and /odoo/abj/<id> (router resId)
            this.pendingOpenId = this.props.action?.context?.checkinme_open_id || this.props.resId || null;
            this.urlVisitId = this.props.resId || undefined;
        }

        onWillStart(async () => {
            this.prefetchWebPush();
            this.abjNative.info().then((info) => (this.state.appInfo = info));
            await this.loadDashboard();
            if (this.pendingOpenId) {
                // only the RPC delays the first render: the GPS fix arrives on the mounted screen
                await this.openVisit(this.pendingOpenId, { waitGps: false });
            }
        });
        onWillDestroy(() => {
            browser.clearTimeout(this.searchTimer);
            removePrintArea();
        });
    }

    // ------------------------------------------------------------------
    // Getters
    // ------------------------------------------------------------------
    get dashboard() {
        return this.state.dashboard;
    }

    get title() {
        switch (this.state.view) {
            case "checkin":
                return _t("New check-in");
            case "checkout":
                return _t("Check out");
            case "result":
                return this.state.resultKind === "view" ? _t("Visit") : _t("Done");
            case "history":
                return _t("My visits");
            case "settings":
                return _t("Settings");
            default:
                return this.app.name || _t("ABJ");
        }
    }

    get hasPosition() {
        return this.state.geo.status === "ok";
    }

    get requireGps() {
        return Boolean(this.dashboard && this.dashboard.settings.require_gps);
    }

    get selectedType() {
        return (this.dashboard?.types || []).find((t) => t.id === this.state.checkin.type_id) || null;
    }

    get customerSections() {
        const sections = [
            { key: "search", label: _t("Results") },
            { key: "nearby", label: _t("Customers near you") },
            { key: "recent", label: _t("Recently visited") },
        ];
        return sections
            .map((s) => ({ ...s, items: this.state.customers.filter((c) => c.section === s.key) }))
            .filter((s) => s.items.length);
    }

    get openVisitElapsed() {
        const visit = this.dashboard?.open_visit;
        const start = visit && parseServerDatetime(visit.checkin_time);
        if (!start) {
            return "";
        }
        const minutes = Math.max(0, Math.round((Date.now() - start.getTime()) / 60000));
        if (minutes < 60) {
            return _t("%s min", minutes);
        }
        return _t("%sh %smin", Math.floor(minutes / 60), String(minutes % 60).padStart(2, "0"));
    }

    get canSubmitCheckin() {
        if (this.state.busy || !this.selectedType) {
            return false;
        }
        if (this.requireGps && !this.hasPosition) {
            return false;
        }
        return !(this.selectedType.requires_customer && !this.state.checkin.partner);
    }

    /** "app" block of the dashboard (older servers do not send it). */
    get app() {
        return this.dashboard?.app || {};
    }

    get isNative() {
        return this.abjNative.isNative();
    }

    get isStandalone() {
        return isDisplayStandalone();
    }

    get isIOS() {
        return isIOS();
    }

    get installUrl() {
        return INSTALL_URL;
    }

    get appModeLabel() {
        const info = this.state.appInfo;
        if (this.isNative) {
            return info?.appVersion ? _t("Android app %s (%s)", info.appVersion, info.appBuild || "?") : _t("Android app");
        }
        return this.isStandalone ? _t("Installed web app") : _t("Web browser");
    }

    /** Notification state of this device for the settings badge: {label, badge, hint} */
    get notificationState() {
        if (this.isNative) {
            const push = this.nativePush;
            if (!push.available) {
                return { label: _t("Not available"), badge: "none", hint: _t("This build of the app has no notification service (Firebase is not configured).") };
            }
            if (push.permission === "denied") {
                return { label: _t("Blocked"), badge: "behind", hint: _t("Notifications are blocked for ABJ. Allow them in the app settings.") };
            }
            if (push.blocked) {
                return { label: _t("Turned off"), badge: "behind", hint: _t("A manager turned off notifications for this phone. Ask them to unarchive it in ABJ App Devices.") };
            }
            if (push.permission === "granted") {
                return push.registered
                    ? { label: _t("On"), badge: "verified", hint: "" }
                    : { label: _t("Registering"), badge: "upcoming", hint: push.error };
            }
            return { label: _t("Off"), badge: "upcoming", hint: "" };
        }
        switch (this.state.push.status) {
            case "granted":
                return { label: _t("On"), badge: "verified", hint: "" };
            case "denied":
                return {
                    label: _t("Blocked"),
                    badge: "behind",
                    hint: this.isIOS && this.isStandalone
                        ? _t("Notifications are blocked for ABJ. Allow them in the iPhone Settings > Notifications > ABJ.")
                        : _t("Notifications are blocked for this site. Allow them in the browser settings (site settings > Notifications)."),
                };
            case "ios-install-first":
                return { label: _t("Install first"), badge: "at_risk", hint: _t("Install ABJ on your Home Screen first: tap \"Install page\" below, then on that page tap Share > Add to Home Screen (iOS 16.4 or later), and open ABJ from the Home Screen.") };
            case "unsupported":
                return { label: _t("Not supported"), badge: "none", hint: _t("This browser cannot receive notifications.") };
            default:
                return { label: _t("Off"), badge: "upcoming", hint: "" };
        }
    }

    /** False when the server says it cannot send to this kind of device yet (unknown: true). */
    get serverPushReady() {
        const push = this.app.push;
        if (!push) {
            return true;
        }
        return this.isNative ? push.native_ready !== false : push.web_push_ready !== false;
    }

    get canEnableWebPush() {
        return !this.isNative && webPushSupported() && ["prompt", "granted"].includes(this.state.push.status);
    }

    get canEnableNativePush() {
        const push = this.nativePush;
        return this.isNative && push.available && push.permission !== "granted";
    }

    get showPushBanner() {
        return !this.isNative && this.state.push.status === "prompt" && !this.state.push.bannerDismissed && webPushSupported();
    }

    /** The test notification only makes sense where this phone can receive notifications. */
    get canTestPush() {
        if (this.isNative) {
            return this.nativePush.available;
        }
        return !["ios-install-first", "unsupported"].includes(this.state.push.status);
    }

    /** Done visits whose follow-up date is today (older servers do not send them). */
    get followups() {
        return this.dashboard?.followups_today || [];
    }

    get transports() {
        return this.printer.availableTransports();
    }

    get currentTransport() {
        return this.printer.getTransport(this.state.printer.transport);
    }

    get printerReady() {
        return this.printer.isConfigured(this.state.printer);
    }

    get paperWidths() {
        return PAPER_WIDTHS;
    }

    get copiesOptions() {
        return [1, 2, 3];
    }

    formatDistance(meters) {
        return formatDistance(meters);
    }

    /** Clamped, rounded percentage for progress bars (templates cannot use globals such as Math). */
    pct(value) {
        return Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
    }

    formatCoord(value) {
        return formatCoord(value);
    }

    saleOrders(visit) {
        return (visit && visit.sale_orders) || [];
    }

    /** False for a teammate's visit (manager opening a team notification): read-only. */
    isMine(visit) {
        return Boolean(visit) && visit.is_mine !== false;
    }

    isPrinting(visitId, orderId = false) {
        return this.state.printing === `${visitId}-${orderId || 0}`;
    }

    // ------------------------------------------------------------------
    // Data
    // ------------------------------------------------------------------
    emptyCheckin() {
        return {
            step: "location",
            partner: null,
            type_id: null,
            purpose: "",
            notes: "",
            photo: null,
            photoPreview: "",
            planned_id: null,
            newCustomer: { open: false, name: "", phone: "", street: "", city: "" },
        };
    }

    async loadDashboard() {
        try {
            this.state.dashboard = await this.orm.call("checkinme.checkin", "mobile_get_dashboard", []);
            if (!this.state.checkin.type_id) {
                const types = this.state.dashboard.types;
                const preferred = types.find((t) => t.counts_as_visit) || types[0];
                this.state.checkin.type_id = preferred ? preferred.id : null;
            }
        } catch (error) {
            this.handleError(error);
        } finally {
            this.state.loading = false;
        }
    }

    handleError(error, title = _t("ABJ")) {
        const message =
            (error && error.data && error.data.message) || (error && error.message) || _t("Something went wrong.");
        this.notification.add(message, { type: "danger", title });
        if (error instanceof PrintError || EXPECTED_ERRORS.includes(error?.exceptionName)) {
            // expected (printer off, deleted visit, no access...): already explained to the user
            console.warn(error);
        } else {
            console.error(error);
        }
    }

    /**
     * Keep the URL in sync with the open visit (/odoo/abj/<id>), so a reload or a shared link
     * reopens it. No-op when the action service does not provide updateActionState.
     */
    setUrlVisit(id) {
        const resId = id || undefined;
        if (resId === this.urlVisitId) {
            return;
        }
        this.urlVisitId = resId;
        try {
            this.props.updateActionState?.({ resId });
        } catch (error) {
            console.warn("Sale Check: URL not updated", error);
        }
    }

    // ------------------------------------------------------------------
    // GPS
    // ------------------------------------------------------------------
    async captureGps() {
        const geo = this.state.geo;
        if (!browser.navigator.geolocation) {
            geo.status = "error";
            geo.error = _t("Geolocation is not supported by this device.");
            return false;
        }
        geo.status = "locating";
        geo.error = "";
        geo.denied = false;
        try {
            const position = await new Promise((resolve, reject) =>
                browser.navigator.geolocation.getCurrentPosition(resolve, reject, GEO_OPTIONS)
            );
            geo.latitude = position.coords.latitude;
            geo.longitude = position.coords.longitude;
            geo.accuracy = Number.isFinite(position.coords.accuracy) ? Math.round(position.coords.accuracy) : 0;
            geo.status = "ok";
            return true;
        } catch (error) {
            geo.status = "error";
            geo.denied = Boolean(error && error.code === 1);
            geo.error = geo.denied
                ? this.locationDeniedMessage()
                : _t("Your position could not be captured. Move to an open area and try again.");
            return false;
        }
    }

    /** Where to allow a denied location permission: the Android app and an iPhone have no site settings. */
    locationDeniedMessage() {
        if (this.isNative) {
            return _t("Location access was denied. Tap \"Open app settings\", allow Location for ABJ, then try again.");
        }
        if (this.isIOS) {
            return _t("Location access was denied. Allow it in the iPhone Settings > Privacy & Security > Location Services, then try again.");
        }
        return _t("Location access was denied. Allow location for this site in the browser settings (site settings > Location), then try again.");
    }

    geoPayload() {
        if (!this.hasPosition) {
            return {};
        }
        return {
            latitude: this.state.geo.latitude,
            longitude: this.state.geo.longitude,
            accuracy: this.state.geo.accuracy,
        };
    }

    // ------------------------------------------------------------------
    // Check-in flow
    // ------------------------------------------------------------------
    /**
     * @param {object} [planned] mobile values of a planned visit (or prefilled values without id)
     * @param {object} [options]
     * @param {boolean} [options.waitGps=true] false: return without waiting for the GPS fix
     */
    async startCheckin(planned = null, { waitGps = true } = {}) {
        this.state.checkin = this.emptyCheckin();
        const types = this.dashboard?.types || [];
        const preferred = types.find((t) => t.counts_as_visit) || types[0];
        this.state.checkin.type_id = preferred ? preferred.id : null;
        if (planned) {
            this.state.checkin.planned_id = planned.id;
            this.state.checkin.type_id = planned.type_id || this.state.checkin.type_id;
            this.state.checkin.purpose = planned.purpose || "";
            this.state.checkin.notes = planned.notes || "";
            if (planned.partner_id) {
                this.state.checkin.partner = {
                    id: planned.partner_id,
                    name: planned.partner_name,
                    address: planned.partner_address,
                    phone: planned.partner_phone,
                    distance_m: null,
                };
            }
        }
        this.setUrlVisit(planned && planned.id);
        this.state.customerQuery = "";
        this.state.customers = [];
        this.state.view = "checkin";
        this.state.checkin.step = "location";
        const located = this.captureGps().then((ok) => ok && this.continueFromLocation());
        if (waitGps) {
            await located;
        }
    }

    /** "Follow-ups today": a new check-in at the customer of an earlier visit. */
    startFollowUp(visit) {
        return this.startCheckin({ ...visit, id: false, purpose: "", notes: "" });
    }

    continueFromLocation() {
        if (this.requireGps && !this.hasPosition) {
            this.notification.add(_t("A GPS position is required to check in."), { type: "warning" });
            return;
        }
        if (this.state.checkin.partner) {
            this.state.checkin.step = "details";
        } else {
            this.state.checkin.step = "customer";
            this.loadCustomers();
        }
    }

    goToStep(step) {
        this.state.checkin.step = step;
        if (step === "customer" && !this.state.customers.length) {
            this.loadCustomers();
        }
    }

    async loadCustomers() {
        this.state.searching = true;
        try {
            const geo = this.hasPosition ? this.state.geo : {};
            this.state.customers = await this.orm.call("checkinme.checkin", "mobile_search_customers", [
                this.state.customerQuery,
                geo.latitude || null,
                geo.longitude || null,
            ]);
        } catch (error) {
            this.handleError(error);
        } finally {
            this.state.searching = false;
        }
    }

    onCustomerInput(ev) {
        this.state.customerQuery = ev.target.value;
        browser.clearTimeout(this.searchTimer);
        this.searchTimer = browser.setTimeout(() => this.loadCustomers(), SEARCH_DEBOUNCE_MS);
    }

    selectCustomer(customer) {
        this.state.checkin.partner = customer;
        this.state.checkin.step = "details";
    }

    skipCustomer() {
        this.state.checkin.partner = null;
        this.state.checkin.step = "details";
    }

    toggleNewCustomer() {
        const form = this.state.checkin.newCustomer;
        form.open = !form.open;
        if (form.open && this.state.customerQuery && !form.name) {
            form.name = this.state.customerQuery;
        }
    }

    async createCustomer() {
        const form = this.state.checkin.newCustomer;
        if (!form.name.trim()) {
            this.notification.add(_t("Please enter the customer name."), { type: "warning" });
            return;
        }
        this.state.busy = true;
        try {
            const geo = this.hasPosition ? this.state.geo : {};
            const customer = await this.orm.call("checkinme.checkin", "mobile_create_customer", [
                form.name,
                form.phone,
                form.street,
                form.city,
                geo.latitude || null,
                geo.longitude || null,
            ]);
            this.state.checkin.newCustomer = { open: false, name: "", phone: "", street: "", city: "" };
            this.selectCustomer(customer);
        } catch (error) {
            this.handleError(error);
        } finally {
            this.state.busy = false;
        }
    }

    chooseType(type) {
        this.state.checkin.type_id = type.id;
    }

    async onPhotoChange(ev) {
        const file = ev.target.files && ev.target.files[0];
        if (!file) {
            return;
        }
        this.state.busy = true;
        try {
            const { base64, mimetype } = await compressPhoto(file);
            this.state.checkin.photo = base64;
            this.state.checkin.photoPreview = `data:${mimetype};base64,${base64}`;
        } catch (error) {
            this.handleError(error);
        } finally {
            this.state.busy = false;
        }
    }

    removePhoto() {
        this.state.checkin.photo = null;
        this.state.checkin.photoPreview = "";
    }

    async submitCheckin() {
        if (!this.canSubmitCheckin) {
            if (this.selectedType && this.selectedType.requires_customer && !this.state.checkin.partner) {
                this.notification.add(_t("Please select a customer for this activity type."), { type: "warning" });
            }
            return;
        }
        this.state.busy = true;
        try {
            const c = this.state.checkin;
            const vals = {
                partner_id: c.partner ? c.partner.id : false,
                type_id: c.type_id,
                purpose: c.purpose,
                notes: c.notes,
                photo: c.photo || false,
                planned_id: c.planned_id || false,
                ...this.geoPayload(),
            };
            this.state.result = await this.orm.call("checkinme.checkin", "mobile_checkin", [vals]);
            this.state.resultKind = "checkin";
            this.state.view = "result";
            this.setUrlVisit(this.state.result.id);
            this.loadDashboard();
        } catch (error) {
            this.handleError(error);
        } finally {
            this.state.busy = false;
        }
    }

    // ------------------------------------------------------------------
    // Check-out flow
    // ------------------------------------------------------------------
    /**
     * @param {object} visit mobile values of the open visit
     * @param {object} [options]
     * @param {boolean} [options.waitGps=true] false: return without waiting for the GPS fix
     */
    async startCheckout(visit, { waitGps = true } = {}) {
        this.state.checkout = {
            visit,
            outcome: visit.outcome || "",
            sale_amount: visit.sale_amount ? String(visit.sale_amount) : "",
            notes: visit.notes || "",
            next_action_date: visit.next_action_date || "",
        };
        this.state.view = "checkout";
        this.setUrlVisit(visit.id);
        const located = this.captureGps();
        if (waitGps) {
            await located;
        }
    }

    chooseOutcome(value) {
        this.state.checkout.outcome = this.state.checkout.outcome === value ? "" : value;
    }

    async submitCheckout() {
        const co = this.state.checkout;
        if (!co.visit || this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const vals = {
                outcome: co.outcome || false,
                notes: co.notes,
                sale_amount: co.sale_amount === "" ? "" : Number(co.sale_amount),
                next_action_date: co.next_action_date || false,
                ...this.geoPayload(),
            };
            this.state.result = await this.orm.call("checkinme.checkin", "mobile_checkout", [co.visit.id, vals]);
            this.state.resultKind = "checkout";
            this.state.view = "result";
            this.setUrlVisit(this.state.result.id);
            this.loadDashboard();
            if (this.state.printer.autoPrint && this.printerReady) {
                // never blocks nor fails the check-out: errors are only notified
                this.printSlip(this.state.result);
            }
        } catch (error) {
            this.handleError(error);
        } finally {
            this.state.busy = false;
        }
    }

    /**
     * Open a visit in the screen (deep link, notification, history, Home): check-out form for my
     * open visit, check-in for my planned visit, otherwise the read-only visit card.
     *
     * @param {number} id
     * @param {object} [options] passed to startCheckout / startCheckin
     */
    async openVisit(id, options = {}) {
        try {
            const visit = await this.orm.call("checkinme.checkin", "mobile_get_checkin", [id]);
            const mine = this.isMine(visit);
            if (mine && visit.state === "checked_in") {
                await this.startCheckout(visit, options);
            } else if (mine && visit.state === "draft") {
                await this.startCheckin(visit, options);
            } else {
                this.state.result = visit;
                this.state.resultKind = "view";
                this.state.view = "result";
                this.setUrlVisit(visit.id);
            }
        } catch (error) {
            if (this.urlVisitId && this.urlVisitId === id) {
                // bad deep link: do not keep it in the URL (each reload would fail again)
                this.setUrlVisit(false);
            }
            this.handleError(error);
        }
    }

    // ------------------------------------------------------------------
    // Navigation
    // ------------------------------------------------------------------
    async goHome() {
        this.state.view = "home";
        this.setUrlVisit(false);
        await this.loadDashboard();
    }

    async openHistory() {
        this.state.view = "history";
        this.setUrlVisit(false);
        try {
            this.state.history = await this.orm.call("checkinme.checkin", "mobile_get_history", []);
        } catch (error) {
            this.handleError(error);
        }
    }

    openSettings() {
        this.state.view = "settings";
        this.setUrlVisit(false);
        this.refreshPushStatus();
        if (!this.webPushPrefetch) {
            this.prefetchWebPush();
        }
        if (this.currentTransport?.picker === "list" && !this.state.printerUi.devices.length) {
            this.loadPrinterDevices();
        }
    }

    openRecord(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "checkinme.checkin",
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openFullApp() {
        this.action.doAction("checkinme_sales_activity.action_checkinme_checkin");
    }

    /** Leave the app screen for the complete Odoo web client. */
    openFullOdoo() {
        browser.location.assign("/odoo");
    }

    // ------------------------------------------------------------------
    // Notifications
    // ------------------------------------------------------------------
    refreshPushStatus() {
        this.state.push.status = webPushStatus();
    }

    /** Non-blocking: prepares the web push subscription so the button can subscribe immediately. */
    prefetchWebPush() {
        if (this.isNative || !["prompt", "granted"].includes(this.state.push.status)) {
            return;
        }
        prefetchWebPush(this.orm).then((prefetched) => {
            this.webPushPrefetch = prefetched;
        });
    }

    /**
     * Click handler: must start the subscription synchronously (no await before), the permission
     * prompt is only allowed during the user gesture.
     */
    enableNotifications() {
        if (this.state.push.busy) {
            return;
        }
        const pending = enableWebPush(this.orm, this.webPushPrefetch);
        this.state.push.busy = true;
        pending
            .then((status) => {
                this.state.push.status = status === "unsupported" ? webPushStatus() : status;
                if (status === "granted") {
                    this.state.push.bannerDismissed = true;
                    this.notification.add(_t("Notifications are on for this device."), { type: "success", title: _t("Notifications") });
                } else if (status === "denied") {
                    this.notification.add(this.notificationState.hint, { type: "warning", title: _t("Notifications") });
                }
            })
            .catch((error) => this.handleError(error, _t("Notifications")))
            .finally(() => {
                this.state.push.busy = false;
            });
    }

    async enableNativeNotifications() {
        this.state.push.busy = true;
        try {
            await this.abjNative.requestPush();
        } finally {
            this.state.push.busy = false;
        }
    }

    openAppSettings() {
        this.abjNative.openAppSettings();
    }

    dismissPushBanner() {
        this.state.push.bannerDismissed = true;
        writeFlag(PUSH_BANNER_KEY);
    }

    async sendTestPush() {
        // the server reports whether *this* phone was reached (not another device of the user)
        const target = this.isNative ? { token: this.abjNative.pushToken() } : { endpoint: readPushEndpoint() };
        if (!target.token && !target.endpoint) {
            this.notification.add(_t("This phone is not registered for notifications yet. Turn notifications on first."), {
                type: "warning",
                title: _t("Notifications"),
            });
            return;
        }
        this.state.push.testing = true;
        try {
            const result = (await this.orm.call("checkinme.checkin", "mobile_send_test_push", [], target)) || {};
            const sent = (Number(result.web) || 0) + (Number(result.native) || 0);
            // this_device is null/absent when the server cannot tell: fall back to the device count
            const reached = result.this_device ?? sent > 0;
            this.notification.add(
                result.message || (reached ? _t("Test notification sent.") : _t("This phone is not registered for notifications yet.")),
                { type: reached ? "success" : "warning", title: _t("Notifications") }
            );
        } catch (error) {
            this.handleError(error, _t("Notifications"));
        } finally {
            this.state.push.testing = false;
        }
    }

    /**
     * Settings > Log out: stop the notifications of this phone first (FCM token in the Android
     * app, web push endpoint in a browser), else the next user of a shared phone would get them.
     */
    async logOut() {
        if (this.state.loggingOut) {
            return;
        }
        this.state.loggingOut = true;
        try {
            if (this.isNative) {
                await this.abjNative.unregisterPush();
            } else {
                await unregisterWebPush(this.orm);
            }
        } catch (error) {
            console.warn("ABJ: notifications of this phone not unregistered", error);
        }
        browser.location.assign(LOGOUT_URL);
    }

    // ------------------------------------------------------------------
    // Printing
    // ------------------------------------------------------------------
    updatePrinter(values) {
        this.state.printer = savePrinterSettings({ ...this.state.printer, ...values });
    }

    setPrinterOption(key, value) {
        this.updatePrinter({ [key]: value });
    }

    chooseTransport(id) {
        if (id === this.state.printer.transport) {
            return;
        }
        this.updatePrinter({ transport: id, deviceId: "", deviceName: "" });
        this.state.printerUi.devices = [];
        this.state.printerUi.preview = "";
        if (this.currentTransport?.picker === "list") {
            this.loadPrinterDevices();
        }
    }

    async loadPrinterDevices() {
        const transport = this.currentTransport;
        if (!transport?.listDevices) {
            return;
        }
        this.state.printerUi.loadingDevices = true;
        try {
            this.state.printerUi.devices = await transport.listDevices();
        } catch (error) {
            this.handleError(error, _t("Printer"));
        } finally {
            this.state.printerUi.loadingDevices = false;
        }
    }

    pickPrinterDevice(device) {
        this.updatePrinter({ deviceId: device.id, deviceName: device.name });
    }

    /**
     * Click handler: the device chooser of the browser needs the user gesture, so the transport's
     * select() is called synchronously.
     */
    choosePrinter(all = false) {
        const transport = this.currentTransport;
        if (!transport || this.state.printerUi.busy) {
            return;
        }
        let pending;
        try {
            pending = Promise.resolve(transport.select(this.state.printer, { all }));
        } catch (error) {
            pending = Promise.reject(error);
        }
        this.state.printerUi.busy = true;
        pending
            .then((device) => {
                if (device) {
                    this.updatePrinter({ deviceId: device.id, deviceName: device.name });
                } else if (transport.picker === "list") {
                    this.loadPrinterDevices();
                }
            })
            .catch((error) => this.handleError(error, _t("Printer")))
            .finally(() => {
                this.state.printerUi.busy = false;
            });
    }

    async printTestPage() {
        if (this.state.printerUi.busy) {
            return;
        }
        this.state.printerUi.busy = true;
        try {
            const { canvas } = await this.printer.testPage(this.state.printer, { company: this.app.company?.name || "" });
            this.state.printerUi.preview = canvas.toDataURL("image/png");
        } catch (error) {
            this.handleError(error, _t("Printer"));
        } finally {
            this.state.printerUi.busy = false;
        }
    }

    /**
     * Print the visit slip, or the receipt of one of its sales orders.
     *
     * @param {object} visit mobile values of the check-in
     * @param {object} [order] one of visit.sale_orders
     */
    async printSlip(visit, order = null) {
        if (!this.printerReady) {
            this.openSettings();
            return;
        }
        if (this.state.printing) {
            return;
        }
        this.state.printing = `${visit.id}-${order ? order.id : 0}`;
        try {
            const doc = await this.orm.call(
                "checkinme.checkin",
                "mobile_get_receipt",
                [visit.id],
                order ? { kind: "order", order_id: order.id } : { kind: "visit" }
            );
            await this.printer.printDocument(doc, this.state.printer);
        } catch (error) {
            this.handleError(error, _t("Printer"));
        } finally {
            this.state.printing = "";
        }
    }
}

registry.category("lazy_components").add("checkinme_sales_activity.Screen", CheckinmeScreen);
