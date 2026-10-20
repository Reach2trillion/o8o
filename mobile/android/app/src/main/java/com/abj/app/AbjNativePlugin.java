package com.abj.app;

import android.Manifest;
import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothClass;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothSocket;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.print.PrintDocumentAdapter;
import android.print.PrintManager;
import android.provider.Settings;
import android.util.Base64;
import android.webkit.WebView;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Logger;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;
import java.io.Closeable;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Pattern;

/**
 * Native side of the ABJ app, exposed to the Odoo web client as {@code Capacitor.Plugins.AbjNative}.
 *
 * Receipts are rasterised by the web client (ESC/POS {@code GS v 0} bands, base64), so this plugin only
 * moves bytes: Bluetooth Classic SPP, raw TCP (port 9100) and the Android print framework for the page.
 *
 * Prints run one at a time on their own background thread, each blocking step under a {@link Deadline}
 * so that a printer that stops reading cannot block it; device listing and downloads use another thread
 * and never wait behind a print. Every method rejects with a {@code code} instead of throwing: an
 * uncaught exception here would crash the whole app.
 * Rejection codes: NO_BLUETOOTH, BLUETOOTH_OFF, PERMISSION_DENIED, INVALID_ARGUMENT, CONNECT_FAILED,
 * WRITE_FAILED, TIMEOUT (the printer stopped accepting data), UNAVAILABLE, NATIVE_ERROR.
 */
@CapacitorPlugin(
    name = "AbjNative",
    permissions = { @Permission(strings = { Manifest.permission.BLUETOOTH_CONNECT }, alias = AbjNativePlugin.BLUETOOTH) }
)
public class AbjNativePlugin extends Plugin {

    static final String BLUETOOTH = "bluetooth";

    // Serial Port Profile: the service every Bluetooth Classic receipt printer exposes.
    private static final UUID SPP_UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB");
    private static final int MAX_PRINT_BYTES = 16 * 1024 * 1024;
    private static final int MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024;
    // RFCOMM connect() has no timeout of its own: both attempts (secure, then insecure) must end within this.
    private static final long SPP_CONNECT_TIMEOUT_MS = 30000;
    // Extra time of a Bluetooth print on top of its transfer time (network prints use their timeoutMs).
    private static final long SPP_WRITE_GRACE_MS = 10000;
    // Slowest transfer accepted before a print is aborted: 4 KB/s, far below the speed of any receipt printer.
    private static final long MIN_BYTES_PER_MS = 4;
    private static final Pattern PRINTER_NAME_HINT = Pattern.compile(
        "print|thermal|receipt|\\bpos|mtp|mpt|rpp|\\bpt-?\\d|xp-|zj-|gp-|bt-?58|bt-?80|58mm|80mm|innerprinter|tm-|spp-r|goojprt|munbyn|rongta|bixolon|sunmi",
        Pattern.CASE_INSENSITIVE
    );

    private ExecutorService printer; // printBluetooth, printTcp: one job at a time
    private ExecutorService io; // listPrinters, saveDownload
    private ScheduledExecutorService watchdog; // Deadline timers

    @Override
    public void load() {
        printer = Executors.newSingleThreadExecutor(daemonThreads("AbjNativePrint"));
        io = Executors.newSingleThreadExecutor(daemonThreads("AbjNativeIO"));
        ScheduledThreadPoolExecutor timers = new ScheduledThreadPoolExecutor(1, daemonThreads("AbjNativeWatchdog"));
        timers.setRemoveOnCancelPolicy(true); // a finished job does not keep its socket referenced until its timer is due
        watchdog = timers;
    }

    @Override
    protected void handleOnDestroy() {
        for (ExecutorService executor : new ExecutorService[] { printer, io, watchdog }) {
            if (executor != null) {
                executor.shutdownNow();
            }
        }
    }

    // ------------------------------------------------------------------
    // Info and permissions
    // ------------------------------------------------------------------

