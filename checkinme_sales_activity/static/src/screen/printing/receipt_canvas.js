/** @odoo-module **/

import { packBits, rgbaToGray } from "@checkinme_sales_activity/screen/printing/escpos";

/**
 * Receipt renderer: draws a receipt document (contract of ``mobile_get_receipt``) on a canvas
 * as wide as the printer head, then thresholds it into a 1-bit bitmap for ESC/POS raster
 * printing. Text goes through the browser's shaping engine, so Khmer prints correctly.
 *
 * Document: {title, name, blocks: [...]} with blocks
 *   {type: "text", text, align: left|center|right, size: s|m|l, bold}
 *   {type: "pair", left, right, bold}            label left, value right
 *   {type: "item", name, detail, amount}         name line, then detail left / amount right
 *   {type: "total", left, right}                 bold, larger
 *   {type: "separator"}                          dashed line
 *   {type: "qr", value, size}                    QR code image from /report/barcode (skipped on error)
 *   {type: "feed", lines}                        blank lines
 *   {type: "ruler"}                              (client only) printable width ruler, for test pages
 * Unknown block types are ignored.
 */

export const KHMER_FAMILY = "ABJ Khmer";
export const FONT_STACK = `"${KHMER_FAMILY}", "Noto Sans Khmer", "Khmer OS", sans-serif`;
const FONT_BASE_URL = "/checkinme_sales_activity/static/src/fonts/";
const FONT_FILES = {
    400: "noto-sans-khmer-khmer-400-normal.woff2",
    700: "noto-sans-khmer-khmer-700-normal.woff2",
};
// The bundled files are the Khmer subset of Noto Sans Khmer: Latin falls back to the next fonts
const KHMER_UNICODE_RANGE = "U+1780-17FF, U+19E0-19FF, U+200C-200D, U+25CC";

const BASE_WIDTH = 384; // 58 mm paper (48 mm printable at 203 dpi)
const SIZES = { s: 20, m: 24, l: 32 };
const LINE_HEIGHT = 1.7;
const MARGIN = 4;
const COENG = "\u17D2";

let fontsPromise = null;

/**
 * Register the bundled Khmer font as "ABJ Khmer" (once). Resolves to false (and the receipt uses
 * the system fonts of the stack) when the font cannot be loaded.
 *
 * @returns {Promise<boolean>}
 */
export function loadReceiptFonts() {
    if (!fontsPromise) {
        fontsPromise = (async () => {
            if (typeof FontFace === "undefined" || !document.fonts) {
                return false;
            }
            try {
                const faces = await Promise.all(
                    Object.entries(FONT_FILES).map(([weight, file]) =>
                        new FontFace(KHMER_FAMILY, `url(${FONT_BASE_URL}${file}) format("woff2")`, {
                            weight,
                            style: "normal",
                            unicodeRange: KHMER_UNICODE_RANGE,
                        }).load()
                    )
                );
                faces.forEach((face) => document.fonts.add(face));
                await Promise.all([
                    document.fonts.load(`400 24px "${KHMER_FAMILY}"`, "\u1780"),
                    document.fonts.load(`700 24px "${KHMER_FAMILY}"`, "\u1780"),
                ]);
                return true;
            } catch (error) {
                console.warn("ABJ receipt: Khmer font not loaded, using system fonts", error);
                fontsPromise = null; // retry on the next receipt
                return false;
            }
        })();
    }
    return fontsPromise;
}

/**
 * @param {string} value
 * @param {number} size dots
 * @returns {string}
 */
export function qrImageUrl(value, size) {
    const params = new URLSearchParams({ barcode_type: "QR", value, width: size, height: size });
    return `/report/barcode/?${params.toString()}`;
}

function loadImage(src) {
    return new Promise((resolve) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () => resolve(null);
        img.src = src;
    });
}

// ----------------------------------------------------------------------
// Text wrapping
// ----------------------------------------------------------------------

function makeSegmenter(locale, granularity) {
    try {
        return typeof Intl !== "undefined" && Intl.Segmenter ? new Intl.Segmenter(locale || undefined, { granularity }) : null;
    } catch {
        return null;
    }
}

/**
 * Split a string into user-perceived characters. Without Intl.Segmenter, combining marks and
 * Khmer subscript consonants (coeng + consonant) stay attached to their base.
 */
function graphemes(text, locale) {
    const segmenter = makeSegmenter(locale, "grapheme");
    if (segmenter) {
        return Array.from(segmenter.segment(text), (s) => s.segment);
    }
    const clusters = [];
    let previous = "";
    for (const char of text) {
        if (clusters.length && (/\p{M}/u.test(char) || previous === COENG)) {
            clusters[clusters.length - 1] += char;
        } else {
            clusters.push(char);
        }
        previous = char;
    }
    return clusters;
}

