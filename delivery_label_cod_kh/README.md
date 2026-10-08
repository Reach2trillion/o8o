# Cambodia COD Delivery Label 100 x 80 mm (Odoo 18)

A clean, high-contrast, thermal-printer friendly **100 mm wide x 80 mm tall** shipping label for
outgoing transfers, built for Cambodian **Cash-on-Delivery (COD)** couriers. It shows the rider
how much money to collect (USD and riel) or a clear **PAID / DO NOT COLLECT**, who to call, where
to go, and lets the customer **scan to pay** instead of paying cash.
Bilingual Khmer / English, black on white only (no colours, gradients or grey: thermal printers
dither them).

| Section | What it shows |
|---|---|
| **Header** | Company logo (converted to grayscale), name, optional tagline, phone (`031 266 3333`) / transfer reference, sales order, date / courier, parcel `1/3`, weight |
| **Receiver** | Name (phone numbers typed in the name are removed), **phone in 18 pt** (or a blank line to fill by hand), address (street, street 2, city), **province box** (`ខេត្តកណ្តាល`) |
| **Payment** (the anchor) | **COD**: black box, amount ~25 pt, `≈ 69,000៛`, "Goods $15.00 + Delivery $2.00", "Total for 3 parcels, collect once", **"SCAN TO PAY $17.00 ▶"** + payment QR. **PAID**: thick frame "PAID · DO NOT COLLECT". **NO COD**: thin frame |
| **Items** | `ទំនិញ Items (ចំនួន Qty 3): 2× Serum 30ml, 1× Cream … +2 more`, note, chips "Allow check" / "FRAGILE" |
| **Footer** | Code128 barcode of the transfer (Barcode app) and the thank-you text |

One label per parcel (`kh_parcel_count`), one PDF page per label, no blank page when printing
several transfers at once.

## Requirements

* Odoo 18.0 (Community or Enterprise). Dependencies: `sale_stock`, `stock_delivery` (standard).
* **wkhtmltopdf 0.12.6 with patched Qt** (the version recommended by Odoo).
* **Khmer fonts on the Odoo server** (the PDF is rendered on the server, not in the browser):

  ```bash
  sudo apt-get install fonts-khmeros fonts-noto-core   # Khmer OS Battambang / Siemreap, Noto Sans Khmer
  fc-cache -f
  ```

  Without them Khmer text prints as empty boxes ("tofu"). The label uses
  `Arial / Liberation Sans` for Latin text and digits and `Khmer OS Battambang`, `Khmer OS Siemreap`,
  `Noto Sans Khmer`, `Khmer OS` for Khmer.
* QR codes and barcodes are drawn server side by `ir.actions.report.barcode()` (reportlab). When
  reportlab has no bitmap backend (reportlab 4 from pip without `rlPyCairo`), the module draws them
  itself with `python-qrcode` (an Odoo requirement) and Pillow: pixel-exact black and white PNGs.

## Installation

1. Copy `delivery_label_cod_kh` into your addons path, update the apps list and install
   **Cambodia COD Delivery Label (100x80)**.
2. Configure *Inventory > Configuration > Settings > COD Delivery Label* (below).
3. Print a test label (see *Printer setup*) before using it for real parcels.

## Configuration (Inventory > Settings > COD Delivery Label)

All settings are per company.

| Setting | Default | Meaning |
|---|---|---|
| **Riel conversion** | Odoo currency rate | *Odoo currency rate*: the rate of the KHR currency (Accounting > Currencies); falls back to the **fixed rate** when KHR is inactive or has no rate. *Fixed rate*: riel per 1 USD. |
| Fixed rate | 4100 | Riel per 1 USD (fixed source, or fallback). |
| Round riel to | 100 | Half-up rounding step: `$17 x 4061.32 = 69,042.44 -> 69,000៛`. 0 or negative = 1 riel. |
| Print riel equivalent | on | `≈ 69,000៛` next to a dollar amount, `≈ $16.74` next to a riel amount. Never on PAID / NO COD labels. |
| **Payment QR code (COD)** | Odoo payment link | See *Payment QR* below: *Odoo payment link of the sales order*, *Static ABA KHQR image*, *Custom URL* or *No payment QR*. |
| ABA KHQR image | - | Static KHQR of the shop (square QR, max 512 px), for the *Static ABA KHQR image* mode and as fallback of the Odoo link. |
| Payment URL template | - | For *Custom URL*, e.g. `https://pay.example.com/?ref={order}&amount={amount}&ccy={currency}`. |
| **Label info QR code** | Receiver location | *Receiver location*: Google Maps link `https://maps.google.com/?q=<lat>,<lng>` of the contact's geolocation (the transfer reference when the contact has none); *Transfer reference*; *Nothing*. |
| Print item summary | on | List the products (disable it for discreet parcels). |
| Products listed | 3 | Products listed before `+N more`. |
| Label tagline / footer | - / thank-you text | Free texts (translatable). |
| Delivery order defaults | allow check on, fragile off | Defaults of the handling flags of new delivery orders. |

