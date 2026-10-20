/** @odoo-module **/

import { browser } from "@web/core/browser/browser";
import { _t } from "@web/core/l10n/translation";
import { chunk, toBase64 } from "@checkinme_sales_activity/screen/printing/escpos";

/**
 * Ways to reach a receipt printer from the phone. Every transport has the same interface:
 *   id, label, help
 *   picker        "none" | "list" (in-app device list) | "chooser" (browser device chooser) | "address" (host / port)
 *   needsBytes    false when the transport prints the receipt image itself (system print)
 *   available()   true when usable on this device / browser
 *   select(settings, {all})   (user gesture) let the user pick a device -> {id, name} | null when cancelled
 *   print(bytes, settings, {canvas, doc})
 * Errors are raised as PrintError with a message for the salesperson.
 */

// Serial Port Profile (Bluetooth Classic printers)
export const SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb";
// GATT services of common BLE thermal printers
export const BLE_PRINTER_SERVICES = [
    "000018f0-0000-1000-8000-00805f9b34fb",
    "49535343-fe7d-4ae5-8fa9-9fafd205e455",
    "0000ff00-0000-1000-8000-00805f9b34fb",
    "0000ffe0-0000-1000-8000-00805f9b34fb",
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
];
const SERIAL_CHUNK = 512;
const BLE_CHUNK = 100;
const BLE_CHUNK_NO_RESPONSE = 20;
const PRINT_AREA_CLASS = "o_abj_print_area";
const PRINTING_BODY_CLASS = "o_abj_printing";
const DOTS_PER_MM = 8; // 203 dpi thermal heads

export class PrintError extends Error {
    constructor(message, code = "PRINT_FAILED") {
        super(message);
        this.name = "PrintError";
        this.code = code;
    }
}

/** Friendly message for the error codes of the AbjNative plugin. */
function nativeErrorMessage(error) {
    switch (error?.code) {
        case "NO_BLUETOOTH":
            return _t("This phone has no Bluetooth.");
        case "BLUETOOTH_OFF":
            return _t("Bluetooth is turned off. Turn it on and try again.");
        case "PERMISSION_DENIED":
            return _t("ABJ is not allowed to use Bluetooth. Allow the \"Nearby devices\" permission in the app settings.");
        case "CONNECT_FAILED":
            return _t("Could not connect to the printer. Check that it is switched on, paired with this phone and not connected to another phone.");
        case "WRITE_FAILED":
            return _t("The connection to the printer was lost while printing.");
        default:
            return error?.message || _t("The printer did not respond.");
    }
}

async function callNative(promiseFactory) {
    try {
        return (await promiseFactory()) || {};
    } catch (error) {
        throw new PrintError(nativeErrorMessage(error), error?.code || "NATIVE_ERROR");
    }
}

class Transport {
    static id = "";

    /**
     * @param {object|null} native the ``abj_native`` service
     */
    constructor(native) {
        this.native = native || null;
    }
    get id() {
        return this.constructor.id;
    }
    get picker() {
        return "chooser";
    }
    get needsBytes() {
        return true;
    }
    get nativePlugin() {
        return this.native?.isNative() ? this.native.plugin("AbjNative") : null;
    }
    available() {
        return false;
    }
    async select() {
        return null;
    }
}

// ----------------------------------------------------------------------
// Android app (native plugin AbjNative)
// ----------------------------------------------------------------------

export class NativeBluetoothTransport extends Transport {
    static id = "native_bt";

