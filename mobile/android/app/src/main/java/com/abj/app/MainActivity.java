package com.abj.app;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.os.Build;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.WebView;
import androidx.activity.OnBackPressedCallback;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.Logger;

/**
 * ABJ: a Capacitor WebView on the Sale Check phone screen of the Odoo server (capacitor.config.ts).
 */
public class MainActivity extends BridgeActivity {

    /** FCM channel: AndroidManifest meta-data, server payloads (android.notification.channel_id) and the web client use it. */
    static final String NOTIFICATION_CHANNEL_ID = "abj_default";
    private static final String TAG = "ABJ";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // App plugins must be registered before BridgeActivity.onCreate() builds the bridge.
        registerPlugin(AbjNativePlugin.class);
        super.onCreate(savedInstanceState);
        createNotificationChannel();
        if (bridge == null || bridge.getWebView() == null) {
            return; // no usable WebView: BridgeActivity already shows its error layout
        }
        try {
            WebView webView = bridge.getWebView();
            bridge.setWebViewClient(new AbjWebViewClient(bridge));
            webView.setDownloadListener(new AbjDownloads.Listener(this, webView));
            getOnBackPressedDispatcher().addCallback(this, new BackCallback());
        } catch (Exception e) {
            Logger.error(TAG, "WebView setup failed", e);
        }
    }

    @Override
    public void onPause() {
        super.onPause();
        // Write the Odoo session cookie to disk now: the WebView only syncs cookies periodically, and a
        // killed app would otherwise come back logged out.
        try {
            CookieManager.getInstance().flush();
        } catch (Exception e) {
            Logger.error(TAG, "Cookie flush failed", e);
        }
    }

    /**
     * Creates the channel early so that pushes received before the web client starts (app killed) use
     * it instead of FCM's "Miscellaneous" fallback channel. Same id and name as the web client creates.
     */
    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        try {
            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager == null || manager.getNotificationChannel(NOTIFICATION_CHANNEL_ID) != null) {
                return;
            }
            NotificationChannel channel = new NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                getString(R.string.abj_channel_name),
                NotificationManager.IMPORTANCE_HIGH
            );
            channel.setDescription(getString(R.string.abj_channel_description));
            manager.createNotificationChannel(channel);
        } catch (Exception e) {
            Logger.error(TAG, "Cannot create the notification channel", e);
        }
    }

    /**
     * Back button: @capacitor/app goes back in the WebView history but ignores the press when there is
     * none left; then send the app to the background (keeps the Odoo session warm) instead of doing nothing.
     */
    private final class BackCallback extends OnBackPressedCallback {

        BackCallback() {
            super(true);
        }

        @Override
        public void handleOnBackPressed() {
            try {
                WebView webView = bridge != null ? bridge.getWebView() : null;
                if (webView != null && webView.canGoBack()) {
                    // Hand the press to the next callback (@capacitor/app) so its JS "backButton" listeners still fire.
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                    setEnabled(true);
                } else {
                    moveTaskToBack(true);
                }
            } catch (Exception e) {
                setEnabled(true);
                Logger.error(TAG, "Back navigation failed", e);
            }
        }
    }
}