The settings are real company fields: unticking a setting that defaults to on is saved (unlike
`config_parameter` booleans).

## Using it

* **Delivery order > COD Label tab** (outgoing transfers only): COD mode, the amount *Auto* would
  print, the manual amount, the resulting status and amount, the **payment link** (copy button,
  to send it by Telegram / SMS), receiver phone, number of parcels, fragile / allow check, label note.
  A warning is shown when the automatic amount cannot be used (see the ambiguity guard below).
* **COD Label** button in the header of delivery orders, or **Print > COD Label 100x80** on one or
  many transfers (list view: select, Print). Each transfer prints *Parcels* labels (`1/N ... N/N`).
* Transfer list: optional **COD** column (amount to collect).

### Receiver phone

Taken from the delivery contact's phone, mobile, then numbers typed in the contact **name**
(`"Andyyvathhh 095634706"` prints *Andyyvathhh* / *095 634 706*), else from the company contact.
Cambodian numbers (`+855`, `855`, `0...`, with spaces / dots / dashes) are printed in local format
`0XX XXX XXX` / `0XX XXX XXXX`; anything else is printed as typed; at most two numbers. The field
is editable on the transfer; it is recomputed when the contact's name / phone / mobile changes.

## COD rules (money is never guessed)

| Case | Label |
|---|---|
| Not an outgoing transfer, or a return | NO COD |
| Point of Sale order | PAID (paid at the till) |
| No sales order (mode Auto) | NO COD |
| Sales order | `due = order total - paid`, PAID when `due <= 0` |
| Mode **Manual amount** | that amount (0 = PAID) |
| Mode **Paid - do not collect** | PAID |

`paid` = paid part of the **posted** customer invoices of the order (`total - residual`), minus the
same for posted credit notes, converted to the order currency; or the order's online payment
transactions (`amount_paid`) when higher (`max`, not a sum: a transaction reconciled with an
invoice is not counted twice). Draft invoices count as unpaid. The amount is in the currency of the
sales order; the fee breakdown is the total of the delivery lines.

**Ambiguity guard**: when the automatic amount is due and the sales order has **more than one**
non-cancelled delivery (backorder, partial delivery), printing is refused with the list of the
deliveries: set the mode to *Manual amount* or *Paid* on each of them. Validating transfers is never
blocked. Internal pick / pack transfers of multi-step deliveries do not count.

**Limitations**

* A payment registered **without being reconciled** with the invoice of the order is not seen: the
  label still shows the amount. Use *Paid - do not collect* (or a manual amount) in that case.
* Payments on another document than the order's invoices (e.g. a deposit on the customer account)
  are not seen either.
* The riel amount is an indication (rate of the day, rounded); the amount to collect is the one in
  the order currency.

## Payment QR ("scan to pay")

Printed **only on COD labels** with an amount to collect, in a white square next to the amount
(white quiet zone of about 2 mm, also next to the black box), 18 mm wide, with the caption
`ស្កេនដើម្បីទូទាត់ · SCAN TO PAY $17.00 ▶`. Never on PAID / NO COD labels.

| Mode | QR content |
|---|---|
| **Odoo payment link** (default) | The standard payment link of the sales order, built with Odoo's *Generate a Payment Link* wizard: `{base URL}/payment/pay?amount=17.0&access_token=...&sale_order_id=42`, for **exactly the amount printed** (order currency). The customer opens the Odoo payment page of the order and pays with any **published payment provider** (e.g. ABA PayWay "ABA KHQR"). The transaction is linked to the order: once it is done, `amount_paid` rises and a re-printed label says PAID. The link works for confirmed orders (the controller only checks the token). Transfers **without sales order** (manual amount) print the static KHQR image when one is set, else no payment QR. |
| **Static ABA KHQR image** | The uploaded image; the customer types the amount shown in the caption (`ABA KHQR · SCAN TO PAY $17.00`). Upload the square QR itself (crop the ABA card), it prints 18 mm wide. |
| **Custom URL** | The URL template with `{order}`, `{picking}`, `{amount}` (`17.00`), `{currency}` (`USD`), `{partner}` (receiver name), each URL-encoded. Unknown placeholders and malformed braces are left as typed (never an error). Empty template = no payment QR. |
| **No payment QR** | - |

