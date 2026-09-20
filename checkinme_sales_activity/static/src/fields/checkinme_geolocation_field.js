/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { user } from "@web/core/user";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const GOOGLE_MAPS_URL = "https://www.google.com/maps?q=";
const GEOLOCATION_OPTIONS = { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 };

// navigator.geolocation error codes (GeolocationPositionError)
const PERMISSION_DENIED = 1;
const POSITION_UNAVAILABLE = 2;
const TIMEOUT = 3;

/**
 * Format a WGS84 coordinate with up to 7 decimals (the precision of the
 * ``digits=(10, 7)`` latitude / longitude fields), trimming trailing zeros.
 *
 * @param {number|string|boolean} value
 * @returns {string}
 */
function formatCoordinate(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) {
        return "";
    }
    return num.toFixed(7).replace(/\.?0+$/, "");
}

/**
 * GPS geolocation widget bound to a latitude float field.
 *
 * Options (view side):
 *   longitude_field      (required) name of the float field receiving the longitude
 *   accuracy_field       (optional) name of the float field receiving the accuracy (metres)
 *   auto_capture         (optional, default false) capture the position automatically
 *                        once when the widget is mounted, when there is no position yet
 *                        and the record is new, or is 'checked_in' and belongs to the
 *                        current user (record.user_id)
 *   auto_capture_on_new  (optional, default true) allow the automatic capture on new
 *                        (unsaved) records; set it to False on a check-out widget so a
 *                        brand new check-in does not also capture a check-out position
 */
export class CheckinmeGeolocationField extends Component {
    static template = "checkinme_sales_activity.GeolocationField";
    static props = {
        ...standardFieldProps,
        longitudeField: { type: String },
        accuracyField: { type: String, optional: true },
        autoCapture: { type: Boolean, optional: true },
        autoCaptureOnNew: { type: Boolean, optional: true },
    };
    static defaultProps = {
        autoCapture: false,
        autoCaptureOnNew: true,
    };

    setup() {
        this.notification = useService("notification");
        this.state = useState({ loading: false, error: "" });
        this.autoCaptureDone = false;
        this.isUnmounted = false;

        onMounted(() => {
            try {
                if (this.shouldAutoCapture()) {
                    this.autoCaptureDone = true;
                    this.capture();
                }
            } catch (error) {
                // Never let the widget break the form.
                console.warn("CheckinMe geolocation: automatic capture skipped", error);
            }
        });
        onWillUnmount(() => {
            this.isUnmounted = true;
        });
    }

    // ------------------------------------------------------------------
    // Getters
    // ------------------------------------------------------------------

    get latitude() {
        return this.props.record.data[this.props.name];
    }

    get longitude() {
        return this.props.longitudeField ? this.props.record.data[this.props.longitudeField] : undefined;
    }

    get accuracy() {
        return this.props.accuracyField ? this.props.record.data[this.props.accuracyField] : undefined;
    }

    get hasPosition() {
        return Boolean(this.latitude || this.longitude);
    }

    /** "11.5563738, 104.9282099" */
    get formattedPosition() {
        if (!this.hasPosition) {
            return "";
        }
        return `${formatCoordinate(this.latitude || 0)}, ${formatCoordinate(this.longitude || 0)}`;
    }

    /** "±12 m" or "" when unknown */
    get formattedAccuracy() {
        const accuracy = Number(this.accuracy);
        if (!this.hasPosition || !Number.isFinite(accuracy) || accuracy <= 0) {
            return "";
        }
        return `±${Math.round(accuracy)} m`;
    }

    get googleMapsUrl() {
        if (!this.hasPosition) {
            return "";
        }
        return `${GOOGLE_MAPS_URL}${formatCoordinate(this.latitude || 0)},${formatCoordinate(this.longitude || 0)}`;
    }

    // ------------------------------------------------------------------
    // Behaviour
    // ------------------------------------------------------------------

