/** @odoo-module **/

import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { user } from "@web/core/user";
import { concatBytes, encodeRaster } from "@checkinme_sales_activity/screen/printing/escpos";
import { renderReceipt } from "@checkinme_sales_activity/screen/printing/receipt_canvas";
import { createTransports, PrintError } from "@checkinme_sales_activity/screen/printing/transports";

/**
 * Receipt printing for the phone screen: printer settings of this device (localStorage) and
 * the print pipeline  document -> canvas / 1-bit bitmap -> ESC/POS bytes -> transport.
 */

export const SETTINGS_KEY = "abj.printer.v1";
// printable width in dots of 203 dpi heads
export const PAPER_WIDTHS = [
    { mm: 58, dots: 384 },
    { mm: 80, dots: 576 },
];
export const DEFAULT_SETTINGS = Object.freeze({
    transport: "",
    deviceId: "",
    deviceName: "",
    host: "",
    port: 9100,
    widthDots: 384,
    cut: false,
    autoPrint: false,
    copies: 1,
});
const MAX_COPIES = 5;
const FEED_LINES = 4;

function sanitizeSettings(values) {
    const settings = { ...DEFAULT_SETTINGS };
    for (const key of Object.keys(DEFAULT_SETTINGS)) {
        if (values && values[key] !== undefined && values[key] !== null) {
            settings[key] = values[key];
        }
    }
    settings.transport = String(settings.transport || "");
    settings.deviceId = String(settings.deviceId || "");
    settings.deviceName = String(settings.deviceName || "");
    settings.host = String(settings.host || "").trim();
    settings.port = Math.min(65535, Math.max(1, parseInt(settings.port, 10) || 9100));
    settings.widthDots = PAPER_WIDTHS.some((p) => p.dots === Number(settings.widthDots)) ? Number(settings.widthDots) : 384;
    settings.cut = Boolean(settings.cut);
    settings.autoPrint = Boolean(settings.autoPrint);
    settings.copies = Math.min(MAX_COPIES, Math.max(1, parseInt(settings.copies, 10) || 1));
    return settings;
}

/** Printer settings of this device (defaults when nothing is stored or storage is unavailable). */
export function loadPrinterSettings() {
    try {
        const raw = browser.localStorage.getItem(SETTINGS_KEY);
        return sanitizeSettings(raw ? JSON.parse(raw) : {});
    } catch (error) {
        console.warn("ABJ printer: settings not readable", error);
        return { ...DEFAULT_SETTINGS };
    }
}

export function savePrinterSettings(settings) {
    const clean = sanitizeSettings(settings);
    try {
        browser.localStorage.setItem(SETTINGS_KEY, JSON.stringify(clean));
    } catch (error) {
        console.warn("ABJ printer: settings not saved", error);
    }
    return clean;
}

/**
 * Test page: Khmer + Latin text, every block type and a ruler showing the printable width.
 *
 * @param {object} settings
 * @param {{company?: string, transportLabel?: string}} [info]
 */
export function testDocument(settings, { company = "", transportLabel = "" } = {}) {
    const paper = PAPER_WIDTHS.find((p) => p.dots === settings.widthDots) || PAPER_WIDTHS[0];
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    const stamp = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`;
    return {
        title: _t("TEST PAGE"),
        name: _t("ABJ test page"),
        blocks: [
            { type: "text", text: company || "ABJ", align: "center", size: "l", bold: true },
            { type: "text", text: "សួស្តី ABJ", align: "center", size: "l" },
            { type: "text", text: _t("Printer test page"), align: "center", size: "s" },
            { type: "separator" },
            { type: "pair", left: _t("Paper"), right: `${paper.mm} mm · ${paper.dots} dots` },
            { type: "pair", left: _t("Connection"), right: transportLabel },
            { type: "pair", left: _t("Printer"), right: settings.deviceName || settings.host || "-" },
            { type: "pair", left: "អតិថិជន", right: "ផ្សារ ABC", bold: true },
            { type: "text", text: "អរគុណសម្រាប់ការទិញទំនិញរបស់អ្នក សូមអញ្ជើញមកម្តងទៀត។ Thank you, see you again!" },
            { type: "separator" },
            { type: "item", name: "Serum 30ml", detail: "2 × $12.00", amount: "$24.00" },
            { type: "total", left: _t("Total"), right: "$24.00" },
            { type: "ruler" },
            { type: "text", text: stamp, align: "center", size: "s" },
            { type: "feed", lines: 1 },
        ],
    };
}

export class ReceiptPrinter {
    /**
     * @param {{native?: object}} [options] native: the ``abj_native`` service
     */
    constructor({ native = null } = {}) {
        this.transports = createTransports(native);
    }

    /** Transports usable on this device, in display order. */
    availableTransports() {
        return this.transports.filter((transport) => transport.available());
    }

    getTransport(id) {
        return this.availableTransports().find((transport) => transport.id === id) || null;
    }

    /** True when printing can start without visiting the settings first. */
    isConfigured(settings) {
        const transport = settings && this.getTransport(settings.transport);
        if (!transport) {
            return false;
        }
        switch (transport.picker) {
            case "none":
                return true;
            case "address":
                return Boolean(settings.host);
            default:
                return Boolean(settings.deviceId);
        }
    }

    /**
     * Render and print a receipt document (contract of ``mobile_get_receipt``).
     *
     * @returns {Promise<{canvas: HTMLCanvasElement, bytes: Uint8Array|null}>}
     */
    async printDocument(doc, settings) {
        const transport = this.getTransport(settings.transport);
        if (!transport) {
            throw new PrintError(_t("No printer is set up on this device."), "NO_PRINTER");
        }
        const { canvas, bitmap } = await renderReceipt(doc, { widthDots: settings.widthDots, locale: user.lang });
        let bytes = null;
        if (transport.needsBytes) {
            const job = encodeRaster(bitmap, { cut: settings.cut, feedLines: FEED_LINES });
            bytes = concatBytes(new Array(settings.copies || 1).fill(job));
        }
        await transport.print(bytes, settings, { canvas, doc });
        return { canvas, bytes };
    }

    async testPage(settings, { company = "" } = {}) {
        const transport = this.getTransport(settings.transport);
        const doc = testDocument(settings, { company, transportLabel: transport ? String(transport.label) : "" });
        return this.printDocument(doc, settings);
    }
}