The same link is shown on the transfer (*COD Label* tab > *Payment Link*, copy button).

Setup checklist for the Odoo link:

* The system parameter `web.base.url` must be the **public address** of the database (e.g.
  `https://abjskincare.com`, frozen with `web.base.url.freeze = True`); with the *Website* app the
  domain of the order's (or company's) website is used when one is set.
* At least one payment provider enabled **and published** for the company and the currency of the
  orders (*Invoicing > Configuration > Payment Providers*, or the *Payment Providers* link of the
  setting).
* Links are signed with the database secret: they stay valid after a database restore / copy, and
  can be built outside of an HTTP request (PDF rendered by a scheduled action, the shell, tests).
  The module makes `payment.link.wizard` compute the very same token there (Odoo's helper reads it
  from the HTTP request only).

**Placement of the info QR**: the payment QR takes the QR slot of the payment section. On those
COD labels the **map** QR moves to the receiver section (next to the province box, caption
`ស្កេនមើលទីតាំង MAP`; name and address get a narrower column), and a **transfer reference** QR is
**omitted** (the Code128 barcode of the footer carries the same reference, and the receiver texts
keep their full width). PAID / NO COD labels keep the info QR in the payment section. Override
`stock.picking._kh_info_qr_place()` to change this rule.

**Limitations**

* The QR of a label that was already handed over **stays valid after the order is paid**: Odoo's
  `/payment/pay` page only checks the signed amount, not what is left to pay, so scanning an old
  label again could charge twice. Re-printed labels are correct (PAID, no payment QR). Mitigation:
  give riders the rule "PAID labels: never collect", refund duplicates from the transaction, or
  restrict the payment page to the amount still due with a custom controller override.
* Payment links carry a 64-character token: the QR has about 45 x 45 modules (error correction L,
  0.4 mm per module at 18 mm). It was verified to decode at 203 dpi; do not print the label scaled
  down.
* The static KHQR mode cannot pre-fill the amount (the customer types it).

## Printer setup

* Media / label size **100 mm x 80 mm** (width x height), the paper format of the report is
  *COD Label 100 x 80 mm* (custom 100 x 80, margins 0, 96 dpi, smart shrinking disabled).
* Print **at 100 % / "Actual size"** (never "Fit to page" / "Shrink to fit"), orientation as the
  page (landscape label, the PDF page is already 100 mm wide and 80 mm tall).
* Thermal printers: 203 dpi or better, direct thermal or transfer, darkness medium-high. The label
  is pure black and white, so no dithering setting is needed.
* Test with a COD label that has a payment QR: scan it with a phone before printing in bulk.

## Migration from the old labels

1. Install this module, configure it and print test labels of real orders (COD, paid, no COD).
2. When validated, stop using the Softhealer report *Shipping Label 100x80*
   (`sh_receipt_reports.sh_rr_delivery_80x100`): it was hand-edited inside the third-party module
   (hard-coded address and phone, purple gradients, no COD amount) and those edits are lost at the
   next update of that module. Remove it from the Print menu if you wish (*Settings > Technical >
   Actions > Reports*), do not edit it.
3. Uninstall `stock_shipping_label` (MRDIL ODOO) once nobody uses it.

These steps are deliberately manual: the module does not touch the old modules.

## Technical notes

* Values are computed in Python (`stock.picking._kh_get_label_values()`, one dict per parcel);
  the report model `report.delivery_label_cod_kh.report_cod_label` also runs the ambiguity guard,
  so the Print menu is protected like the button. The QWeb template is split in inheritable
  sections (`report_cod_label_head`, `_receiver`, `_payment`, `_items`, `_foot`, `_style`).
* wkhtmltopdf 0.12 (old WebKit) has no flexbox / grid / CSS variables: the layout uses fixed-layout
  tables with absolute millimetre heights and clipped boxes; long texts are measured and truncated
  server side (`models/kh_label_text.py`, calibrated widths of Latin and Khmer glyphs, cuts never
  split a Khmer syllable), font sizes are chosen per label (long names / amounts get smaller).
* Hooks: `_kh_label_courier()` (courier name, e.g. from a Studio field), `_kh_info_qr_place()`,
  `_kh_get_payment_url()`, `_kh_custom_payment_url()`.
* Tests: `odoo-bin -d <db> -i delivery_label_cod_kh --test-enable --test-tags /delivery_label_cod_kh
  --stop-after-init` (COD rules, riel, phones, label values, payment QR / token, HTTP payment page,
  real PDF page count and size).
