/**
 * Capacitor configuration of the ABJ Android app.
 *
 * The app is a thin native shell: its WebView loads the Sale Check phone screen straight from the
 * Odoo server (`server.url` + `server.appStartPath`), so server updates reach the phones without a
 * new APK. The native plugin `AbjNative` (printing) and FCM push are the only things it adds.
 *
 * Build-time environment variables (read by `npx cap sync`, which writes
 * android/app/src/main/assets/capacitor.config.json; the Gradle build reads the same variables):
 *   ABJ_SERVER_URL   origin of the Odoo server, e.g. https://erp.abjskincare.com (no path, no trailing slash)
 *   ABJ_APP_ID       Android applicationId (default com.abj.app)
 *   ABJ_APP_VERSION  version name, also sent in the user agent (default: "version" of package.json)
 */
import type { CapacitorConfig } from '@capacitor/cli';
import { readFileSync } from 'fs';
import { join } from 'path';

const PLACEHOLDER_SERVER_URL = 'https://erp.example.com';
const DEFAULT_APP_ID = 'com.abj.app';
const APP_ID_RE = /^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$/;
const VERSION_RE = /^[0-9A-Za-z][0-9A-Za-z.+_-]*$/;

/** Return the origin (scheme://host[:port]) of ABJ_SERVER_URL, or throw when it carries anything else. */
function serverOrigin(raw: string | undefined): string {
    const value = (raw || '').trim().replace(/\/+$/, '');
    if (!value) {
        console.warn(`[ABJ] ABJ_SERVER_URL is not set, using the placeholder ${PLACEHOLDER_SERVER_URL}`);
        return PLACEHOLDER_SERVER_URL;
    }
    let url: URL;
    try {
        url = new URL(value);
    } catch {
        throw new Error(`ABJ_SERVER_URL is not a valid URL: "${raw}"`);
    }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') {
        throw new Error(`ABJ_SERVER_URL must start with https:// (got "${raw}")`);
    }
    if (url.username || url.password || url.search || url.hash || url.pathname !== '/') {
        throw new Error(
            `ABJ_SERVER_URL must be the server origin only, e.g. https://erp.example.com ` +
                `(no path, query, fragment or credentials; got "${raw}")`,
        );
    }
    return `${url.protocol}//${url.host}`;
}

function appVersion(): string {
    const fromEnv = (process.env.ABJ_APP_VERSION || '').trim();
    const version = fromEnv || JSON.parse(readFileSync(join(__dirname, 'package.json'), 'utf8')).version;
    if (!VERSION_RE.test(version)) {
        throw new Error(`ABJ_APP_VERSION is not a valid version name: "${version}"`);
    }
    return version;
}

const appId = (process.env.ABJ_APP_ID || '').trim() || DEFAULT_APP_ID;
if (!APP_ID_RE.test(appId)) {
    throw new Error(`ABJ_APP_ID is not a valid Android application id: "${appId}"`);
}
const serverUrl = serverOrigin(process.env.ABJ_SERVER_URL);
if (serverUrl.startsWith('http:')) {
    console.warn('[ABJ] ABJ_SERVER_URL uses plain http: only for local testing, push and login cookies need https');
}

const config: CapacitorConfig = {
    appId,
    appName: 'ABJ',
    webDir: 'www',
    backgroundColor: '#22343a',
    server: {
        url: serverUrl,
        // Client action path of the Sale Check phone screen (ir.actions.client path "abj").
        appStartPath: '/odoo/abj',
        cleartext: serverUrl.startsWith('http:'),
    },
    android: {
        // Never "OdooMobile": that token switches the Odoo web client into its own mobile-app mode.
        appendUserAgent: `ABJApp/${appVersion()}`,
        backgroundColor: '#22343a',
        allowMixedContent: false,
    },
    plugins: {
        PushNotifications: {
            presentationOptions: ['badge', 'sound', 'alert'],
        },
        // Light status/navigation bar icons on the slate window background (styles.xml).
        SystemBars: {
            style: 'DARK',
        },
    },
};

export default config;
