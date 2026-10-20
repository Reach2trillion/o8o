/** @odoo-module **/

import { Component, onWillDestroy, onWillStart, useState } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const GEO_OPTIONS = { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 };
const SEARCH_DEBOUNCE_MS = 300;

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

function parseServerDatetime(value) {
    // "YYYY-MM-DD HH:MM:SS" in UTC
    return value ? new Date(value.replace(" ", "T") + "Z") : null;
}

/**
 * Check In phone screen: a single-page flow for field salespeople.
 * Home (today, open visit, targets) -> Check in (GPS -> customer -> details) -> Check out -> Result.
 * Every step calls a regular ORM method, so access rights, GPS verification and Telegram
 * notifications are handled by the server exactly as in the backend views.
 */
export class CheckinmeScreen extends Component {
    static template = "checkinme_sales_activity.Screen";
    static props = { "*": true };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = useState({
            view: "home",
            loading: true,
            busy: false,
            dashboard: null,
            geo: { status: "idle", latitude: 0, longitude: 0, accuracy: 0, error: "" },
            checkin: this.emptyCheckin(),
            checkout: { visit: null, outcome: "", sale_amount: "", notes: "", next_action_date: "" },
            result: null,
            resultKind: "checkin",
            history: [],
            customerQuery: "",
            customers: [],
            searching: false,
        });
        this.searchTimer = null;
        this.pendingOpenId = this.props.action?.context?.checkinme_open_id || null;

        onWillStart(async () => {
            await this.loadDashboard();
            if (this.pendingOpenId) {
                await this.openVisit(this.pendingOpenId);
            }
        });
        onWillDestroy(() => browser.clearTimeout(this.searchTimer));
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
                return _t("Done");
            case "history":
                return _t("My visits");
            default:
                return _t("Check In");
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

    handleError(error) {
        const message =
            (error && error.data && error.data.message) || (error && error.message) || _t("Something went wrong.");
        this.notification.add(message, { type: "danger", title: _t("Check In") });
        console.error(error);
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
            geo.error =
                error && error.code === 1
                    ? _t("Location access was denied. Allow location access for this site and try again.")
                    : _t("Your position could not be captured. Move to an open area and try again.");
            return false;
        }
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
    async startCheckin(planned = null) {
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
        this.state.customerQuery = "";
        this.state.customers = [];
        this.state.view = "checkin";
        this.state.checkin.step = "location";
        const ok = await this.captureGps();
        if (ok) {
            this.continueFromLocation();
        }
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
        try {
            const base64 = await readFileAsBase64(file);
            this.state.checkin.photo = base64;
            this.state.checkin.photoPreview = `data:${file.type || "image/jpeg"};base64,${base64}`;
        } catch (error) {
            this.handleError(error);
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
    async startCheckout(visit) {
        this.state.checkout = {
            visit,
            outcome: visit.outcome || "",
            sale_amount: visit.sale_amount ? String(visit.sale_amount) : "",
            notes: visit.notes || "",
            next_action_date: visit.next_action_date || "",
        };
        this.state.view = "checkout";
        await this.captureGps();
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
            this.loadDashboard();
        } catch (error) {
            this.handleError(error);
        } finally {
            this.state.busy = false;
        }
    }

    async openVisit(id) {
        try {
            const visit = await this.orm.call("checkinme.checkin", "mobile_get_checkin", [id]);
            if (visit.state === "checked_in") {
                await this.startCheckout(visit);
            } else if (visit.state === "draft") {
                await this.startCheckin(visit);
            } else {
                this.state.result = visit;
                this.state.resultKind = "view";
                this.state.view = "result";
            }
        } catch (error) {
            this.handleError(error);
        }
    }

    // ------------------------------------------------------------------
    // Navigation
    // ------------------------------------------------------------------
    async goHome() {
        this.state.view = "home";
        await this.loadDashboard();
    }

    async openHistory() {
        this.state.view = "history";
        try {
            this.state.history = await this.orm.call("checkinme.checkin", "mobile_get_history", []);
        } catch (error) {
            this.handleError(error);
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
}

registry.category("lazy_components").add("checkinme_sales_activity.Screen", CheckinmeScreen);
