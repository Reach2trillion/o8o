package com.abj.app;

import android.net.Uri;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeWebViewClient;
import com.getcapacitor.Logger;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/**
 * Capacitor's WebViewClient plus two things for a remote (Odoo) start URL:
 * - installs the download shim (AbjDownloads.SHIM_JS) in every page of the server;
 * - when the server cannot be reached, shows the bundled ABJ page (www/index.html) with a retry button
 *   instead of the WebView's raw error page. HTTP errors (Odoo 404/500 pages) are left alone.
 */
class AbjWebViewClient extends BridgeWebViewClient {

    private static final String TAG = "AbjWebViewClient";
    private static final String FALLBACK_ASSET = "public/index.html";
    private static final String FALLBACK_URL_TOKEN = "\"__ABJ_APP_URL__\"";

    private final Bridge bridge;
    private boolean fallbackShown;

    AbjWebViewClient(Bridge bridge) {
        super(bridge);
        this.bridge = bridge;
    }

    @Override
    public void onPageFinished(WebView view, String url) {
        super.onPageFinished(view, url);
        try {
            if (fallbackShown && (url == null || url.startsWith("about:") || url.startsWith("data:"))) {
                // Back from the offline page must not return to the failed load.
                view.clearHistory();
                fallbackShown = false;
            } else if (isServerUrl(url)) {
                view.evaluateJavascript(AbjDownloads.SHIM_JS, null);
            }
        } catch (Exception e) {
            Logger.error(TAG, "onPageFinished failed", e);
        }
    }

    @Override
    public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
        super.onReceivedError(view, request, error);
        try {
            if (request == null || error == null || !request.isForMainFrame() || !isConnectivityError(error.getErrorCode())) {
                return;
            }
            String failedUrl = request.getUrl() != null ? request.getUrl().toString() : null;
            if (isServerUrl(failedUrl)) {
                showFallback(view, failedUrl);
            }
        } catch (Exception e) {
            Logger.error(TAG, "Cannot show the offline page", e);
        }
    }

    private static boolean isConnectivityError(int code) {
        return (
            code == WebViewClient.ERROR_HOST_LOOKUP ||
            code == WebViewClient.ERROR_CONNECT ||
            code == WebViewClient.ERROR_TIMEOUT ||
            code == WebViewClient.ERROR_IO ||
            code == WebViewClient.ERROR_FAILED_SSL_HANDSHAKE
        );
    }

    /** True when the URL is on the configured Odoo server (same scheme and host:port). */
    private boolean isServerUrl(String url) {
        String serverUrl = bridge.getServerUrl();
        if (url == null || serverUrl == null) {
            return false;
        }
        Uri target = Uri.parse(url);
        Uri server = Uri.parse(serverUrl);
        return (
            target.getScheme() != null &&
            target.getScheme().equalsIgnoreCase(server.getScheme()) &&
            target.getEncodedAuthority() != null &&
            target.getEncodedAuthority().equalsIgnoreCase(server.getEncodedAuthority())
        );
    }

    private void showFallback(WebView view, String retryUrl) throws IOException {
        String html;
        try (InputStream in = view.getContext().getAssets().open(FALLBACK_ASSET)) {
            ByteArrayOutputStream buffer = new ByteArrayOutputStream();
            byte[] chunk = new byte[8192];
            int read;
            while ((read = in.read(chunk)) != -1) {
                buffer.write(chunk, 0, read);
            }
            html = buffer.toString(StandardCharsets.UTF_8.name());
        }
        html = html.replace(FALLBACK_URL_TOKEN, JSONObject.quote(retryUrl));
        fallbackShown = true;
        // No base URL: the page gets an opaque origin, no Capacitor bridge and no server cookies.
        view.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }
}