    @PluginMethod
    public void getInfo(PluginCall call) {
        try {
            JSObject info = new JSObject();
            info.put("platform", "android");
            PackageInfo packageInfo = packageInfo();
            info.put("appVersion", packageInfo != null && packageInfo.versionName != null ? packageInfo.versionName : "");
            info.put("appBuild", packageInfo != null ? String.valueOf(versionCode(packageInfo)) : "");
            info.put("appId", getContext().getPackageName());
            info.put("androidSdk", Build.VERSION.SDK_INT);
            info.put("firebase", isFirebaseConfigured());
            BluetoothAdapter adapter = bluetoothAdapter();
            info.put("bluetooth", adapter != null);
            info.put("bluetoothEnabled", adapter != null && isEnabled(adapter));
            info.put("bluetoothPermission", hasBluetoothPermission());
            call.resolve(info);
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Cannot read the app information", e);
        }
    }

    @PluginMethod
    public void requestBluetoothPermission(PluginCall call) {
        try {
            if (hasBluetoothPermission()) {
                resolveGranted(call, true);
            } else {
                requestPermissionForAlias(BLUETOOTH, call, "bluetoothPermissionCallback");
            }
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Cannot request the Bluetooth permission", e);
        }
    }

    /** Continues the call that asked for BLUETOOTH_CONNECT, whichever method it was. */
    @PermissionCallback
    private void bluetoothPermissionCallback(PluginCall call) {
        if (call == null) {
            return;
        }
        try {
            boolean granted = hasBluetoothPermission();
            String method = call.getMethodName();
            if ("requestBluetoothPermission".equals(method)) {
                resolveGranted(call, granted);
            } else if (!granted) {
                call.reject("Bluetooth permission denied", "PERMISSION_DENIED");
            } else if ("listPrinters".equals(method)) {
                listPrinters(call);
            } else if ("printBluetooth".equals(method)) {
                printBluetooth(call);
            } else {
                resolveGranted(call, true);
            }
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Bluetooth permission callback failed", e);
        }
    }

