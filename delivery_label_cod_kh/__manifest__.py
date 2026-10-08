# -*- coding: utf-8 -*-
{
    'name': 'Cambodia COD Delivery Label (100x80)',
    'summary': 'High-contrast 100 x 80 mm delivery label for Cambodian Cash-on-Delivery couriers '
               '(amount to collect in USD and KHR, receiver phone, province, QR, barcode)',
    'description': """
Cambodia COD Delivery Label (100 x 80 mm)
=========================================
A clean, thermal-printer friendly shipping label for outgoing transfers, built for
Cambodian Cash-on-Delivery (COD) couriers. Bilingual Khmer / English, black on white only.

* Money block as the visual anchor: amount to collect (USD and riel equivalent) or a clear
  PAID / DO NOT COLLECT state, computed from the sales order invoices and payments, with a
  manual override on the transfer and a guard against splitting one order over several
  deliveries.
* Receiver name, callable phone (also extracted from the contact name when staff typed it
  there), address and province box; sender, courier, parcel x/N and weight.
* Items summary, "allow check" / "fragile" handling chips and a free note.
* QR code (Google Maps location of the receiver, transfer reference or the shop's ABA KHQR)
  and a Code128 barcode of the transfer reference for the Barcode app.
* One label per parcel, one 100 x 80 mm page per label.
""",
    'version': '18.0.1.0.0',
    'category': 'Inventory/Delivery',
    'author': 'Reach2trillion',
    'website': 'https://github.com/Reach2trillion/o8o',
    'license': 'LGPL-3',
    'depends': [
        'sale_stock',
        'stock_delivery',
    ],
    'data': [
        # reports
        'report/cod_label_report_actions.xml',
        'report/cod_label_report_templates.xml',
        # views
        'views/stock_picking_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'application': False,
    'installable': True,
    'auto_install': False,
}
