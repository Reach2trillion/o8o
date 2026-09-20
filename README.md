# o8o - Odoo 18 custom modules (Reach2trillion)

| Module | Description |
|---|---|
| [`checkinme_sales_activity`](checkinme_sales_activity/README.md) | CheckinMe Sales Activity System: GPS check-ins linked to Google Maps, customer / meeting tracking, monthly KPI targets, Telegram notifications and reports, daily / weekly / monthly / yearly performance reports for outside sales teams. |

## Development

```bash
# run the test suite of a module against a local Odoo 18 checkout
odoo-bin -d test_db --addons-path=<odoo>/addons,<this repo> \
  -i checkinme_sales_activity --test-enable --test-tags /checkinme_sales_activity --stop-after-init
```