    /** BLUETOOTH_CONNECT only exists since Android 12; below it the install-time BLUETOOTH permission is enough. */
    @Override
    public Map<String, PermissionState> getPermissionStates() {
        Map<String, PermissionState> states = super.getPermissionStates();
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            states.put(BLUETOOTH, PermissionState.GRANTED);
        }
        return states;
    }

    // ------------------------------------------------------------------
    // Bluetooth Classic (SPP) printers
    // ------------------------------------------------------------------

    @PluginMethod
    public void listPrinters(PluginCall call) {
        try {
            if (!hasBluetoothPermission()) {
                requestPermissionForAlias(BLUETOOTH, call, "bluetoothPermissionCallback");
                return;
            }
            runIo(io, call, () -> {
                BluetoothAdapter adapter = requireEnabledAdapter();
                JSObject result = new JSObject();
                result.put("devices", bondedPrinters(adapter));
                call.resolve(result);
            });
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Cannot list the paired printers", e);
        }
    }

    @PluginMethod
    public void printBluetooth(PluginCall call) {
        try {
            final String address = call.getString("address", "").trim().toUpperCase(Locale.ROOT);
            if (!BluetoothAdapter.checkBluetoothAddress(address)) {
                call.reject("Invalid Bluetooth printer address", "INVALID_ARGUMENT");
                return;
            }
            final byte[] data = decodeData(call, MAX_PRINT_BYTES);
            if (data == null) {
                return;
            }
            final int chunkSize = clamp(intOption(call, "chunkSize", 512), 16, 64 * 1024);
            final int chunkDelayMs = clamp(intOption(call, "chunkDelayMs", 0), 0, 1000);
            if (!hasBluetoothPermission()) {
                requestPermissionForAlias(BLUETOOTH, call, "bluetoothPermissionCallback");
                return;
            }
            runIo(printer, call, () -> {
                BluetoothAdapter adapter = requireEnabledAdapter();
                writeSpp(adapter, address, data, chunkSize, chunkDelayMs);
                JSObject result = new JSObject();
                result.put("bytes", data.length);
                call.resolve(result);
            });
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Bluetooth printing failed", e);
        }
    }

    @SuppressLint("MissingPermission") // BLUETOOTH_CONNECT is checked by every caller (hasBluetoothPermission)
    private JSArray bondedPrinters(BluetoothAdapter adapter) {
        Set<BluetoothDevice> bonded = adapter.getBondedDevices();
        List<JSObject> devices = new ArrayList<>();
        if (bonded != null) {
            for (BluetoothDevice device : bonded) {
                if (device.getType() == BluetoothDevice.DEVICE_TYPE_LE) {
                    continue; // BLE-only devices have no SPP socket; the web client handles them with Web Bluetooth
                }
                String name = device.getName() != null ? device.getName() : "";
                BluetoothClass btClass = device.getBluetoothClass();
                int deviceClass = btClass != null ? btClass.getDeviceClass() : 0;
                int majorClass = btClass != null ? btClass.getMajorDeviceClass() : 0;
                JSObject entry = new JSObject();
                entry.put("name", name.isEmpty() ? device.getAddress() : name);
                entry.put("address", device.getAddress());
                entry.put("deviceClass", deviceClass);
                entry.put("likelyPrinter", majorClass == BluetoothClass.Device.Major.IMAGING || PRINTER_NAME_HINT.matcher(name).find());
                devices.add(entry);
            }
        }
        // Printers first, then by name
        Collections.sort(devices, (a, b) -> {
            boolean pa = a.getBoolean("likelyPrinter", false);
            boolean pb = b.getBoolean("likelyPrinter", false);
            if (pa != pb) {
                return pa ? -1 : 1;
            }
            return a.getString("name", "").compareToIgnoreCase(b.getString("name", ""));
        });
        return new JSArray(devices);
    }

    @SuppressLint("MissingPermission") // see bondedPrinters
    private void writeSpp(BluetoothAdapter adapter, String address, byte[] data, int chunkSize, int chunkDelayMs) throws AbjException {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            // An ongoing discovery slows RFCOMM connections down; on Android 12+ cancelling it needs BLUETOOTH_SCAN.
            try {
                adapter.cancelDiscovery();
            } catch (SecurityException ignored) {
                // not fatal
            }
        }
        BluetoothSocket socket = connectSpp(adapter.getRemoteDevice(address));
        long chunks = (data.length + chunkSize - 1L) / chunkSize;
        int drainMs = Math.min(2000, data.length / 16);
        // A printer that stops reading (paper out, cover open) blocks write() until the deadline closes the socket.
        try (Deadline sending = new Deadline(transferBudgetMs(data.length, SPP_WRITE_GRACE_MS) + chunks * chunkDelayMs + drainMs)) {
            sending.watch(socket);
            try {
                OutputStream out = socket.getOutputStream();
                for (int offset = 0; offset < data.length; offset += chunkSize) {
                    out.write(data, offset, Math.min(chunkSize, data.length - offset));
                    if (chunkDelayMs > 0) {
                        out.flush();
                        Thread.sleep(chunkDelayMs);
                    }
                }
                out.flush();
                // The socket write returns before the printer has received everything: closing right away
                // truncates the end of the receipt on many models.
                Thread.sleep(drainMs);
            } catch (IOException e) {
                throw writeFailed(sending, e);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new AbjException("WRITE_FAILED", "Printing was interrupted");
        } finally {
            closeQuietly(socket);
        }
    }

    /** Secure RFCOMM first (bonded printers), then insecure for the printers that refuse it. */
    @SuppressLint("MissingPermission") // see bondedPrinters
    private BluetoothSocket connectSpp(BluetoothDevice device) throws AbjException {
        IOException lastError = null;
        try (Deadline connecting = new Deadline(SPP_CONNECT_TIMEOUT_MS)) {
            for (boolean secure : new boolean[] { true, false }) {
                BluetoothSocket socket = null;
                try {
                    socket = secure
                        ? device.createRfcommSocketToServiceRecord(SPP_UUID)
                        : device.createInsecureRfcommSocketToServiceRecord(SPP_UUID);
                    connecting.watch(socket); // close() from another thread aborts a blocked connect()
                    socket.connect();
                    return socket;
                } catch (IOException e) {
                    lastError = e;
                    closeQuietly(socket);
                }
                if (connecting.expired()) {
                    throw new AbjException("CONNECT_FAILED", "Cannot connect to the printer (timed out): is it on, in range and paired?");
                }
            }
        }
        String reason = lastError != null && lastError.getMessage() != null ? ": " + lastError.getMessage() : "";
        throw new AbjException("CONNECT_FAILED", "Cannot connect to the printer (is it on and paired?)" + reason);
    }

    // ------------------------------------------------------------------
    // Network printers (raw TCP, usually port 9100)
    // ------------------------------------------------------------------

    @PluginMethod
    public void printTcp(PluginCall call) {
        try {
            final String host = call.getString("host", "").trim();
            final int port = intOption(call, "port", 9100);
            final int timeoutMs = clamp(intOption(call, "timeoutMs", 8000), 1000, 60000);
            if (host.isEmpty() || host.contains(" ") || host.contains("/")) {
                call.reject("Invalid printer host", "INVALID_ARGUMENT");
                return;
            }
            if (port < 1 || port > 65535) {
                call.reject("Invalid printer port", "INVALID_ARGUMENT");
                return;
            }
            final byte[] data = decodeData(call, MAX_PRINT_BYTES);
            if (data == null) {
                return;
            }
            runIo(printer, call, () -> {
                writeTcp(host, port, data, timeoutMs);
                JSObject result = new JSObject();
                result.put("bytes", data.length);
                call.resolve(result);
            });
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Network printing failed", e);
        }
    }

    private void writeTcp(String host, int port, byte[] data, int timeoutMs) throws AbjException {
        Socket socket = new Socket();
        try {
            try {
                socket.connect(new InetSocketAddress(host, port), timeoutMs);
            } catch (IOException | IllegalArgumentException e) {
                throw new AbjException("CONNECT_FAILED", "Cannot connect to " + host + ":" + port + ": " + e.getMessage());
            }
            // write() has no timeout of its own (setSoTimeout only bounds reads): a printer that accepted the
            // connection and then stopped reading (paper out, cover open) would block it for ever.
            try (Deadline sending = new Deadline(transferBudgetMs(data.length, timeoutMs))) {
                sending.watch(socket);
                try {
                    socket.setTcpNoDelay(true);
                    OutputStream out = socket.getOutputStream();
                    out.write(data);
                    out.flush();
                } catch (IOException e) {
                    throw writeFailed(sending, e);
                }
            }
        } finally {
            closeQuietly(socket);
        }
    }

    // ------------------------------------------------------------------
    // System printing (Android print framework: PDF, Wi-Fi and vendor print services)
    // ------------------------------------------------------------------

    @PluginMethod
    public void printPage(PluginCall call) {
        try {
            String requested = call.getString("jobName", "");
            final String jobName = requested == null || requested.trim().isEmpty() ? "ABJ" : requested.trim();
            getBridge().executeOnMainThread(() -> {
                try {
                    WebView webView = getBridge().getWebView();
                    PrintManager printManager = (PrintManager) getActivity().getSystemService(Context.PRINT_SERVICE);
                    if (webView == null || printManager == null) {
                        call.reject("Printing is not available on this device", "UNAVAILABLE");
                        return;
                    }
                    // Prints the current page with its @media print rules (the receipt area only).
                    PrintDocumentAdapter adapter = webView.createPrintDocumentAdapter(jobName);
                    printManager.print(jobName, adapter, null);
                    JSObject result = new JSObject();
                    result.put("jobName", jobName);
                    call.resolve(result);
                } catch (Exception e) {
                    fail(call, "NATIVE_ERROR", "Cannot open the print dialog", e);
                }
            });
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Cannot open the print dialog", e);
        }
    }

    // ------------------------------------------------------------------
    // Settings shortcuts
    // ------------------------------------------------------------------

    @PluginMethod
    public void openBluetoothSettings(PluginCall call) {
        openSettings(call, new Intent(Settings.ACTION_BLUETOOTH_SETTINGS));
    }

    @PluginMethod
    public void openAppSettings(PluginCall call) {
        Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
        intent.setData(Uri.fromParts("package", getContext().getPackageName(), null));
        openSettings(call, intent);
    }

    private void openSettings(PluginCall call, Intent intent) {
        getBridge().executeOnMainThread(() -> {
            try {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                getActivity().startActivity(intent);
                call.resolve();
            } catch (Exception e) {
                fail(call, "UNAVAILABLE", "Cannot open the settings screen", e);
            }
        });
    }

    // ------------------------------------------------------------------
    // Downloads (blob: / data: files of the Odoo web client, see AbjDownloads)
    // ------------------------------------------------------------------

    @PluginMethod
    public void saveDownload(PluginCall call) {
        try {
            final byte[] data = decodeData(call, MAX_DOWNLOAD_BYTES);
            if (data == null) {
                return;
            }
            final String fileName = call.getString("fileName", "");
            final String mimeType = call.getString("mimeType", "");
            runIo(io, call, () -> {
                AbjDownloads.Saved saved;
                try {
                    saved = AbjDownloads.save(getContext(), data, fileName, mimeType);
                } catch (IOException e) {
                    throw new AbjException("WRITE_FAILED", "Cannot save the file: " + e.getMessage());
                }
                AbjDownloads.open(getActivity(), saved);
                JSObject result = new JSObject();
                result.put("fileName", saved.fileName);
                result.put("uri", saved.uri.toString());
                call.resolve(result);
            });
        } catch (Exception e) {
            fail(call, "NATIVE_ERROR", "Cannot save the file", e);
        }
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private interface IoTask {
        void run() throws Exception;
    }

    /** Checked failure carrying the rejection code. */
    static final class AbjException extends Exception {

        private static final long serialVersionUID = 1L;
        final String code;

        AbjException(String code, String message) {
            super(message);
            this.code = code;
        }
    }

    /**
     * Watchdog of one blocking step of a print. When it expires it closes the watched socket from the
     * watchdog thread, which makes a blocked connect() or write() fail at once; {@link #expired()} then
     * tells the timeout apart from a lost connection.
     */
    private final class Deadline implements AutoCloseable {

        private final AtomicBoolean expired = new AtomicBoolean();
        private final AtomicReference<Closeable> watched = new AtomicReference<>();
        private final ScheduledFuture<?> timer;

        Deadline(long delayMs) {
            timer = watchdog.schedule(this::expire, delayMs, TimeUnit.MILLISECONDS);
        }

        /** Closes {@code socket} when the deadline expires (at once if it already has). */
        void watch(Closeable socket) {
            watched.set(socket);
            if (expired.get()) {
                closeQuietly(socket);
            }
        }

        boolean expired() {
            return expired.get();
        }

        private void expire() {
            expired.set(true);
            closeQuietly(watched.get());
        }

        @Override
        public void close() {
            timer.cancel(false);
        }
    }

    /** Time a print may take once connected: {@code graceMs} plus its size sent at {@link #MIN_BYTES_PER_MS}. */
    private static long transferBudgetMs(int bytes, long graceMs) {
        return graceMs + bytes / MIN_BYTES_PER_MS;
    }

    /** TIMEOUT when the deadline closed the socket (the printer stopped reading), else WRITE_FAILED. */
    private static AbjException writeFailed(Deadline deadline, IOException error) {
        if (deadline.expired()) {
            return new AbjException("TIMEOUT", "The printer stopped accepting data. Check its paper and cover, then print again.");
        }
        return new AbjException("WRITE_FAILED", "The connection to the printer was lost while printing: " + error.getMessage());
    }

    private static ThreadFactory daemonThreads(String name) {
        return (runnable) -> {
            Thread thread = new Thread(runnable, name);
            thread.setDaemon(true);
            return thread;
        };
    }

    /** Runs a task on a background thread; any failure (even an Error) rejects the call instead of killing the app. */
    private void runIo(ExecutorService executor, PluginCall call, IoTask task) {
        try {
            executor.execute(() -> {
                try {
                    task.run();
                } catch (AbjException e) {
                    call.reject(e.getMessage(), e.code);
                } catch (SecurityException e) {
                    fail(call, "PERMISSION_DENIED", "Permission denied", e);
                } catch (Throwable e) {
                    fail(call, "NATIVE_ERROR", "Unexpected error", e);
                }
            });
        } catch (Exception e) {
            fail(call, "UNAVAILABLE", "The native printer service is stopped", e);
        }
    }

    private void fail(PluginCall call, String code, String message, Throwable error) {
        Logger.error(getLogTag(), message, error);
        String detail = error != null && error.getMessage() != null ? message + ": " + error.getMessage() : message;
        try {
            call.reject(detail, code);
        } catch (Exception ignored) {
            // the call may already have been answered
        }
    }

    private void resolveGranted(PluginCall call, boolean granted) {
        JSObject result = new JSObject();
        result.put("granted", granted);
        call.resolve(result);
    }

    /** Decodes the base64 {@code data} option; rejects the call and returns null when it is unusable. */
    private byte[] decodeData(PluginCall call, int maxBytes) {
        String data = call.getString("data", "");
        int comma = data.indexOf(',');
        if (data.startsWith("data:") && comma > 0) {
            data = data.substring(comma + 1); // tolerate a data: URL
        }
        if (data.isEmpty()) {
            call.reject("No data to send", "INVALID_ARGUMENT");
            return null;
        }
        if ((long) data.length() * 3 / 4 > maxBytes) {
            call.reject("The data is too large", "INVALID_ARGUMENT");
            return null;
        }
        try {
            byte[] bytes = Base64.decode(data, Base64.DEFAULT);
            if (bytes.length == 0) {
                call.reject("No data to send", "INVALID_ARGUMENT");
                return null;
            }
            return bytes;
        } catch (IllegalArgumentException e) {
            call.reject("The data is not valid base64", "INVALID_ARGUMENT");
            return null;
        }
    }

    /** Integer option that also accepts numeric strings (values typed in an input field). */
    private static int intOption(PluginCall call, String name, int defaultValue) {
        Object value = call.getData().opt(name);
        if (value instanceof Number) {
            return ((Number) value).intValue();
        }
        if (value instanceof String) {
            try {
                return Integer.parseInt(((String) value).trim());
            } catch (NumberFormatException ignored) {
                return defaultValue;
            }
        }
        return defaultValue;
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }

    private static void closeQuietly(Closeable closeable) {
        if (closeable == null) {
            return;
        }
        try {
            closeable.close();
        } catch (IOException ignored) {
            // nothing left to do
        }
    }

    private boolean hasBluetoothPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
            return true;
        }
        return getContext().checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED;
    }

    private BluetoothAdapter bluetoothAdapter() {
        BluetoothManager manager = (BluetoothManager) getContext().getSystemService(Context.BLUETOOTH_SERVICE);
        return manager != null ? manager.getAdapter() : null;
    }

    private static boolean isEnabled(BluetoothAdapter adapter) {
        try {
            return adapter.isEnabled();
        } catch (SecurityException e) {
            return false;
        }
    }

    private BluetoothAdapter requireEnabledAdapter() throws AbjException {
        BluetoothAdapter adapter = bluetoothAdapter();
        if (adapter == null) {
            throw new AbjException("NO_BLUETOOTH", "This device has no Bluetooth");
        }
        if (!isEnabled(adapter)) {
            throw new AbjException("BLUETOOTH_OFF", "Bluetooth is turned off");
        }
        if (!hasBluetoothPermission()) {
            throw new AbjException("PERMISSION_DENIED", "Bluetooth permission denied");
        }
        return adapter;
    }

    @SuppressWarnings("deprecation")
    private PackageInfo packageInfo() {
        try {
            return getContext().getPackageManager().getPackageInfo(getContext().getPackageName(), 0);
        } catch (PackageManager.NameNotFoundException e) {
            return null;
        }
    }

    @SuppressWarnings("deprecation")
    private static long versionCode(PackageInfo packageInfo) {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P ? packageInfo.getLongVersionCode() : packageInfo.versionCode;
    }

    /**
     * True when google-services.json was bundled (Firebase initialised itself). Checked by reflection:
     * firebase-messaging is a dependency of the push plugin only, and PushNotifications.register()
     * crashes the app when Firebase is not configured.
     */
    private boolean isFirebaseConfigured() {
        try {
            Class<?> firebaseApp = Class.forName("com.google.firebase.FirebaseApp");
            Object apps = firebaseApp.getMethod("getApps", Context.class).invoke(null, getContext());
            return apps instanceof List && !((List<?>) apps).isEmpty();
        } catch (Throwable e) {
            return false;
        }
    }
}
