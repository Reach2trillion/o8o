package com.abj.app;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.MimeTypeMap;
import android.webkit.URLUtil;
import android.webkit.WebView;
import android.widget.Toast;
import androidx.core.content.FileProvider;
import com.getcapacitor.Logger;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.UnsupportedEncodingException;
import java.net.URLDecoder;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.json.JSONObject;

/**
 * File downloads of the Odoo web client inside the app WebView.
 *
 * A WebView does not download anything by itself. Two cases:
 * - plain http(s) links (attachments, /web/content?download=true): {@link Listener} hands them to the
 *   system DownloadManager together with the WebView session cookie, so the user stays logged in;
 * - files built in the page (Odoo PDF reports, exports: an {@code <a download href="blob:...">} clicked
 *   by script and revoked right after): {@link #SHIM_JS}, injected in every server page, reads the blob
 *   in the page and passes it to {@code AbjNative.saveDownload}, which stores it in Downloads and opens it.
 */
final class AbjDownloads {

    private static final String TAG = "AbjDownloads";
    private static final String DEFAULT_MIME = "application/octet-stream";
    private static final String PACKAGE_MIME = "application/vnd.android.package-archive";
    // Content-Disposition parameters: filename*=charset'language'percent-encoded, and filename="quoted" or token
    private static final Pattern DISPOSITION_EXTENDED_NAME = Pattern.compile(
        "filename\\*\\s*=\\s*([^']*)'[^']*'([^;\\s]+)",
        Pattern.CASE_INSENSITIVE
    );
    private static final Pattern DISPOSITION_NAME = Pattern.compile(
        "filename\\s*=\\s*(?:\"((?:\\\\.|[^\"\\\\])*)\"|([^;\\s]+))",
        Pattern.CASE_INSENSITIVE
    );

    /** Installed once per document by AbjWebViewClient.onPageFinished. */
    static final String SHIM_JS =
        "(function () {\n" +
        "    if (window.__abjDownloadShim) { return; }\n" +
        "    window.__abjDownloadShim = true;\n" +
        "    function plugin() {\n" +
        "        var cap = window.Capacitor;\n" +
        "        return cap && cap.Plugins && cap.Plugins.AbjNative;\n" +
        "    }\n" +
        "    function save(href, fileName) {\n" +
        "        return fetch(href)\n" +
        "            .then(function (response) { return response.blob(); })\n" +
        "            .then(function (blob) {\n" +
        "                return new Promise(function (resolve, reject) {\n" +
        "                    var reader = new FileReader();\n" +
        "                    reader.onload = function () { resolve({ blob: blob, data: String(reader.result).split(',')[1] || '' }); };\n" +
        "                    reader.onerror = function () { reject(reader.error); };\n" +
        "                    reader.readAsDataURL(blob);\n" +
        "                });\n" +
        "            })\n" +
        "            .then(function (res) {\n" +
        "                return plugin().saveDownload({ data: res.data, fileName: fileName || '', mimeType: res.blob.type || '' });\n" +
        "            })\n" +
        "            .catch(function (error) { console.warn('ABJ: download failed', error); });\n" +
        "    }\n" +
        "    window.__abjSaveDownload = save;\n" +
        "    document.addEventListener('click', function (ev) {\n" +
        "        var link = ev.target && ev.target.closest ? ev.target.closest('a[download]') : null;\n" +
        "        var href = link ? link.href || '' : '';\n" +
        "        if (!plugin() || (href.indexOf('blob:') !== 0 && href.indexOf('data:') !== 0)) { return; }\n" +
        "        ev.preventDefault();\n" +
        "        save(href, link.getAttribute('download'));\n" +
        "    }, true);\n" +
        "})();";

    private AbjDownloads() {}

    /** Result of {@link #save}: where the file landed. */
    static final class Saved {

        final Uri uri;
        final String fileName;
        final String mimeType;

