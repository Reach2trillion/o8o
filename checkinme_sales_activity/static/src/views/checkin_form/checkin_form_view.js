/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { FormController } from "@web/views/form/form_controller";
import { formView } from "@web/views/form/form_view";

const GEOLOCATION_OPTIONS = { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 };

// Buttons that must capture the device position right before executing.
const GEO_ACTIONS = {
    action_check_in: { lat: "latitude", lng: "longitude", acc: "accuracy" },
    action_check_out: { lat: "checkout_latitude", lng: "checkout_longitude", acc: "checkout_accuracy" },
};

/**
 * Check-in form: the "Check In" (planned visit) and "Check Out" buttons capture the GPS position
 * at the moment they are pressed, so the stored coordinates reflect where the salesperson really
 * was when the event happened (the server still enforces the GPS requirement for check-ins).
 */
export class CheckinmeCheckinFormController extends FormController {
    setup() {
        super.setup();
        this.notification = useService("notification");
    }

    async beforeExecuteActionButton(clickParams) {
        const mapping = GEO_ACTIONS[clickParams.name];
        if (mapping && clickParams.type === "object") {
            await this.captureGeolocation(mapping);
        }
        return super.beforeExecuteActionButton(clickParams);
    }

    async captureGeolocation(mapping) {
        const record = this.model.root;
        if (!navigator.geolocation) {
            this.notification.add(_t("Geolocation is not supported by this browser/device."), {
                type: "warning",
                title: _t("GPS unavailable"),
            });
            return;
        }
        let position;
        try {
            position = await new Promise((resolve, reject) =>
                navigator.geolocation.getCurrentPosition(resolve, reject, GEOLOCATION_OPTIONS)
            );
        } catch (error) {
            const denied = error && error.code === 1;
            this.notification.add(
                denied
                    ? _t("Location access was denied. Allow location access for this site and try again.")
                    : _t("Your GPS position could not be captured (%s).", (error && error.message) || _t("unknown error")),
                { type: "warning", title: _t("GPS capture failed") }
            );
            return;
        }
        const changes = {};
        const fields = record.fields || {};
        if (mapping.lat in fields) {
            changes[mapping.lat] = position.coords.latitude;
        }
        if (mapping.lng in fields) {
            changes[mapping.lng] = position.coords.longitude;
        }
        if (mapping.acc in fields) {
            changes[mapping.acc] = Number.isFinite(position.coords.accuracy) ? position.coords.accuracy : 0;
        }
        if (Object.keys(changes).length) {
            await record.update(changes);
        }
    }
}

export const checkinmeCheckinFormView = {
    ...formView,
    Controller: CheckinmeCheckinFormController,
};

registry.category("views").add("checkinme_checkin_form", checkinmeCheckinFormView);