    /**
     * Automatic capture happens once, only when:
     *  - auto_capture is enabled and the field is editable,
     *  - no position has been captured yet,
     *  - the record is new (and auto_capture_on_new is not disabled) OR the record
     *    is 'checked_in' and belongs to the current user (user_id many2one).
     */
    shouldAutoCapture() {
        if (!this.props.autoCapture || this.props.readonly || this.autoCaptureDone || this.hasPosition) {
            return false;
        }
        const record = this.props.record;
        if (record.isNew) {
            return this.props.autoCaptureOnNew;
        }
        const data = record.data || {};
        const ownerId = Array.isArray(data.user_id) ? data.user_id[0] : false;
        return data.state === "checked_in" && Boolean(ownerId) && ownerId === user.userId;
    }

    async capture() {
        if (this.state.loading) {
            return;
        }
        this.state.error = "";
        if (!navigator.geolocation) {
            this.state.error = _t("Geolocation is not supported by this browser/device.");
            this.notify(this.state.error, _t("GPS unavailable"));
            return;
        }
        this.state.loading = true;
        try {
            const position = await new Promise((resolve, reject) => {
                navigator.geolocation.getCurrentPosition(resolve, reject, GEOLOCATION_OPTIONS);
            });
            if (this.isUnmounted) {
                return;
            }
            const coords = position.coords;
            const changes = { [this.props.name]: coords.latitude };
            if (this.props.longitudeField) {
                changes[this.props.longitudeField] = coords.longitude;
            }
            if (this.props.accuracyField) {
                changes[this.props.accuracyField] = Number.isFinite(coords.accuracy) ? coords.accuracy : 0;
            }
            await this.props.record.update(changes);
        } catch (error) {
            this.onCaptureError(error);
        } finally {
            if (!this.isUnmounted) {
                this.state.loading = false;
            }
        }
    }

    onCaptureError(error) {
        const code = error && typeof error.code === "number" ? error.code : 0;
        let message;
        switch (code) {
            case PERMISSION_DENIED:
                message = _t(
                    "Location access was denied. Please allow location access for this site in your browser or phone settings, then tap Capture GPS again."
                );
                break;
            case POSITION_UNAVAILABLE:
                message = _t(
                    "Your position is currently unavailable. Make sure GPS / location services are switched on and try again."
                );
                break;
            case TIMEOUT:
                message = _t(
                    "Getting your position timed out. Move to an open area with a better GPS signal and try again."
                );
                break;
            default:
                message = _t("The GPS position could not be captured. Please try again.");
                console.warn("CheckinMe geolocation: capture failed", error);
        }
        if (!this.isUnmounted) {
            this.state.error = message;
        }
        this.notify(message, _t("GPS capture failed"));
    }

    notify(message, title) {
        try {
            this.notification.add(message, { type: "warning", title });
        } catch (error) {
            console.warn("CheckinMe geolocation: could not display notification", error);
        }
    }
}

export const checkinmeGeolocationField = {
    component: CheckinmeGeolocationField,
    displayName: _t("GPS Geolocation"),
    supportedTypes: ["float"],
    supportedOptions: [
        { label: _t("Longitude field"), name: "longitude_field", type: "field" },
        { label: _t("Accuracy field"), name: "accuracy_field", type: "field" },
        { label: _t("Capture automatically"), name: "auto_capture", type: "boolean" },
        { label: _t("Capture automatically on new records"), name: "auto_capture_on_new", type: "boolean" },
    ],
    // Make sure the companion fields are loaded and writable even when the view
    // does not declare them explicitly (readonly changes are dropped on save).
    fieldDependencies: ({ options }) => {
        const dependencies = [];
        for (const fieldName of [options.longitude_field, options.accuracy_field]) {
            if (fieldName) {
                dependencies.push({ name: fieldName, type: "float", readonly: false });
            }
        }
        return dependencies;
    },
    extractProps: ({ options }) => ({
        longitudeField: options.longitude_field,
        accuracyField: options.accuracy_field,
        autoCapture: !!options.auto_capture,
        autoCaptureOnNew: options.auto_capture_on_new !== false,
    }),
};

registry.category("fields").add("checkinme_geolocation", checkinmeGeolocationField);