        Saved(Uri uri, String fileName, String mimeType) {
            this.uri = uri;
            this.fileName = fileName;
            this.mimeType = mimeType;
        }
    }

    /** DownloadListener of the bridge WebView (set in MainActivity). */
    static final class Listener implements DownloadListener {

        private final Activity activity;
        private final WebView webView;

        Listener(Activity activity, WebView webView) {
            this.activity = activity;
            this.webView = webView;
        }

        @Override
        public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long contentLength) {
            try {
                String fileName = safeFileName(dispositionFileName(url, contentDisposition, mimeType), mimeType);
                if (url.startsWith("blob:") || url.startsWith("data:")) {
                    // Normally caught earlier by SHIM_JS; read it in the page while the blob still exists.
                    String name = contentDisposition != null && !contentDisposition.isEmpty() ? fileName : "";
                    webView.evaluateJavascript(
                        "window.__abjSaveDownload && window.__abjSaveDownload(" + JSONObject.quote(url) + ", " + JSONObject.quote(name) + ");",
                        null
                    );
                    return;
                }
                if (!URLUtil.isNetworkUrl(url)) {
                    return;
                }
                enqueue(url, userAgent, mimeType, fileName);
                toast(activity, activity.getString(R.string.abj_download_started, fileName));
            } catch (Exception e) {
                Logger.error(TAG, "Download failed: " + url, e);
                toast(activity, activity.getString(R.string.abj_download_failed));
            }
        }

