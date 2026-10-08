# o8o - Odoo 18 custom modules (Reach2trillion)

| Module | Description |
|---|---|
| [`checkinme_sales_activity`](checkinme_sales_activity/README.md) | CheckinMe Sales Activity System: GPS check-ins linked to Google Maps, customer / meeting tracking, monthly KPI targets, Telegram notifications and reports, daily / weekly / monthly / yearly performance reports for outside sales teams. |
| [`delivery_label_cod_kh`](delivery_label_cod_kh/README.md) | Cambodia COD Delivery Label 100 x 80 mm: thermal-friendly bilingual Khmer / English shipping label with the amount to collect (USD + riel) or PAID / NO COD from the sales order, "scan to pay" QR (Odoo payment link, ABA KHQR image or custom URL), receiver phone extracted from messy contacts, province box, map QR, parcels 1/N and a Code128 barcode. |

## Development

```bash
# run the test suite of a module against a local Odoo 18 checkout
odoo-bin -d test_db --addons-path=<odoo>/addons,<this repo> \
  -i checkinme_sales_activity --test-enable --test-tags /checkinme_sales_activity --stop-after-init
odoo-bin -d test_db --addons-path=<odoo>/addons,<this repo> \
  -i delivery_label_cod_kh --test-enable --test-tags /delivery_label_cod_kh --stop-after-init
```