    get label() {
        return _t("Bluetooth printer");
    }
    get help() {
        return _t("A printer paired in the phone's Bluetooth settings.");
    }
    get picker() {
        return "list";
    }
    available() {
        return Boolean(this.nativePlugin);
    }
    /** Paired devices, likely printers first. */
    async listDevices() {
        const plugin = this.nativePlugin;
        const { granted } = await callNative(() => plugin.requestBluetoothPermission());
        if (!granted) {
            throw new PrintError(nativeErrorMessage({ code: "PERMISSION_DENIED" }), "PERMISSION_DENIED");
        }
        const { devices } = await callNative(() => plugin.listPrinters());
        return (devices || [])
            .map((device) => ({
                id: device.address,
                name: device.name || device.address,
                likelyPrinter: Boolean(device.likelyPrinter),
            }))
            .sort((a, b) => Number(b.likelyPrinter) - Number(a.likelyPrinter) || a.name.localeCompare(b.name));
    }
    async select() {
        const printers = (await this.listDevices()).filter((device) => device.likelyPrinter);
        return printers.length === 1 ? printers[0] : null;
    }
    async print(bytes, settings) {
        if (!settings.deviceId) {
            throw new PrintError(_t("Choose a Bluetooth printer first (Settings > Printer)."), "NO_PRINTER");
        }
        const plugin = this.nativePlugin;
        await callNative(() => plugin.printBluetooth({ address: settings.deviceId, data: toBase64(bytes) }));
    }
}

export class NativeTcpTransport extends Transport {
    static id = "native_tcp";

    get label() {
        return _t("Network printer");
    }
    get help() {
        return _t("An ESC/POS printer on the same Wi-Fi network (port 9100).");
    }
    get picker() {
        return "address";
    }
    available() {
        return Boolean(this.nativePlugin);
    }
    async print(bytes, settings) {
        const host = (settings.host || "").trim();
        if (!host) {
            throw new PrintError(_t("Enter the IP address of the network printer (Settings > Printer)."), "NO_PRINTER");
        }
        const plugin = this.nativePlugin;
        await callNative(() => plugin.printTcp({ host, port: Number(settings.port) || 9100, data: toBase64(bytes) }));
    }
}

// ----------------------------------------------------------------------
// Chrome: Web Serial (Bluetooth Classic SPP on Android 138+, USB on desktop)
// ----------------------------------------------------------------------

function serialPortName(info) {
    if (info.bluetoothServiceClassId) {
        return _t("Bluetooth serial printer");
    }
    if (info.usbVendorId) {
        const hex = (value) => Number(value || 0).toString(16).padStart(4, "0");
        return _t("USB printer %s:%s", hex(info.usbVendorId), hex(info.usbProductId));
    }
    return _t("Serial printer");
}

export class WebSerialTransport extends Transport {
    static id = "web_serial";

    get label() {
        return _t("Bluetooth / USB (Chrome)");
    }
    get help() {
        return _t("Classic Bluetooth printers paired with the phone, or USB printers, through Chrome.");
    }
    available() {
        return Boolean(browser.navigator.serial) && !this.native?.isNative();
    }
    async select(settings, { all = false } = {}) {
        const serial = browser.navigator.serial;
        const request = async (options) => {
            try {
                return await serial.requestPort(options);
            } catch (error) {
                if (error?.name === "NotFoundError") {
                    return null; // chooser cancelled or no matching device
                }
                throw error;
            }
        };
        let port;
        try {
            port = await request(
                all ? {} : { filters: [{ bluetoothServiceClassId: SPP_UUID }], allowedBluetoothServiceClassIds: [SPP_UUID] }
            );
        } catch (error) {
            if (all || error?.name !== "TypeError") {
                throw error;
            }
            // browser without Bluetooth filters: plain chooser
            port = await request({});
        }
        if (!port) {
            return null;
        }
        const ports = await serial.getPorts();
        const info = port.getInfo ? port.getInfo() : {};
        return { id: JSON.stringify({ index: ports.indexOf(port), info }), name: serialPortName(info) };
    }
    async findPort(settings) {
        const ports = await browser.navigator.serial.getPorts();
        let saved = {};
        try {
            saved = JSON.parse(settings.deviceId || "{}");
        } catch {
            // invalid stored value: match by position only
        }
        const key = JSON.stringify(saved.info || {});
        const matches = (port) => JSON.stringify(port.getInfo ? port.getInfo() : {}) === key;
        const indexed = ports[saved.index];
        const port = (indexed && matches(indexed) && indexed) || ports.find(matches) || (ports.length === 1 && ports[0]);
        if (!port) {
            throw new PrintError(_t("The printer was not found. Choose it again (Settings > Printer)."), "NO_PRINTER");
        }
        return port;
    }
    async print(bytes, settings) {
        const port = await this.findPort(settings);
        try {
            await port.open({ baudRate: 9600 });
        } catch (error) {
            if (error?.name !== "InvalidStateError") {
                // InvalidStateError: already open
                throw new PrintError(_t("Could not connect to the printer. Check that it is switched on and paired."), "CONNECT_FAILED");
            }
        }
        const writer = port.writable.getWriter();
        try {
            for (const part of chunk(bytes, SERIAL_CHUNK)) {
                await writer.write(part);
            }
            await writer.close(); // flushes
        } finally {
            writer.releaseLock();
            try {
                await port.close();
            } catch (error) {
                console.warn("ABJ printer: serial port not closed", error);
            }
        }
    }
}

