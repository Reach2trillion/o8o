# o8o - Odoo 18 custom modules (Reach2trillion)

| Module | Description |
|---|---|
| [`checkinme_sales_activity`](checkinme_sales_activity/README.md) | **Sale Check** sales activity system: GPS check-ins linked to Google Maps, customer / meeting tracking, monthly KPI targets, Telegram notifications and reports, daily / weekly / monthly / yearly performance reports for outside sales teams. Its phone screen installs as the **ABJ** app (web app on iPhone and Android, push notifications, receipt printing). |

| Directory | Description |
|---|---|
| [`mobile/`](mobile/README.md) | Optional **ABJ** Android app: a Capacitor shell around the Odoo page `/odoo/abj` with native Bluetooth / network receipt printing and Firebase push notifications. Built by GitHub Actions (`.github/workflows/abj-android.yml`); set the repository variable `ABJ_SERVER_URL` (`https://abjskincare.com` for ABJ) first, otherwise the APK opens a placeholder server. |

Tested on Odoo 18 Community only (automated tests, headless Chromium, CI build of the APK); not on real phones,
real printers or Odoo Enterprise. See [What was and was not tested](checkinme_sales_activity/README.md#what-was-and-was-not-tested)
and the [production notes for the ABJ server](checkinme_sales_activity/README.md#production-notes-abj-server).

## Development

```bash
# run the test suite of a module against a local Odoo 18 checkout
odoo-bin -d test_db --addons-path=<odoo>/addons,<this repo> \
  -i checkinme_sales_activity --test-enable --test-tags /checkinme_sales_activity --stop-after-init
```
