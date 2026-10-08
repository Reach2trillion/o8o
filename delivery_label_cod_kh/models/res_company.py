# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.tools import float_round

KHR_RATE_SOURCES = [
    ('odoo', 'Odoo currency rate'),
    ('fixed', 'Fixed rate'),
]

QR_CONTENTS = [
    ('map', "Receiver location (Google Maps)"),
    ('reference', 'Transfer reference'),
    ('none', 'Nothing'),
]

PAY_QR_MODES = [
    ('odoo_link', 'Odoo payment link of the sales order'),
    ('khqr_image', 'Static ABA KHQR image'),
    ('custom_url', 'Custom URL'),
    ('none', 'No payment QR'),
]

NO_FEE_NOTES = [
    ('none', 'Print nothing'),
    ('free', 'Free delivery (paid by the shop)'),
    ('receiver', 'The receiver pays the courier separately'),
]

DEFAULT_KHR_RATE = 4100.0
DEFAULT_FOOTER = 'អរគុណសម្រាប់ការគាំទ្រ! Thank you for your support!'


class ResCompany(models.Model):
    _inherit = 'res.company'

    kh_label_khr_rate_source = fields.Selection(
        KHR_RATE_SOURCES, string='KHR Rate Source', default='odoo', required=True,
        help="How amounts are converted between the order currency and Cambodian riel on the COD label. "
             "'Odoo currency rate' uses the rate of the KHR currency (Accounting > Configuration > Currencies) "
             "and falls back to the fixed rate when KHR is inactive or has no rate.")
    kh_label_khr_rate = fields.Float(
        string='Fixed KHR Rate', default=DEFAULT_KHR_RATE, digits=(16, 4),
        help="Riel per 1 USD. Used when the rate source is 'Fixed rate', and as a fallback when the "
             "KHR currency is missing, inactive or has no rate.")
    kh_label_khr_rounding = fields.Integer(
        string='Round Riel To', default=100,
        help="The riel amount printed on the label is rounded (half-up) to this step, e.g. 100 prints "
             "69,042 as 69,000. Zero or a negative value means no rounding (1 riel).")
    kh_label_show_khr = fields.Boolean(
        string='Print Riel Equivalent', default=True,
        help="Print the riel equivalent of the amount to collect (or the USD equivalent for orders in riel).")
    kh_label_qr_content = fields.Selection(
        QR_CONTENTS, string='Label Info QR Code', default='map', required=True,
        help="What the information QR code of the label contains:\n"
             "- Receiver location: Google Maps link built from the contact's geolocation "
             "(falls back to the transfer reference when the contact has no coordinates).\n"
             "- Transfer reference: the reference of the transfer.\n"
             "- Nothing: no information QR code.")
    kh_label_khqr_image = fields.Image(
        string='ABA KHQR Image', max_width=512, max_height=512,
        help="Static ABA KHQR payment QR code of the shop. Printed as the payment QR of COD labels "
             "in 'Static ABA KHQR image' mode, and as fallback of the Odoo payment link for "
             "transfers without sales order.")
    kh_label_pay_qr = fields.Selection(
        PAY_QR_MODES, string='Payment QR Code', default='odoo_link', required=True,
        help="'Scan to pay' QR code printed next to the amount on COD labels only:\n"
             "- Odoo payment link: opens the payment page of the sales order with the COD amount "
             "(pay with any published payment provider, e.g. ABA KHQR). Transfers without sales order "
             "use the static KHQR image instead, when one is set.\n"
             "- Static ABA KHQR image: the uploaded image; the customer types the amount.\n"
             "- Custom URL: the URL template below.\n"
             "- No payment QR.")
    kh_label_pay_url_template = fields.Char(
        string='Payment URL Template',
        help="URL encoded in the payment QR in 'Custom URL' mode. Placeholders (URL-encoded): "
             "{order} sales order reference, {picking} transfer reference, {amount} amount to collect, "
             "{currency} currency code (USD, KHR), {partner} receiver name. "
             "Example: https://pay.example.com/?ref={order}&amount={amount}&ccy={currency}")
    kh_label_no_fee_note = fields.Selection(
        NO_FEE_NOTES, string='Orders Without Delivery Fee', default='none', required=True,
        help="Line printed in the COD box when the sales order has no delivery fee (no delivery line, "
             "or a free one), so a courier knows whether to add its own fee:\n"
             "- Free delivery: 'ដឹកជញ្ជូនឥតគិតថ្លៃ · Free delivery: collect this amount only'.\n"
             "- The receiver pays the courier: 'ថ្លៃដឹកមិនរួមបញ្ចូល · Delivery fee not included'.\n"
             "Orders with a delivery fee print 'Goods $15.00 + Delivery $2.00' instead.")
    kh_label_show_items = fields.Boolean(
        string='Print Item Summary', default=True,
        help="Print the list of products on the label. Disable it for discreet parcels.")
    kh_label_max_item_lines = fields.Integer(
        string='Products Listed', default=3,
        help="Number of products listed on the label before '+N more'.")
    kh_label_tagline = fields.Char(
        string='Label Tagline', translate=True,
        help="Optional short line printed under the company name. Translatable: each language has its "
             "own text and the label prints the text of the language of the user who prints it.")
    kh_label_footer = fields.Char(
        string='Label Footer', translate=True, default=DEFAULT_FOOTER,
        help="Text printed at the bottom of the label. Translatable: each language has its own text and "
             "the label prints the text of the language of the user who prints it.")
    kh_label_fragile_default = fields.Boolean(
        string='Fragile by Default',
        help="Default value of the 'Fragile' flag of new delivery orders.")
    kh_label_allow_check_default = fields.Boolean(
        string='Allow Check by Default', default=True,
        help="Default value of the 'Allow to check goods' flag of new delivery orders "
             "(the customer may open the parcel before paying).")

    # ------------------------------------------------------------------
    # Riel conversion helpers
    # ------------------------------------------------------------------
    @api.model
    def _kh_get_khr_currency(self):
        """Return the KHR currency record (even when inactive), or an empty recordset."""
        return self.env['res.currency'].with_context(active_test=False).search([('name', '=', 'KHR')], limit=1)

    def _kh_get_usd_currency(self):
        """Return the currency used as 'dollar' for the fixed rate (USD, else the company currency)."""
        self.ensure_one()
        usd = self.env.ref('base.USD', raise_if_not_found=False)
        return usd or self.currency_id

    def _kh_odoo_khr_rate_available(self, khr):
        """True when ``khr`` is active and has at least one rate usable by this company."""
        self.ensure_one()
        if not khr or not khr.active:
            return False
        if khr == self.currency_id:
            return True
        return bool(self.env['res.currency.rate'].sudo().search_count([
            ('currency_id', '=', khr.id),
            ('company_id', 'in', (False, self.root_id.id)),
        ], limit=1))

    def _kh_get_khr_step(self):
        self.ensure_one()
        return self.kh_label_khr_rounding if self.kh_label_khr_rounding and self.kh_label_khr_rounding > 0 else 1

    def _kh_round_khr(self, amount):
        """Round a riel amount half-up to the configured step (100 by default)."""
        self.ensure_one()
        return float_round(amount, precision_rounding=self._kh_get_khr_step())

    def _kh_fixed_rate(self):
        self.ensure_one()
        return self.kh_label_khr_rate if self.kh_label_khr_rate and self.kh_label_khr_rate > 0 else DEFAULT_KHR_RATE

    def _kh_to_khr(self, amount, currency, date=None):
        """Convert ``amount`` expressed in ``currency`` to riel (not rounded).

        Source 'odoo' uses ``res.currency._convert`` when the KHR currency is usable, otherwise the
        fixed rate (riel per 1 USD) is applied, converting non-USD amounts to USD first.
        """
        self.ensure_one()
        date = date or fields.Date.context_today(self)
        khr = self._kh_get_khr_currency()
        if khr and currency == khr:
            return amount
        if self.kh_label_khr_rate_source == 'odoo' and self._kh_odoo_khr_rate_available(khr):
            return currency._convert(amount, khr, self, date, round=False)
        usd = self._kh_get_usd_currency()
        if currency != usd:
            amount = currency._convert(amount, usd, self, date, round=False)
        return amount * self._kh_fixed_rate()

    def _kh_from_khr(self, amount_khr, currency, date=None):
        """Inverse of :meth:`_kh_to_khr`: convert a riel amount to ``currency`` (not rounded)."""
        self.ensure_one()
        date = date or fields.Date.context_today(self)
        khr = self._kh_get_khr_currency()
        if khr and currency == khr:
            return amount_khr
        if self.kh_label_khr_rate_source == 'odoo' and self._kh_odoo_khr_rate_available(khr):
            return khr._convert(amount_khr, currency, self, date, round=False)
        usd = self._kh_get_usd_currency()
        amount_usd = amount_khr / self._kh_fixed_rate()
        if currency != usd:
            return usd._convert(amount_usd, currency, self, date, round=False)
        return amount_usd