const OPENING_PUNCTUATION = /^[([{\u00AB\u201C\u2018"']+$/;
const CLOSING_PUNCTUATION = /^[)\]}\u00BB\u201D\u2019"'.,;:!?%\u17D4\u17D5\u17D6]+\s*$/;

/**
 * No line break after an opening bracket or before closing punctuation / the Khmer full stop:
 * glue those segments to their neighbour.
 */
function gluePunctuation(segments) {
    const pieces = [];
    let pending = "";
    for (const segment of segments) {
        if (OPENING_PUNCTUATION.test(segment)) {
            pending += segment;
        } else if (CLOSING_PUNCTUATION.test(segment) && pieces.length && !pending && !/\s$/.test(pieces[pieces.length - 1])) {
            pieces[pieces.length - 1] += segment;
        } else {
            pieces.push(pending + segment);
            pending = "";
        }
    }
    if (pending) {
        pieces.push(pending);
    }
    return pieces;
}

/** Word-level pieces (with their trailing spaces) that a line may break between. */
function words(text, locale) {
    const segmenter = makeSegmenter(locale, "word");
    if (segmenter) {
        return gluePunctuation(Array.from(segmenter.segment(text), (s) => s.segment));
    }
    // split on spaces / zero width spaces, each separator stays attached to the word before it
    const parts = text.split(/([\s\u200B]+)/);
    const pieces = [];
    for (let i = 0; i < parts.length; i += 2) {
        const piece = parts[i] + (parts[i + 1] || "");
        if (piece) {
            pieces.push(piece);
        }
    }
    return pieces;
}

/**
 * Greedy word wrap measured with the canvas (shaping-aware): Khmer has no spaces between words,
 * so break opportunities come from the word segmenter; a word wider than the line is broken
 * between grapheme clusters.
 *
 * @param {CanvasRenderingContext2D} ctx with the font already set
 * @param {string} text
 * @param {number} maxWidth
 * @param {string} [locale]
 * @returns {string[]}
 */
export function wrapText(ctx, text, maxWidth, locale) {
    const lines = [];
    const fits = (value) => ctx.measureText(value.trimEnd()).width <= maxWidth;
    for (const paragraph of String(text ?? "").split(/\r?\n/)) {
        let line = "";
        for (const word of words(paragraph, locale)) {
            if (fits(line + word)) {
                line += word;
                continue;
            }
            if (line.trim()) {
                lines.push(line.trimEnd());
            }
            line = line.trim() ? word.trimStart() : (line + word).trimStart();
            if (!fits(line)) {
                // a single word wider than the line: break it between grapheme clusters
                let part = "";
                for (const cluster of graphemes(line, locale)) {
                    if (part && !fits(part + cluster)) {
                        lines.push(part.trimEnd());
                        part = cluster.trimStart();
                    } else {
                        part += cluster;
                    }
                }
                line = part;
            }
        }
        lines.push(line.trimEnd());
    }
    return lines;
}

// ----------------------------------------------------------------------
// Layout
// ----------------------------------------------------------------------

/**
 * Lay the document out as a list of draw operations with absolute positions.
 * Pure geometry: ``ctx`` is only used to measure text.
 */
function layout(ctx, doc, { width, scale, fontFamily, locale, images }) {
    const ops = [];
    const left = Math.round(MARGIN * scale);
    const right = width - left;
    const contentWidth = right - left;
    const gap = Math.round(12 * scale);
    let y = Math.round(6 * scale);

    const font = (size, bold) => `${bold ? 700 : 400} ${Math.round((SIZES[size] || SIZES.m) * scale)}px ${fontFamily}`;
    const lineHeight = (size) => Math.round((SIZES[size] || SIZES.m) * scale * LINE_HEIGHT);
    const measure = (text, fontSpec) => {
        ctx.font = fontSpec;
        return ctx.measureText(text).width;
    };
    const wrap = (text, fontSpec, maxWidth) => {
        ctx.font = fontSpec;
        return wrapText(ctx, text, maxWidth, locale);
    };
    const textLines = (text, { size = "m", bold = false, align = "left" } = {}) => {
        const fontSpec = font(size, bold);
        const height = lineHeight(size);
        for (const line of wrap(text, fontSpec, contentWidth)) {
            const x = align === "center" ? width / 2 : align === "right" ? right : left;
            ops.push({ kind: "text", text: line, x, y: y + height / 2, font: fontSpec, align });
            y += height;
        }
    };
    /** Label on the left, value on the right; both wrap when they do not fit side by side. */
    const pair = (leftText, rightText, { size = "m", bold = false } = {}) => {
        const fontSpec = font(size, bold);
        const height = lineHeight(size);
        leftText = String(leftText ?? "");
        rightText = String(rightText ?? "");
        const rightWidth = measure(rightText, fontSpec);
        if (rightWidth <= contentWidth * 0.6) {
            const leftLines = wrap(leftText, fontSpec, contentWidth - rightWidth - gap);
            leftLines.forEach((line, index) => {
                ops.push({ kind: "text", text: line, x: left, y: y + height / 2, font: fontSpec, align: "left" });
                if (index === 0 && rightText) {
                    ops.push({ kind: "text", text: rightText, x: right, y: y + height / 2, font: fontSpec, align: "right" });
                }
                y += height;
            });
            return;
        }
        // long value: label on its own line(s), value right-aligned below
        if (leftText) {
            textLines(leftText, { size, bold, align: "left" });
        }
        textLines(rightText, { size, bold, align: "right" });
    };

    for (const block of doc.blocks || []) {
        switch (block.type) {
            case "text":
                textLines(block.text, { size: block.size || "m", bold: Boolean(block.bold), align: block.align || "left" });
                break;
            case "pair":
                pair(block.left, block.right, { bold: Boolean(block.bold) });
                break;
            case "item":
                textLines(block.name, { size: "m", bold: false });
                if (block.detail || block.amount) {
                    pair(block.detail, block.amount, { size: "m" });
                }
                break;
            case "total":
                pair(block.left, block.right, { size: "l", bold: true });
                break;
            case "separator": {
                const height = Math.round(lineHeight("m") * 0.6);
                ops.push({ kind: "dash", x1: left, x2: right, y: y + height / 2, lineWidth: Math.max(1, Math.round(2 * scale)), scale });
                y += height;
                break;
            }
            case "qr": {
                const image = images.get(block);
                if (!image) {
                    break;
                }
                const size = Math.min(contentWidth, Math.round((Number(block.size) || 180) * scale));
                const pad = Math.round(8 * scale);
                ops.push({ kind: "image", image, x: Math.round((width - size) / 2), y: y + pad, size });
                y += size + 2 * pad;
                break;
            }
            case "feed":
                y += lineHeight("m") * Math.max(0, Math.min(20, Number(block.lines) || 1));
                break;
            case "ruler": {
                const tick = Math.round(10 * scale);
                ops.push({ kind: "ruler", x1: 0, x2: width, y, tick, scale });
                y += tick * 2 + Math.round(4 * scale);
                textLines(`${width} dots`, { size: "s", align: "center" });
                break;
            }
            default:
                break;
        }
    }
    y += Math.round(6 * scale);
    return { ops, height: Math.max(1, Math.ceil(y)) };
}

function draw(ctx, ops) {
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#000";
    ctx.strokeStyle = "#000";
    for (const op of ops) {
        if (op.kind === "text") {
            ctx.font = op.font;
            ctx.textAlign = op.align;
            ctx.fillText(op.text, op.x, op.y);
        } else if (op.kind === "dash") {
            ctx.save();
            ctx.lineWidth = op.lineWidth;
            ctx.setLineDash([Math.round(8 * op.scale), Math.round(5 * op.scale)]);
            ctx.beginPath();
            ctx.moveTo(op.x1, Math.round(op.y) + 0.5);
            ctx.lineTo(op.x2, Math.round(op.y) + 0.5);
            ctx.stroke();
            ctx.restore();
        } else if (op.kind === "image") {
            ctx.save();
            ctx.imageSmoothingEnabled = false;
            ctx.drawImage(op.image, op.x, op.y, op.size, op.size);
            ctx.restore();
        } else if (op.kind === "ruler") {
            // full-width bar, a tick every 8 dots (one byte), a long tick every 64 dots
            const bar = Math.max(2, Math.round(3 * op.scale));
            ctx.fillRect(op.x1, op.y, op.x2 - op.x1, bar);
            for (let x = 0; x < op.x2; x += 8) {
                const long = x % 64 === 0;
                ctx.fillRect(x, op.y + bar, 1, long ? op.tick * 2 : op.tick);
            }
            ctx.fillRect(op.x2 - 1, op.y + bar, 1, op.tick * 2);
        }
    }
}

/**
 * Render a receipt document.
 *
 * @param {{title?: string, name?: string, blocks: object[]}} doc
 * @param {{widthDots?: number, fontFamily?: string, locale?: string, threshold?: number}} [options]
 * @returns {Promise<{canvas: HTMLCanvasElement, bitmap: {width: number, height: number, data: Uint8Array}}>}
 */
export async function renderReceipt(doc, { widthDots = BASE_WIDTH, fontFamily = FONT_STACK, locale, threshold = 160 } = {}) {
    const width = Math.ceil(Math.max(64, Number(widthDots) || BASE_WIDTH) / 8) * 8;
    const scale = width / BASE_WIDTH;
    await loadReceiptFonts();

    const images = new Map();
    await Promise.all(
        (doc.blocks || [])
            .filter((block) => block.type === "qr" && block.value)
            .map(async (block) => {
                const size = Math.round((Number(block.size) || 180) * scale);
                const image = await loadImage(qrImageUrl(String(block.value), size));
                if (image) {
                    images.set(block, image);
                }
            })
    );

    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const { ops, height } = layout(ctx, doc, { width, scale, fontFamily, locale, images });

    canvas.height = height; // resets the context state
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, width, height);
    draw(ctx, ops);

    const gray = rgbaToGray(ctx.getImageData(0, 0, width, height).data);
    return { canvas, bitmap: packBits(gray, width, height, threshold) };
}