// ----------------------------------------------------------------------
// Chrome: Web Bluetooth (BLE printers)
// ----------------------------------------------------------------------

// BluetoothDevice objects of this page session (getDevices() is not available everywhere)
const bleDevices = new Map();

export class WebBluetoothTransport extends Transport {
    static id = "web_bluetooth";

    get label() {
        return _t("Bluetooth LE (Chrome)");
    }
    get help() {
        return _t("Bluetooth Low Energy thermal printers, through Chrome.");
    }
    available() {
        return Boolean(browser.navigator.bluetooth) && !this.native?.isNative();
    }
    async select(settings, { all = false } = {}) {
        let device;
        try {
            device = await browser.navigator.bluetooth.requestDevice(
                all
                    ? { acceptAllDevices: true, optionalServices: BLE_PRINTER_SERVICES }
                    : {
                          filters: BLE_PRINTER_SERVICES.map((service) => ({ services: [service] })),
                          optionalServices: BLE_PRINTER_SERVICES,
                      }
            );
        } catch (error) {
            if (error?.name === "NotFoundError") {
                return null;
            }
            throw error;
        }
        bleDevices.set(device.id, device);
        return { id: device.id, name: device.name || _t("Bluetooth printer") };
    }
    async findDevice(settings) {
        let device = bleDevices.get(settings.deviceId);
        if (!device && browser.navigator.bluetooth.getDevices) {
            const devices = await browser.navigator.bluetooth.getDevices();
            device = devices.find((d) => d.id === settings.deviceId);
        }
        if (!device) {
            throw new PrintError(
                _t("Choose the Bluetooth printer again (Settings > Printer): the browser forgets it when it restarts."),
                "RESELECT"
            );
        }
        bleDevices.set(device.id, device);
        return device;
    }
    async findCharacteristic(server) {
        for (const uuid of BLE_PRINTER_SERVICES) {
            let characteristics;
            try {
                const service = await server.getPrimaryService(uuid);
                characteristics = await service.getCharacteristics();
            } catch {
                continue; // service not on this device
            }
            const writable = characteristics.find((c) => c.properties.write || c.properties.writeWithoutResponse);
            if (writable) {
                return writable;
            }
        }
        throw new PrintError(_t("This Bluetooth device does not look like a printer."), "NO_SERVICE");
    }
    async print(bytes, settings) {
        const device = await this.findDevice(settings);
        let server;
        try {
            server = device.gatt.connected ? device.gatt : await device.gatt.connect();
        } catch {
            throw new PrintError(_t("Could not connect to the printer. Check that it is switched on and nearby."), "CONNECT_FAILED");
        }
        try {
            const characteristic = await this.findCharacteristic(server);
            const withResponse = Boolean(characteristic.properties.write);
            const parts = chunk(bytes, withResponse ? BLE_CHUNK : BLE_CHUNK_NO_RESPONSE);
            for (const part of parts) {
                if (!withResponse) {
                    await characteristic.writeValueWithoutResponse(part);
                } else if (characteristic.writeValueWithResponse) {
                    await characteristic.writeValueWithResponse(part);
                } else {
                    await characteristic.writeValue(part);
                }
            }
            if (!withResponse) {
                // let the radio drain its queue before disconnecting
                await new Promise((resolve) => browser.setTimeout(resolve, 500));
            }
        } finally {
            try {
                device.gatt.disconnect();
            } catch {
                // already disconnected
            }
        }
    }
}