        private void enqueue(String url, String userAgent, String mimeType, String fileName) {
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
            String cookies = CookieManager.getInstance().getCookie(url);
            if (cookies != null) {
                request.addRequestHeader("Cookie", cookies);
            }
            if (userAgent != null) {
                request.addRequestHeader("User-Agent", userAgent);
            }
            if (mimeType != null && !mimeType.isEmpty()) {
                request.setMimeType(mimeType);
            }
            request.setTitle(fileName);
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName);
            } else {
                // The public Downloads folder needs WRITE_EXTERNAL_STORAGE before Android 10; the app folder does not.
                request.setDestinationInExternalFilesDir(activity, Environment.DIRECTORY_DOWNLOADS, fileName);
            }
            DownloadManager manager = (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) {
                throw new IllegalStateException("DownloadManager is not available");
            }
            manager.enqueue(request);
        }
    }

    /** Stores the bytes in Downloads (MediaStore on Android 10+, the app's own Download folder before). */
    static Saved save(Context context, byte[] data, String requestedName, String requestedMime) throws IOException {
        String mimeType = requestedMime == null || requestedMime.trim().isEmpty() ? null : requestedMime.trim().toLowerCase(Locale.ROOT);
        String fileName = safeFileName(requestedName, mimeType);
        if (mimeType == null) {
            String extension = MimeTypeMap.getFileExtensionFromUrl(Uri.encode(fileName));
            mimeType = extension != null ? MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension.toLowerCase(Locale.ROOT)) : null;
            if (mimeType == null) {
                mimeType = DEFAULT_MIME;
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContentResolver resolver = context.getContentResolver();
            ContentValues values = new ContentValues();
            values.put(MediaStore.MediaColumns.DISPLAY_NAME, fileName);
            values.put(MediaStore.MediaColumns.MIME_TYPE, mimeType);
            values.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
            values.put(MediaStore.MediaColumns.IS_PENDING, 1);
            Uri uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
            if (uri == null) {
                throw new IOException("the Downloads folder is not available");
            }
            try (OutputStream out = resolver.openOutputStream(uri)) {
                if (out == null) {
                    throw new IOException("cannot write to the Downloads folder");
                }
                out.write(data);
            } catch (IOException | RuntimeException e) {
                resolver.delete(uri, null, null);
                throw e;
            }
            values.clear();
            values.put(MediaStore.MediaColumns.IS_PENDING, 0);
            resolver.update(uri, values, null, null);
            return new Saved(uri, fileName, mimeType);
        }
        File dir = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
        if (dir == null) {
            dir = new File(context.getCacheDir(), "downloads");
        }
        if (!dir.isDirectory() && !dir.mkdirs()) {
            throw new IOException("cannot create " + dir);
        }
        File file = uniqueFile(dir, fileName);
        try (OutputStream out = new FileOutputStream(file)) {
            out.write(data);
        }
        Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".fileprovider", file);
        return new Saved(uri, file.getName(), mimeType);
    }

    /** Tells the user where the file went and opens it in a viewer when one is installed. */
    static void open(Activity activity, Saved saved) {
        activity.runOnUiThread(() -> {
            try {
                toast(activity, activity.getString(R.string.abj_download_saved, saved.fileName));
                if (PACKAGE_MIME.equals(saved.mimeType) || saved.fileName.toLowerCase(Locale.ROOT).endsWith(".apk")) {
                    return; // never hand an app package to the installer automatically
                }
                Intent view = new Intent(Intent.ACTION_VIEW);
                view.setDataAndType(saved.uri, saved.mimeType);
                view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
                activity.startActivity(view);
            } catch (ActivityNotFoundException e) {
                // no viewer for this type: the file stays in Downloads
            } catch (Exception e) {
                Logger.error(TAG, "Cannot open " + saved.fileName, e);
            }
        });
    }

    /**
     * File name of a download: the {@code filename*} (RFC 6266 / 5987) or {@code filename} parameter of its
     * Content-Disposition, else {@link URLUtil#guessFileName}. Odoo sends only {@code filename*=UTF-8''...},
     * which guessFileName ignores before Android 15: it would save /web/content/123 as "123.pdf".
     */
    static String dispositionFileName(String url, String contentDisposition, String mimeType) {
        if (contentDisposition != null) {
            Matcher extended = DISPOSITION_EXTENDED_NAME.matcher(contentDisposition);
            if (extended.find()) {
                String charset = extended.group(1).trim();
                try {
                    // percent-encoded: a "+" is a plus sign, not a space
                    String name = URLDecoder.decode(extended.group(2).replace("+", "%2B"), charset.isEmpty() ? "UTF-8" : charset);
                    if (!name.trim().isEmpty()) {
                        return name;
                    }
                } catch (UnsupportedEncodingException | IllegalArgumentException e) {
                    // unknown charset or bad escape: try the plain form
                }
            }
            Matcher plain = DISPOSITION_NAME.matcher(contentDisposition);
            if (plain.find()) {
                String name = plain.group(1) != null ? plain.group(1).replaceAll("\\\\(.)", "$1") : plain.group(2);
                if (!name.trim().isEmpty()) {
                    return name;
                }
            }
        }
        return URLUtil.guessFileName(url, contentDisposition, mimeType);
    }

    static String safeFileName(String requested, String mimeType) {
        String name = requested == null ? "" : requested.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_").trim();
        while (name.startsWith(".")) {
            name = name.substring(1);
        }
        if (name.isEmpty()) {
            name = "ABJ-" + System.currentTimeMillis();
        }
        if (name.length() > 120) {
            name = name.substring(name.length() - 120);
        }
        if (name.lastIndexOf('.') <= 0 && mimeType != null) {
            String extension = MimeTypeMap.getSingleton().getExtensionFromMimeType(mimeType);
            if (extension != null) {
                name = name + "." + extension;
            }
        }
        return name;
    }

    private static File uniqueFile(File dir, String fileName) {
        File file = new File(dir, fileName);
        int dot = fileName.lastIndexOf('.');
        String base = dot > 0 ? fileName.substring(0, dot) : fileName;
        String extension = dot > 0 ? fileName.substring(dot) : "";
        for (int i = 1; file.exists() && i < 1000; i++) {
            file = new File(dir, base + " (" + i + ")" + extension);
        }
        return file;
    }

    private static void toast(Activity activity, String message) {
        activity.runOnUiThread(() -> Toast.makeText(activity, message, Toast.LENGTH_SHORT).show());
    }
}
