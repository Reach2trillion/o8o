# Sale Check (Odoo 18)

Mobile-friendly sales activity management and tracking for outside sales teams.

| Feature | What it does |
|---|---|
| **GPS Check-ins** | Salespeople check in at the customer's site from their phone. The position is captured with the device GPS, linked to Google Maps and compared with the customer's geolocation to mark the visit as *Verified on site* or *Far from customer*. |
| **Customer Management** | Every check-in is tied to a customer, an activity type (visit, meeting, demo, delivery, collection, call...), a time, a location, meeting notes, an outcome, a follow-up date, a sales amount and a photo. Quotations can be created straight from the visit. |
| **KPI Tracking** | Monthly targets per salesperson (visits, new customers, orders, sales amount) with real-time actuals, achievement percentages, expected progress and an *On track / At risk / Behind / Achieved* status. |
| **Telegram Integration** | Instant check-in / check-out notifications (message, location pin, photo) to the managers' Telegram group and to the salesperson's manager, plus automatic daily, weekly and monthly activity reports. |
| **Performance Reports** | Daily, weekly, monthly and yearly results per employee in pivot / graph views (visits, verified visits, new customers, orders, sales amount), a printable PDF activity report and a per-visit "Visit Sheet". |

## Requirements

* Odoo 18.0, Community or Enterprise. The automated tests run on Community only: see [Production notes (ABJ server)](#production-notes-abj-server) before upgrading an Enterprise database.
* Dependencies (all standard): `base`, `mail`, `hr`, `sale`, `base_geolocalize`.
* Outbound HTTPS access from the Odoo server to `api.telegram.org` for the Telegram features.

## Installation

1. Copy `checkinme_sales_activity` into your addons path.
2. Update the apps list and install **Sale Check**.
3. Give users one of the two access levels (Settings > Users > *Sale Check*):
   * **Salesperson** - creates and edits own check-ins (planned visits may be deleted), creates customers from the phone screen (the group implies Odoo's *Contact Creation* right), reads the check-ins, targets and reports of direct reports.
   * **Manager** - full access, targets, activity types, Telegram logs, ABJ Push Logs and ABJ App Devices.
4. Make sure every salesperson has an **Employee** record linked to their user (Employees app). Check-ins, targets and reports are attached to the employee; the employee's timezone is used to compute the check-in day.

## Configuration (Settings > Sale Check)

Like every Odoo settings page, this one is only available to users with *Administration > Settings* rights (the Sale Check **Manager** group alone is not enough to open Settings).

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the **bot token**.
2. Add the bot to the managers' group (or channel) and make sure it may post there.
3. Find the **chat id** of the group (e.g. add [@userinfobot](https://t.me/userinfobot) / [@getidsbot](https://t.me/getidsbot) to the group, or read it from `https://api.telegram.org/bot<TOKEN>/getUpdates`). Group ids are negative numbers such as `-1001234567890`.
4. Enter both values in *Settings > Sale Check > Telegram Integration* and click **Test Connection**.
5. Optional: give managers a personal chat id on their employee form (Employees app > employee > *HR Settings* tab > Sale Check > *Telegram Chat ID*; editing it requires HR *Officer* rights). When an employee checks in, the notification is sent to the managers' group **and** to the personal chat of the employee's manager and department manager. If your users already have a *Telegram Group ID* on their user record (from another Telegram module), it is used as a fallback.

Check-in and check-out notifications are queued and delivered by the scheduled action *Sale Check: Send Queued Telegram Notifications* a few seconds after the record is saved, so a slow or unreachable Telegram never delays the salesperson; make sure the Odoo cron worker is running (it is on any standard deployment).

Notification toggles: check-in, check-out, location pin, photo. Automatic reports: daily at 18:00, weekly on Monday at 09:00 (previous week) and monthly on the 1st at 09:00 (previous month), in the timezone of the main company at installation time. The schedules are ordinary scheduled actions (*Settings > Technical > Scheduled Actions > Sale Check: ...*) and can be changed there.

If another Telegram module already stores a bot token under `send_by_telegram.bot_token` or `abj.telegram.bot_token`, it is used as a fallback when no Sale Check token is set.

### GPS verification

* **Require GPS position to check in** (default on): a check-in cannot be saved without a captured position.
* **Maximum distance** (default 500 m): a visit is *Verified on site* when the check-in position is within this distance of the customer's geolocation. Geolocate customers on the contact form: *Partner Assignment* tab > Geolocation > **Compute based on address** (provided by `base_geolocalize`, uses OpenStreetMap or Google Maps as configured in General Settings), or fill *Geo Latitude / Geo Longitude* manually. Use *Recompute Location Check* on the check-in list after geolocating customers retroactively.

### KPI

* **Sales results source**: measure sales against targets from confirmed sales orders of the salesperson (default) or from the *Sales Amount* entered on check-ins.

## Phone screen (Sale Check > New check-in)

The first menu of the app is a dedicated phone screen, built the same way as a quick-order screen: a single page that walks the salesperson through the day without ever opening a backend form.

1. **Home**: today's *Visits / Verified / Customers / Check-ins* counters, the currently open visit with a big **Check Out** button, **Check In Now**, the month's targets with progress bars (*This month*), the planned visits with **Check In** buttons, *Follow-ups today* (done visits whose follow-up date is today; **Check In** starts a new visit at that customer) and *Today's visits*. The top bar opens *My visits* (history icon) and the app settings (gear icon); in the web app, a *Turn on notifications* banner shows until notifications are enabled or the banner is dismissed.
2. **Check in** in three taps: the position is captured automatically (**Retry** if needed), then the customer is picked from *Customers near you* (sorted by distance from the phone's position), *Recently visited* or a search by name / phone, or created on the spot (the phone's position becomes the customer's geolocation), then the activity type is chosen from large tiles, with purpose, notes and a photo from the camera.
3. **Check out** with the outcome tiles, sales amount, follow-up date and notes; the check-out position is captured again.
4. **Result** card with the reference, the verification badge (distance to the customer), the Telegram status, a Google Maps link, the print buttons (see [Printing receipts](#printing-receipts)) and **Edit in Odoo**; **My visits** lists the recent history. A teammate's visit (e.g. a manager tapping a team notification) opens as a read-only card.

Every tap writes a real record through the regular ORM methods, so access rights, GPS verification and Telegram notifications are exactly the same as in the backend views. The screen's code is loaded on demand (asset bundle `checkinme_sales_activity.assets_screen`), so the rest of Odoo is not slowed down.

**Install on phone**: *Sale Check > Install on phone* installs the screen on the phone as the **ABJ** app (see [ABJ mobile app](#abj-mobile-app) below). The screen has a stable address, `/odoo/abj` (a visit: `/odoo/abj/<check-in id>`), and opens full screen. From the backend, *Actions > Open in phone screen* on any check-in opens it in the screen.

## ABJ mobile app

The phone screen is what the salespeople install on their phones, under the name **ABJ** (icon: the ABJ wordmark on slate). There is nothing to download from a store for iPhones and most Android phones: the app is the Odoo page `/odoo/abj` installed as a web app. An optional Android app (a thin shell around the same page) exists for the cases listed below.

| | iPhone / iPad (iOS 16.4+) | Android, installed from Chrome | Android app "ABJ" (optional) |
|---|---|---|---|
| Install | Safari > Share > *Add to Home Screen* | Chrome > *Install* | APK built from [`mobile/`](../mobile/README.md) |
| Notifications | Web Push (Apple) | Web Push (Google) | Firebase Cloud Messaging |
| GPS and camera | Yes | Yes | Yes |
| Receipt printing | System print (AirPrint) only | Bluetooth printer (Web Serial / Web Bluetooth), system print | Paired Bluetooth printer, network printer (port 9100), Android print |
| Login | Same as in Safari | Same as in Chrome | Odoo login and password only (no Google or Telegram login) |

### Install on an iPhone

1. Open **Safari** (iOS 16.4 or later), log in to Odoo and open *Sale Check > Install on phone* (or go to `https://<your server>/scoped_app?app_id=checkinme_sales_activity&app_name=ABJ&path=odoo/abj`).
2. On that install page, tap **Share** > **Add to Home Screen** > **Add**. The ABJ icon appears on the Home Screen. (Adding another Odoo page to the Home Screen installs the main Odoo web app instead, "ABJ Care" on ABJ's server.)
3. Open **ABJ from the Home Screen** (not from Safari), tap the gear icon > *Notifications* > **Enable notifications** (or **Turn on** in the banner of the home screen) and allow them. iOS only delivers web notifications to apps added to the Home Screen; a Safari tab never receives them (its settings screen shows *Install first* and an **Install page** button).

### Install on Android

1. Open **Chrome**, log in to Odoo and open *Sale Check > Install on phone*, then tap **Install** (or Chrome menu > *Add to Home screen* > *Install*).
2. Open ABJ from the home screen, tap the gear icon > *Notifications* > **Enable notifications** (or **Turn on** in the home screen banner) and allow them.

The installed app runs in Chrome, so everything Chrome offers works in it (Bluetooth printing through Web Serial / Web Bluetooth, downloads, the same login options as Chrome).

The settings screen (gear icon) also shows how ABJ is running (*Web browser*, *Installed web app* or *Android app*), **Open full Odoo**, and **Log out**, which first unregisters this phone from notifications (see [Notifications](#notifications)).

### Optional Android app

The Android app is a small native shell (Capacitor) that opens the same `/odoo/abj` page and adds what a web page cannot do on its own: printing to paired Bluetooth Classic printers without Chrome's pickers, network receipt printers (TCP port 9100), the Android print service, and native push notifications through Firebase. Use it if you distribute phones with an MDM / the Play Store, or if Web Serial printing does not work with your printers. Build and signing instructions, Firebase setup for the build and printer pairing are in [`mobile/README.md`](../mobile/README.md).

* **"Sign in with Google" and Telegram login are not expected to work in the Android app**: they open external pages, outside the app's WebView (and Google blocks its sign-in page in embedded WebViews). Give these salespeople an Odoo password (or use the Chrome-installed app).
* Do not use the Android app on a phone where notifications are also enabled in Chrome for the same server (in the ABJ web app or any other Odoo page or web app of that server: they share one browser subscription): every notification would arrive twice. Keep only one of them.

### Notifications

| Notification | When | Who | Setting (Settings > Sale Check > ABJ Mobile App) |
|---|---|---|---|
| New visit planned | Someone else plans a visit for the salesperson, or assigns a planned visit to them | The salesperson | *Planned Visit Notifications* (on) |
| N follow-ups today | Every morning, for the visits whose follow-up date is today | The salesperson | *Morning Follow-up Reminders* (on) |
| Check-in / check-out | Each check-in / check-out of a team member | Their manager and department manager, if they may open the visit | *Team Check-in Notifications* (off: Telegram already covers the managers) |
| Odoo messages | Mentions, direct messages, assigned activities | The recipients who use the Android app | *Odoo Messages in the Android App* (on; browsers and the installed web app get them from Odoo itself) |

Tapping a notification opens the visit in ABJ (`/odoo/abj/<id>`; a teammate's visit opens read-only), the follow-up reminder opens the ABJ home screen with its *Follow-ups today* card, and an Odoo message opens the record or the Discuss conversation. Notifications are queued (`checkinme.push.message`) and sent by the scheduled action *Sale Check: Send Queued ABJ Push Notifications* right after the record is saved (and hourly as a safety net), so a slow push service never delays the salesperson.

**Push Logs** (*Sale Check > Reporting > Push Logs*, Sale Check managers) lists every notification with the number of devices reached and the errors returned by the push services; **Send Again** resends a failed or partially sent one. Copies of Odoo messages forwarded to the Android app (event *Odoo Message*) are private: only administrators (*Administration > Settings* rights) see them, and their text is removed once delivered. Expired browser subscriptions are removed and dead Android app tokens are archived automatically.

**ABJ App Devices** (*Sale Check > Configuration > ABJ App Devices*) lists the phones running the Android app (at most 10 active phones per user). Archive a lost phone there: it stays archived (*Archived by a Manager*) even when its user opens the app again, until a manager unarchives it or another user logs in on that phone. **Log out** in ABJ's settings screen (and, in the Android app, Odoo's own user menu > *Log out*) unregisters the phone first, so the next user of a shared phone does not receive the previous user's notifications.

To check a phone, use **Send test notification** in ABJ's settings screen (shown only where this phone can receive notifications): it is sent to this phone only and the answer says whether *this device* was reached. **Send a test notification to me** in *Settings > Sale Check > ABJ Mobile App* sends one to every registered device of the current user and reports how many browser / PWA and Android app devices were reached. The reasons of a failure are only in Push Logs.

The morning reminder runs at 01:00 UTC (08:00 in Phnom Penh). For another timezone, change *Next Execution Date* of *Sale Check: ABJ Follow-up Reminders* in *Settings > Technical > Scheduled Actions*.

### Printing receipts

On the result of a visit, **Print slip** prints the visit slip (customer, times, location check, outcome, follow-up, notes, a QR code to the location) and **Print &lt;order&gt;** prints a quotation / sales order taken during the visit (cancelled orders are not offered); *My visits* has a print button on each row. Without a printer, these places show *Set up printer* instead. The receipt is drawn on the phone as an image, because the text mode of receipt printers (ESC/POS) cannot print Khmer; any 58 mm (384 dots) or 80 mm (576 dots) ESC/POS thermal printer that supports raster images works. Its labels and dates follow the salesperson's Odoo language (*My Preferences*); the module ships no translation, so the labels stay in English until one is added, while Khmer names and notes print with the bundled Noto Sans Khmer font.

Set the printer once per phone (the settings are stored on the phone) in ABJ > gear icon > *Printer*: the connection, the printer, the paper width (58 / 80 mm), *Cut the paper after printing* and *Copies* (receipt printers only) and *Print the visit slip after each check-out*; then **Print test page**. Only the connections available on the phone are offered:

* **Android, installed from Chrome**: pair the printer first in Android *Settings > Bluetooth*, then choose *Bluetooth / USB (Chrome)* and **Choose printer** (Web Serial, Chrome 138 or later; allow Chrome the *Nearby devices* permission; *Printer not listed? Show all devices* removes the filter). Printers that only speak Bluetooth Low Energy use *Bluetooth LE (Chrome)*; Chrome asks to pick the printer again after each restart of the app. *System print* uses the Android print services (Mopria, *Save as PDF*).
* **iPhone**: Safari cannot talk to Bluetooth printers. Only *System print* (AirPrint printers, or *Save to Files*) is available.
* **Android app**: pair the printer in Android settings, then choose *Bluetooth printer* and tap it under *Paired printers* (Bluetooth Classic only; *Refresh the list* after pairing a new one); *Network printer* takes the printer's IP address and port (9100); *System print* uses the Android print services. A printer that stops accepting data (paper out, cover open) ends the job with "The printer stopped accepting data. Check its paper and cover, then print again." instead of blocking the app.

### Firebase (Android app notifications only)

The web app needs no setup: Odoo's mail module creates its Web Push keys the first time a user enables notifications. For the Android app:

1. In the [Firebase console](https://console.firebase.google.com/), create a project and add an Android app with the application id of the build (`com.abj.app` by default). Its `google-services.json` is used by the build only (see [`mobile/README.md`](../mobile/README.md)); never upload it to Odoo.
2. *Project settings > Service accounts > Generate new private key* downloads a JSON key (or create a dedicated service account with the narrower *Firebase Cloud Messaging API Admin* role, see [`mobile/README.md`](../mobile/README.md#firebase-push-notifications)).
3. In Odoo, *Settings > Sale Check > ABJ Mobile App > Android App Notifications (Firebase)* (administrators only): upload this JSON key (*Upload Key (JSON)*) and save. *Firebase Project* and *Service Account* are then displayed; the key itself is never shown again (it is stored in the system parameter `checkinme.fcm_service_account`, readable by administrators only). **Remove Service Account** deletes it.
4. Click **Send a test notification to me** with the Android app installed and logged in.

To rotate the key, upload the new one, then delete the old key in the Google Cloud console (*IAM > Service accounts > Keys*).

### Deployment checklist

* Odoo is self-hosted or on Odoo.sh: custom modules cannot be installed on Odoo Online.
* Public **HTTPS** address: set `web.base.url` to `https://...` and add the system parameter `web.base.url.freeze` = `True` (Apple refuses Web Push from a server that does not identify itself with an https URL, and notification links are built on it). Behind a reverse proxy, start Odoo with `proxy_mode = True`.
* Outbound HTTPS from the Odoo server to `*.push.apple.com` (iPhones), `fcm.googleapis.com` (Chrome on Android and the Android app) and `oauth2.googleapis.com` (Android app).
* At least one cron worker (`max_cron_threads >= 1`, the default): notifications and reminders are sent by scheduled actions.
* **Never delete the system parameters `mail.web_push_vapid_private_key` / `mail.web_push_vapid_public_key`**: every phone would silently stop receiving notifications until its user enables them again.

### Production notes (ABJ server)

* Odoo 18 **Enterprise**, self-hosted at <https://abjskincare.com>; `web.base.url` is already set to it and frozen.
* Sale Check is installed there at version 18.0.1.0.0; this version is 18.0.1.1.0. Upgrade with `-u checkinme_sales_activity` (e.g. `odoo-bin -d <db> -u checkinme_sales_activity --stop-after-init`), then restart Odoo so the assets (phone screen, service worker) are rebuilt.
* The Web Push VAPID keys already exist: do not delete them (see above).
* Installed there and **not** covered by this module's tests: the Enterprise modules `web_mobile`, `mail_mobile` (push for Odoo's own mobile app) and `social_push_notifications`, and the custom `auth_telegram` (Telegram login). This work was tested on Odoo 18 **Community** only: upgrade a staging copy of the production database first and check login, the phone screen, notifications and printing there.
* Telegram login (`auth_telegram`) and Google login open external pages and are not expected to work inside the optional Android app's WebView: those users need an Odoo password there. The PWA in Chrome / Safari is not affected as far as we know, but verify it on staging.
* The main Odoo PWA is named "ABJ Care" (scope `/odoo`); the new phone app "ABJ" has the scope `/odoo/abj`. Installing both on a phone is fine.
* Phones that installed the earlier "Sale Check" app (old address `/odoo/action-...`) must uninstall it and install ABJ again from *Sale Check > Install on phone*.

### Known limitations

* iPhones need iOS 16.4 or later and the Home Screen app; removing the app or clearing Safari data removes the subscription (enable notifications again).
* Not verified on a real iPhone: opening the visit when a notification is tapped (the app may open on its home page instead) and printing from the Home Screen app (if the print dialog does not open, the receipt image opens in a new page to print or share it).
* Receipt printing was checked against the specifications, Chrome's source code and simulated printers, never with a real printer: cheap printers differ, test yours with *Print test page*. Printers that only speak Bluetooth LE must be picked again after each restart.
* Push services keep an undelivered notification for a limited time (24 hours, 12 hours for the morning reminder, 4 hours for team check-ins, 1 hour for tests): a phone switched off longer does not get it. Battery savers may delay notifications.
* Odoo's own notifications to browsers and the installed web app (chat, mentions) are unchanged: they come from the mail module.

### What was and was not tested

Tested:

* The module's automated test suite on Odoo 18 **Community**: 185 tests, 0 failures (Telegram, Web Push, Google OAuth and FCM calls mocked; one headless Chrome test opens `/odoo/abj`).
* End-to-end runs of the phone screen in headless Chromium, with simulated printers, simulated push services and a simulated Capacitor bridge (Android app).
* The Android APK, built by GitHub Actions.

Not tested:

* A real iPhone, or a real Android phone (Chrome-installed app or APK).
* Real thermal printers, over any connection: Web Serial, Web Bluetooth, Bluetooth SPP (Android app) or TCP port 9100.
* Delivery through a real Firebase project.
* Odoo **Enterprise**, including the ABJ production server (see [Production notes](#production-notes-abj-server)).

## Using it on a phone

1. Open Odoo in the phone browser or the Odoo mobile app and go to **Sale Check > Check-ins > Check In Now**.
2. The GPS position is captured automatically (allow location access when asked). Use **Capture GPS** to retry.
3. Pick the customer and activity type, add the purpose, notes and a photo, then **Save**. Saving a *Checked In* record posts a note in the chatter and notifies the managers on Telegram.
4. When leaving, open the visit and press **Check Out**: the position is captured at that moment and the visit duration is recorded.
5. Managers can plan visits for the team in **Planned Visits**; the salesperson opens the planned visit on site and presses **Check In**, which captures the position right then (a check-in is refused without a GPS position when *Require GPS* is on).

## Reports

* **Sale Check > Reporting > Performance Analysis**: pivot / graph on check-ins and confirmed sales orders, grouped by salesperson and by day, week, month, quarter or year.
* **Sale Check > Reporting > Activity Report**: pick a period (today, this week, last month, custom...) and salespeople, then print the PDF, open the analysis, or send the summary to Telegram.
* **Targets & KPI**: kanban / list with progress bars; each target shows the month's check-ins and confirmed orders.
* Print a **Visit Sheet** PDF from any check-in (Print menu).

## Technical notes

| Model | Purpose |
|---|---|
| `checkinme.checkin` | Sales check-in / visit (chatter, activities). Computes check-in day in the employee timezone, Google Maps URLs, distance to the customer (haversine) and the location status. |
| `checkinme.activity.type` | Configurable activity types (`counts_as_visit`, `requires_customer`). |
| `checkinme.target` | Monthly KPI target per employee with real-time actuals and achievement. |
| `checkinme.performance.report` | SQL report (inlined `_table_query`) combining check-ins and confirmed sales orders per employee and day. |
| `checkinme.telegram` | Telegram Bot API service (`sendMessage`, `sendLocation`, `sendPhoto`, `getMe`), message formatting, scheduled reports. Never raises towards the user; every call is logged in `checkinme.telegram.log`. |
| `checkinme.report.wizard` | Period / employee selection for the PDF report, analysis and Telegram sending. |
| `checkinme.checkin` (`models/checkinme_mobile.py`) | `mobile_get_dashboard`, `mobile_search_customers`, `mobile_create_customer`, `mobile_checkin`, `mobile_checkout`, `mobile_get_checkin`, `mobile_get_history`, `mobile_get_receipt`, `mobile_send_test_push` (this device only, given its push endpoint or FCM token): the RPC entry points of the phone screen (client action `checkinme_sales_activity.screen`, path `abj`). |
| `checkinme.push` | Push notification service: `_notify(users, title, body, url, event, record)` queues a notification (never raises, nothing queued without a device), `_cron_send_push_queue` sends it by Web Push (`tools/web_push.py`, mail's VAPID keys and browser subscriptions, with a real TTL and high urgency) and FCM HTTP v1 (`tools/fcm.py`, OAuth2 service-account flow without extra dependency). |
| `checkinme.push.message` | Queue and log of the notifications (kept 30 days). Record rules: managers do not see the copies of Odoo messages (`event = 'odoo_message'`), administrators see everything. |
| `checkinme.push.device` | Firebase registration tokens of the Android app (`register_device` / `unregister_device`, called by the app). From the backend a device can only be archived or unarchived; archiving sets `blocked`, which `register_device` respects for the same user. |
| `mail.thread` (`models/mail_thread.py`) | Copies Odoo's own web push notifications to the Android app devices. |
| `controllers/webmanifest.py` | ABJ name, colors and PNG icons in the scoped app manifest (`/web/manifest.scoped_app_manifest?app_id=checkinme_sales_activity`), the iOS Home Screen icon, and the ABJ service worker snippet (`static/src/sw/abj_service_worker.js`) prepended to `/web/service-worker.js`. |

System parameters: `checkinme.telegram_bot_token`, `checkinme.telegram_chat_id`, `checkinme.notify_checkin`, `checkinme.notify_checkout`, `checkinme.send_location_pin`, `checkinme.send_photo`, `checkinme.daily_report`, `checkinme.weekly_report`, `checkinme.monthly_report`, `checkinme.require_gps`, `checkinme.max_distance_m`, `checkinme.sales_source`, `checkinme.push_planned_visit`, `checkinme.push_followup_reminder`, `checkinme.push_team_checkin`, `checkinme.push_odoo_messages`, `checkinme.fcm_service_account`.

Extensions: `hr.employee` (Telegram chat id, smart buttons), `res.partner` (visit count, last visit, smart button), `sale.order` (`checkinme_checkin_id`).

The GPS capture is an OWL field widget (`checkinme_geolocation`) using the browser Geolocation API; it therefore needs HTTPS (or localhost) to work in modern browsers.

## Tests

```bash
odoo-bin -d <db> -i checkinme_sales_activity --test-enable --test-tags /checkinme_sales_activity --stop-after-init
```

All Telegram, Web Push, Google OAuth and FCM HTTP calls are mocked in the test suite (the Web Push payloads are decrypted and the signed tokens verified with throwaway keys). The ABJ web app tests (`test_abj_controllers.py`) use `HttpCase` and need Chrome / Chromium for the browser test. See [What was and was not tested](#what-was-and-was-not-tested) for the scope of the tests.

## License

LGPL-3. Author: Reach2trillion.