// ----------------------------------------------------------------------
// System print dialog (AirPrint, Android print services, PDF)
// ----------------------------------------------------------------------

/** Remove the hidden print container (when the screen is closed). */
export function removePrintArea() {
    document.querySelector(`.${PRINT_AREA_CLASS}`)?.remove();
    document.body.classList.remove(PRINTING_BODY_CLASS);
}

/**
 * @param {HTMLCanvasElement} canvas
 * @param {object} doc
 * @param {object} [options]
 * @param {boolean} [options.clearAfterPrint=true] false: keep the print class until the next
 *      print or removePrintArea() (Android's PrintManager renders the page after printPage()
 *      returned, and again for each preview change, each render firing afterprint)
 */
async function showPrintArea(canvas, doc, { clearAfterPrint = true } = {}) {
    let area = document.querySelector(`.${PRINT_AREA_CLASS}`);
    if (!area) {
        area = document.createElement("div");
        area.className = PRINT_AREA_CLASS;
        document.body.appendChild(area);
    }
    const img = document.createElement("img");
    img.alt = doc?.title || doc?.name || "";
    img.style.width = `${canvas.width / DOTS_PER_MM}mm`;
    img.src = canvas.toDataURL("image/png");
    area.replaceChildren(img);
    try {
        await img.decode();
    } catch {
        // decode() unsupported: the data URL is synchronous enough for printing
    }
    // print stylesheet (screen.scss): while the class is set, only the receipt is printed
    // (it has no effect on screen, so keeping it set is harmless)
    document.body.classList.add(PRINTING_BODY_CLASS);
    if (clearAfterPrint) {
        browser.addEventListener("afterprint", () => document.body.classList.remove(PRINTING_BODY_CLASS), { once: true });
    }
}

/** Last resort when the browser cannot print (window.print missing / failing): open the image. */
async function openReceiptImage(canvas) {
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    const url = blob ? URL.createObjectURL(blob) : canvas.toDataURL("image/png");
    const win = browser.open(url, "_blank");
    if (!win) {
        throw new PrintError(_t("This browser cannot print. Allow pop-ups to open the receipt image instead."), "NO_PRINT");
    }
    if (blob) {
        browser.setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
}

export class SystemPrintTransport extends Transport {
    static id = "system";

    get label() {
        return _t("System print");
    }
    get help() {
        return _t("The phone's print dialog: AirPrint, Android print services or Save as PDF.");
    }
    get picker() {
        return "none";
    }
    get needsBytes() {
        return false;
    }
    available() {
        return true;
    }
    async print(bytes, settings, { canvas, doc } = {}) {
        const plugin = this.nativePlugin;
        await showPrintArea(canvas, doc, { clearAfterPrint: !plugin });
        if (plugin) {
            // Android app: the WebView has no window.print(), use Android's PrintManager
            await callNative(() => plugin.printPage({ jobName: doc?.name || "ABJ" }));
            return;
        }
        try {
            if (typeof window.print !== "function") {
                throw new Error("window.print is not available");
            }
            window.print();
        } catch (error) {
            console.warn("ABJ printer: window.print failed, opening the receipt image", error);
            document.body.classList.remove(PRINTING_BODY_CLASS);
            await openReceiptImage(canvas);
        }
    }
}

export const TRANSPORT_CLASSES = [
    NativeBluetoothTransport,
    NativeTcpTransport,
    WebSerialTransport,
    WebBluetoothTransport,
    SystemPrintTransport,
];

/**
 * @param {object|null} native the ``abj_native`` service
 * @returns {Transport[]}
 */
export function createTransports(native) {
    return TRANSPORT_CLASSES.map((TransportClass) => new TransportClass(native));
}
