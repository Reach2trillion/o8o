# CheckinMe Sales Activity (Odoo 18)

Mobile-friendly sales activity management and tracking for outside sales teams.

| Feature | What it does |
|---|---|
| **GPS Check-ins** | Salespeople check in at the customer's site from their phone. The position is captured with the device GPS, linked to Google Maps and compared with the customer's geolocation to mark the visit as *Verified on site* or *Far from customer*. |
| **Customer Management** | Every check-in is tied to a customer, an activity type (visit, meeting, demo, delivery, collection, call...), a time, a location, meeting notes, an outcome, a follow-up date, a sales amount and a photo. Quotations can be created straight from the visit. |
| **KPI Tracking** | Monthly targets per salesperson (visits, new customers, orders, sales amount) with real-time actuals, achievement percentages, expected progress and an *On track / At risk / Behind / Achieved* status. |
| **Telegram Integration** | Instant check-in / check-out notifications (message, location pin, photo) to the managers' Telegram group and to the salesperson's manager, plus automatic daily, weekly and monthly activity reports. |
| **Performance Reports** | Daily, weekly, monthly and yearly results per employee in pivot / graph views (visits, verified visits, new customers, orders, sales amount), a printable PDF activity report and a per-visit "Visit Sheet". |

## Requirements

* Odoo 18.0 (Community or Enterprise).
* Dependencies (all standard): `base`, `mail`, `hr`, `sale`, `base_geolocalize`.
* Outbound HTTPS access from the Odoo server to `api.telegram.org` for the Telegram features.

## Installation

1. Copy `checkinme_sales_activity` into your addons path.
2. Update the apps list and install **CheckinMe Sales Activity**.
3. Give users one of the two access levels (Settings > Users > *CheckinMe Sales Activity*):
   * **Salesperson** - creates and edits own check-ins (planned visits may be deleted), reads the check-ins, targets and reports of direct reports.
   * **Manager** - full access, targets, activity types, Telegram settings and logs.
4. Make sure every salesperson has an **Employee** record linked to their user (Employees app). Check-ins, targets and reports are attached to the employee; the employee's timezone is used to compute the check-in day.

## Configuration (Settings > CheckinMe)

Like every Odoo settings page, this one is only available to users with *Administration > Settings* rights (the CheckinMe **Manager** group alone is not enough to open Settings).

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the **bot token**.
2. Add the bot to the managers' group (or channel) and make sure it may post there.
3. Find the **chat id** of the group (e.g. add [@userinfobot](https://t.me/userinfobot) / [@getidsbot](https://t.me/getidsbot) to the group, or read it from `https://api.telegram.org/bot<TOKEN>/getUpdates`). Group ids are negative numbers such as `-1001234567890`.
4. Enter both values in *Settings > CheckinMe > Telegram Integration* and click **Test Connection**.
5. Optional: give managers a personal chat id on their employee form (Employees app > employee > *HR Settings* tab > CheckinMe > *Telegram Chat ID*; editing it requires HR *Officer* rights). When an employee checks in, the notification is sent to the managers' group **and** to the personal chat of the employee's manager and department manager. If your users already have a *Telegram Group ID* on their user record (from another Telegram module), it is used as a fallback.

Check-in and check-out notifications are queued and delivered by the scheduled action *CheckinMe: Send Queued Telegram Notifications* a few seconds after the record is saved, so a slow or unreachable Telegram never delays the salesperson; make sure the Odoo cron worker is running (it is on any standard deployment).

Notification toggles: check-in, check-out, location pin, photo. Automatic reports: daily at 18:00, weekly on Monday at 09:00 (previous week) and monthly on the 1st at 09:00 (previous month), in the timezone of the main company at installation time. The schedules are ordinary scheduled actions (*Settings > Technical > Scheduled Actions > CheckinMe: ...*) and can be changed there.

If another Telegram module already stores a bot token under `send_by_telegram.bot_token` or `abj.telegram.bot_token`, it is used as a fallback when no CheckinMe token is set.

### GPS verification

* **Require GPS position to check in** (default on): a check-in cannot be saved without a captured position.
* **Maximum distance** (default 500 m): a visit is *Verified on site* when the check-in position is within this distance of the customer's geolocation. Geolocate customers on the contact form: *Partner Assignment* tab > Geolocation > **Compute based on address** (provided by `base_geolocalize`, uses OpenStreetMap or Google Maps as configured in General Settings), or fill *Geo Latitude / Geo Longitude* manually. Use *Recompute Location Check* on the check-in list after geolocating customers retroactively.

### KPI

* **Sales results source**: measure sales against targets from confirmed sales orders of the salesperson (default) or from the *Sales Amount* entered on check-ins.

## Using it on a phone

1. Open Odoo in the phone browser or the Odoo mobile app and go to **CheckinMe > Check-ins > Check In Now**.
2. The GPS position is captured automatically (allow location access when asked). Use **Capture GPS** to retry.
3. Pick the customer and activity type, add the purpose, notes and a photo, then **Save**. Saving a *Checked In* record posts a note in the chatter and notifies the managers on Telegram.
4. When leaving, open the visit and press **Check Out**: the position is captured at that moment and the visit duration is recorded.
5. Managers can plan visits for the team in **Planned Visits**; the salesperson opens the planned visit on site and presses **Check In**, which captures the position right then (a check-in is refused without a GPS position when *Require GPS* is on).

## Reports

* **CheckinMe > Reporting > Performance Analysis**: pivot / graph on check-ins and confirmed sales orders, grouped by salesperson and by day, week, month, quarter or year.
* **CheckinMe > Reporting > Activity Report**: pick a period (today, this week, last month, custom...) and salespeople, then print the PDF, open the analysis, or send the summary to Telegram.
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

System parameters: `checkinme.telegram_bot_token`, `checkinme.telegram_chat_id`, `checkinme.notify_checkin`, `checkinme.notify_checkout`, `checkinme.send_location_pin`, `checkinme.send_photo`, `checkinme.daily_report`, `checkinme.weekly_report`, `checkinme.monthly_report`, `checkinme.require_gps`, `checkinme.max_distance_m`, `checkinme.sales_source`.

Extensions: `hr.employee` (Telegram chat id, smart buttons), `res.partner` (visit count, last visit, smart button), `sale.order` (`checkinme_checkin_id`).

The GPS capture is an OWL field widget (`checkinme_geolocation`) using the browser Geolocation API; it therefore needs HTTPS (or localhost) to work in modern browsers.

## Tests

```bash
odoo-bin -d <db> -i checkinme_sales_activity --test-enable --test-tags /checkinme_sales_activity --stop-after-init
```

All Telegram HTTP calls are mocked in the test suite.

## License

LGPL-3. Author: Reach2trillion.
