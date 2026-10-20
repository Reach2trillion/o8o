/** @odoo-module **/

/**
 * ESC/POS raster encoding for 58 / 80 mm thermal receipt printers.
 *
 * ESC/POS text mode cannot shape Khmer, so receipts are rendered to a 1-bit bitmap
 * (see receipt_canvas.js) and sent as "GS v 0" raster bands. Pure functions, no DOM.
 *
 * Bitmap format used across the printing modules:
 *   { width, height, data: Uint8Array }  one byte per pixel, 1 = black, row-major,
 *   width a multiple of 8 (padding columns are white).
 */

const ESC = 0x1b;
const GS = 0x1d;
// "GS v 0" accepts up to 65535 rows, but cheap printers have small buffers: send bands of rows
export const MAX_BAND_ROWS = 255;

/**
 * Pack one bitmap into raster bytes: ceil(width / 8) bytes per row, MSB = leftmost pixel, 1 = black.
 *
 * @param {{width: number, height: number, data: Uint8Array}} bitmap
 * @param {number} [fromRow]
 * @param {number} [toRow] exclusive
 * @returns {Uint8Array}
 */
export function packRows(bitmap, fromRow = 0, toRow = bitmap.height) {
    const { width, data } = bitmap;
    const bytesPerRow = Math.ceil(width / 8);
    const out = new Uint8Array(bytesPerRow * (toRow - fromRow));
    let o = 0;
    for (let y = fromRow; y < toRow; y++) {
        const rowStart = y * width;
        for (let bx = 0; bx < bytesPerRow; bx++) {
            let byte = 0;
            for (let bit = 0; bit < 8; bit++) {
                const x = bx * 8 + bit;
                if (x < width && data[rowStart + x]) {
                    byte |= 0x80 >> bit;
                }
            }
            out[o++] = byte;
        }
    }
    return out;
}

/**
 * Encode a bitmap as a complete ESC/POS print job:
 *   ESC @                                 initialise
 *   GS v 0 m xL xH yL yH d1...dk          one per band of <= 255 rows (m = 0: normal density)
 *   ESC d n                               feed n lines
 *   GS V 66 0                             partial cut with feed (only when ``cut``)
 *
 * @param {{width: number, height: number, data: Uint8Array}} bitmap
 * @param {{cut?: boolean, feedLines?: number}} [options]
 * @returns {Uint8Array}
 */
export function encodeRaster(bitmap, { cut = false, feedLines = 4 } = {}) {
    const bytesPerRow = Math.ceil(bitmap.width / 8);
    const parts = [Uint8Array.of(ESC, 0x40)];
    for (let top = 0; top < bitmap.height; top += MAX_BAND_ROWS) {
        const rows = Math.min(MAX_BAND_ROWS, bitmap.height - top);
        parts.push(
            Uint8Array.of(GS, 0x76, 0x30, 0x00, bytesPerRow & 0xff, (bytesPerRow >> 8) & 0xff, rows & 0xff, (rows >> 8) & 0xff),
            packRows(bitmap, top, top + rows)
        );
    }
    const feed = Math.max(0, Math.min(255, Math.round(Number(feedLines) || 0)));
    parts.push(Uint8Array.of(ESC, 0x64, feed));
    if (cut) {
        parts.push(Uint8Array.of(GS, 0x56, 0x42, 0x00));
    }
    return concatBytes(parts);
}

/**
 * @param {Uint8Array[]} parts
 * @returns {Uint8Array}
 */
export function concatBytes(parts) {
    const out = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
    let offset = 0;
    for (const part of parts) {
        out.set(part, offset);
        offset += part.length;
    }
    return out;
}

/**
 * Split bytes into views of at most ``size`` bytes (no copy).
 *
 * @param {Uint8Array} bytes
 * @param {number} size
 * @returns {Uint8Array[]}
 */
export function chunk(bytes, size) {
    const step = Math.max(1, Math.floor(size));
    const chunks = [];
    for (let i = 0; i < bytes.length; i += step) {
        chunks.push(bytes.subarray(i, i + step));
    }
    return chunks;
}

/**
 * Standard base64 (with padding) of a byte array, for the native plugin bridge.
 *
 * @param {Uint8Array} bytes
 * @returns {string}
 */
export function toBase64(bytes) {
    let binary = "";
    // String.fromCharCode.apply has an argument count limit: convert by slices
    for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
}

/**
 * Threshold a grayscale image (one luma byte per pixel, 0 = black) into a bitmap whose width is
 * padded to a multiple of 8 with white columns.
 *
 * @param {Uint8Array|Uint8ClampedArray|number[]} gray
 * @param {number} width
 * @param {number} height
 * @param {number} [threshold] pixels darker than this are printed
 * @returns {{width: number, height: number, data: Uint8Array}}
 */
export function packBits(gray, width, height, threshold = 160) {
    const paddedWidth = Math.ceil(width / 8) * 8;
    const data = new Uint8Array(paddedWidth * height);
    for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
            data[y * paddedWidth + x] = gray[y * width + x] < threshold ? 1 : 0;
        }
    }
    return { width: paddedWidth, height, data };
}

/**
 * Floyd-Steinberg error diffusion (for photos / logos): same output format as ``packBits``.
 *
 * @param {Uint8Array|Uint8ClampedArray|number[]} gray
 * @param {number} width
 * @param {number} height
 * @returns {{width: number, height: number, data: Uint8Array}}
 */
export function ditherFloydSteinberg(gray, width, height) {
    const paddedWidth = Math.ceil(width / 8) * 8;
    const data = new Uint8Array(paddedWidth * height);
    const buffer = Float32Array.from(gray);
    for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
            const i = y * width + x;
            const old = buffer[i];
            const black = old < 128;
            data[y * paddedWidth + x] = black ? 1 : 0;
            const error = old - (black ? 0 : 255);
            if (x + 1 < width) {
                buffer[i + 1] += (error * 7) / 16;
            }
            if (y + 1 < height) {
                if (x > 0) {
                    buffer[i + width - 1] += (error * 3) / 16;
                }
                buffer[i + width] += (error * 5) / 16;
                if (x + 1 < width) {
                    buffer[i + width + 1] += error / 16;
                }
            }
        }
    }
    return { width: paddedWidth, height, data };
}

/**
 * Luma (0..255) of RGBA pixels composited over white, e.g. from ``ctx.getImageData().data``.
 *
 * @param {Uint8ClampedArray} rgba
 * @returns {Uint8Array}
 */
export function rgbaToGray(rgba) {
    const gray = new Uint8Array(rgba.length / 4);
    for (let i = 0, p = 0; p < gray.length; i += 4, p++) {
        const alpha = rgba[i + 3] / 255;
        const luma = 0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2];
        gray[p] = Math.round(luma * alpha + 255 * (1 - alpha));
    }
    return gray;
}
