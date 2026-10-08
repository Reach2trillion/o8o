# -*- coding: utf-8 -*-
import base64
import io
import logging
import math
import re
from urllib.parse import quote as url_quote

from PIL import Image
from reportlab.graphics.barcode.code128 import Code128

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_repr, float_round
from odoo.tools.image import image_data_uri

from .kh_label_text import (
    box_style, count_units, fit_start, fit_width, has_khmer, pick_size, px_to_mm, script_of, text_box,
    text_width_mm, truncate as kh_truncate, wrap_lines,
)

_logger = logging.getLogger(__name__)

PAYMENT_MODES = [
    ('auto', 'Auto (from sales order)'),
    ('manual', 'Manual amount'),
    ('paid', 'Paid - do not collect'),
]

COD_STATES = [
    ('cod', 'Collect on delivery'),
    ('paid', 'Paid - do not collect'),
    ('none', 'No COD'),
]

MAX_PARCELS = 50

# Server-side truncation of the label texts, in visible characters (a Khmer syllable counts once
# per base consonant, see kh_label_text.py); the texts are also fitted to the width of their box.
MAX_NAME = 40
MAX_ADDRESS = 110
MAX_NOTE = 60
MAX_PRODUCT = 28
MAX_PROVINCE = 32
MAX_COURIER = 22
MAX_COMPANY = 32
MAX_TAGLINE = 70
MAX_FOOTER = 110
MAX_REFERENCE = 24
MAX_PAY_URL = 1024  # longer URLs do not fit an 18 mm QR code readable at 203 dpi

# Cambodian phone numbers: +855 / 855 / 00855 (optionally followed by "0" or "(0)") or a leading 0,
# then 8 or 9 digits (9-10 digits in local format), optionally separated by spaces, dots or dashes.
# Matched on the text with Khmer digits translated to ASCII digits (kh_ascii_digits).
PHONE_RE = re.compile(
    r'(?<![\d+])'
    r'(?:(?:\+|00)?\s*855[\s.\-]*(?:\(0\)|0)?[\s.\-]*|0)'
    r'\d(?:[\s.\-]?\d){7,8}'
    r'(?!\d)'
)
PHONE_SPLIT_RE = re.compile(r'\s*[/,;|]\s*')
KHMER_DIGITS = str.maketrans('០១២៣៤៥៦៧៨៩', '0123456789')

MAP_URL = 'https://maps.google.com/?q=%.7f,%.7f'

# ----------------------------------------------------------------------------------------------
# Layout of the label in CSS pixels (96 dpi: 1 px = 0.265 mm), mirrored from the template.
# wkhtmltopdf truncates every length to whole pixels, so the layout is computed in pixels.
# Label 366 x 291 px with a 2 px frame: 362 x 287 px inside, sections (bottom rules included):
#   header 42 | receiver 99 | payment 82 | items 38 | footer 26
# ----------------------------------------------------------------------------------------------
LABEL_W = 362
# header (40 px high): sender | references | courier, parcel
HEAD_H = 40
HEAD_COMPANY_W = 166
HEAD_REF_W = 104
HEAD_COURIER_W = LABEL_W - HEAD_COMPANY_W - HEAD_REF_W
HEAD_PAD_X = 10         # left + right padding of a header cell
HEAD_SEP = 2            # column rule
LOGO_CELL_W = 42
COMPANY_SIZES = (9, 8, 7, 6.5)
TAGLINE_SIZE = 6
COMPANY_PHONE_SIZE = 7.5
REF_SIZES = (8.5, 7.5, 6.5)
META_SIZE = 6.5
COURIER_SIZES = (7, 6.5, 6)
PARCEL_LABEL_H = 12
PARCEL_LINE_H = 14
# receiver (97 px high, 2 px top padding): name, phone, address | province box | info QR
RECV_H = 95
RECV_PAD_X = 12
RECV_TAG_H = 11
PROVINCE_CELL_W = 104
PROVINCE_CELL_W_INFO = 92
PROVINCE_TEXT_PAD = 16  # right padding, box borders and inner padding of the province cell
INFO_CELL_W = 69        # 57 px QR code (15 mm) + 6 px quiet zone on each side
NAME_SIZES = (13, 11, 9)
NAME_SIZES_KHMER = (11, 10, 9)  # Khmer letters are much taller than Latin ones at the same size
PHONE_SIZES = (18, 16, 14, 12)
PHONE_MIN_SIZE_SHARED = 16  # below this size the second phone number moves to its own line
PHONE2_SIZE = 10
ADDRESS_SIZES = (8, 7.5)  # 7.5 pt fits two Khmer lines under a Khmer name
ADDRESS_LINES = 3
ADDRESS_BLANK_SIZE = 7
PROVINCE_SIZES = (15, 13, 11.5, 10, 9, 8)
PROVINCE_CITY_SIZE = 7
# payment (80 px high): COD box | payment QR, PAID / NO COD box | info QR
PAY_H = 76              # inside the 2 px vertical padding of the COD box
PAYQR_CELL_W = 80       # 68 px (18 mm) payment QR + 6 px (1.6 mm) quiet zone on each side
INFO_PAY_CELL_W = 96
COD_PAD_X = 16
PAY_TITLE_H = 17
PAY_SMALL_SIZE = 7
PAY_SMALL_H = 14
AMOUNT_SIZES = (25, 22, 19, 16)
SECONDARY_SIZE = 12
PAY_SMALL_LINES_MAX = 2
# items (36 px high, 1 px top padding): items summary, note | handling chips
ITEMS_H = 35
ITEMS_PAD_X = 12
CHIPS_CELL_W = 156
ITEMS_SIZE = 6.5
NOTE_SIZE = 6.5
NOTE_PREFIX = 'ចំណាំ Note: '
ITEMS_PREFIX = 'ទំនិញ Items (ចំនួន Qty %s): '
# footer (26 px high): Code128 barcode | thank-you text
FOOT_H = 26
# Code128: one CSS pixel per module, scaled up by a CSS transform (vector bars, no resampling) so a
# module is 0.32 mm (2.5 dots at 203 dpi, 1.9 at 150 dpi: whole pixels of 0.26 mm do not decode
# at 150 dpi); long references get 0.29 mm. Quiet zones of 10 modules.
BARCODE_SCALES = ((165, 1.2), (200, 1.107))  # (max modules, scale)
BARCODE_QUIET_MODULES = 10
BARCODE_H = 16
FOOTER_SIZE = 6
FOOTER_PAD_X = 10

PROVINCE_PREFIXES = ('រាជធានី', 'ខេត្ត', 'ក្រុង')

# Payment QR ("scan to pay") of COD labels, see the README.
PAY_CAPTION = 'ស្កេនដើម្បីទូទាត់ · SCAN TO PAY'
PAY_CAPTION_KHQR = 'ABA KHQR · ស្កេនដើម្បីទូទាត់ · SCAN TO PAY'
PAY_QR_PIXELS = 660            # size of the payment QR PNG drawn by reportlab (printed 18 mm wide)
PAY_QR_LEVEL_M_MAX_BYTES = 106  # QR version 6 at level M; longer URLs use level L (less dense)
INFO_QR_PIXELS = 480
MAP_CAPTION_LINES = ('ស្កេនមើលទីតាំង · MAP',)
# COD box line for orders without delivery fee (company setting kh_label_no_fee_note)
NO_FEE_LINES = {
    'free': 'ដឹកជញ្ជូនឥតគិតថ្លៃ · Free delivery: collect this amount only',
    'receiver': 'ថ្លៃដឹកមិនរួមបញ្ចូល · Delivery fee not included',
}
MAP_CAPTION_LINES_SHORT = ('ស្កេនមើលទីតាំង', 'MAP')
PAY_URL_PLACEHOLDER_RE = re.compile(r'\{(order|picking|amount|currency|partner)(?:![rsa])?(?::[^{}]*)?\}')


def kh_ascii_digits(text):
    """``text`` with Khmer digits (០-៩, typed on the Khmer keyboard) replaced by ASCII digits."""
    return (text or '').translate(KHMER_DIGITS)


def kh_normalize_phone(text):
    """Return the local 0XXXXXXXX(X) digits of a Cambodian number, or False."""
    digits = re.sub(r'[^0-9]', '', kh_ascii_digits(text))
    if digits.startswith('00855'):
        digits = digits[2:]
    if digits.startswith('855') and len(digits) >= 11:
        digits = '0' + digits[3:].lstrip('0')
    if digits.startswith('0') and len(digits) in (9, 10):
        return digits
    return False


def kh_format_phone(local):
    """Format local digits as ``0XX XXX XXX`` (9 digits) or ``0XX XXX XXXX`` (10 digits)."""
    return '%s %s %s' % (local[:3], local[3:6], local[6:])


def kh_format_qty(qty):
    """``%g``-like quantity formatting (2.0 -> "2", 2.5 -> "2.5") without exponents."""
    text = ('%.3f' % qty).rstrip('0').rstrip('.')
    return text or '0'


def kh_otsu_threshold(gray):
    """Otsu threshold (0-255) of a grayscale PIL image: separates the ink from the background."""
    histogram = gray.histogram()[:256]
    total = sum(histogram)
    sum_total = sum(index * count for index, count in enumerate(histogram))
    weight_back = sum_back = 0
    best, threshold = -1.0, 127
    for index, count in enumerate(histogram):
        weight_back += count
        if not weight_back:
            continue
        weight_fore = total - weight_back
        if not weight_fore:
            break
        sum_back += index * count
        mean_back = sum_back / weight_back
        mean_fore = (sum_total - sum_back) / weight_fore
        between = weight_back * weight_fore * (mean_back - mean_fore) ** 2
        if between > best:
            best, threshold = between, index
    return threshold


def kh_to_black_and_white(image, threshold=None):
    """Pure black and white ``L`` image: transparency becomes white, the ink black.

    Thermal printers print one dot or none: grey pixels either vanish (threshold drivers) or turn
    into speckles (dithering drivers), so images are converted before printing.
    """
    if image.mode == 'P':
        image = image.convert('RGBA')
    if image.mode in ('RGBA', 'LA'):
        background = Image.new('RGBA', image.size, (255, 255, 255, 255))
        background.alpha_composite(image.convert('RGBA'))
        image = background
    gray = image.convert('L')
    if threshold is None:
        threshold = kh_otsu_threshold(gray)
    return gray.point(lambda value: 0 if value <= threshold else 255, 'L')


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    kh_payment_mode = fields.Selection(
        PAYMENT_MODES, string='COD Mode', default='auto', required=True, copy=False, tracking=True,
        help="Auto: amount still due on the sales order (total minus posted invoice payments and "
             "online payments).\n"
             "Manual: the amount entered below.\n"
             "Paid: the label says PAID - DO NOT COLLECT.")
    kh_cod_currency_id = fields.Many2one(
        'res.currency', string='COD Currency', compute='_compute_kh_cod_currency_id')
    kh_cod_amount_manual = fields.Monetary(
        string='Manual COD Amount', currency_field='kh_cod_currency_id', copy=False, tracking=True,
        help="Amount the courier must collect, used when the COD mode is 'Manual amount'. "
             "Use the mode 'Paid - do not collect' when nothing must be collected.")
    kh_cod_amount_auto = fields.Monetary(
        string='Amount Due on Order', currency_field='kh_cod_currency_id', compute='_compute_kh_cod',
        help="What the 'Auto' mode prints: sales order total minus the paid part of its posted "
             "invoices (credit notes deducted) and its online payments not reconciled with them.")
    kh_cod_amount = fields.Monetary(
        string='COD Amount', currency_field='kh_cod_currency_id', compute='_compute_kh_cod',
        help="Amount printed on the COD label.")
    kh_cod_state = fields.Selection(
        COD_STATES, string='COD Status', compute='_compute_kh_cod')
    kh_cod_warning = fields.Char(string='COD Warning', compute='_compute_kh_cod_warning')
    kh_payment_link = fields.Char(
        string='Payment Link', compute='_compute_kh_payment_link',
        help="Link encoded in the 'scan to pay' QR code of the COD label (Inventory > Settings > "
             "COD Delivery Label > Payment QR Code): the payment page of the sales order for the "
             "amount to collect, or the custom payment URL. Copy it to send it by Telegram or SMS.")
    kh_receiver_phone = fields.Char(
        string='Receiver Phone', compute='_compute_kh_receiver_phone', store=True, readonly=False,
        help="Phone number(s) printed on the label, taken from the contact's phone, mobile or from "
             "a number typed in the contact name. Editable; recomputed when the contact changes.")
    kh_parcel_count = fields.Integer(
        string='Parcels', default=1, copy=False,
        help="Number of parcels: one label is printed per parcel (1/N ... N/N).")
    kh_fragile = fields.Boolean(
        string='Fragile', compute='_compute_kh_handling_flags', store=True, readonly=False, precompute=True,
        help="Print the FRAGILE chip on the label. New delivery orders take the default of their "
             "company (Inventory > Settings > COD Delivery Label).")
    kh_allow_check = fields.Boolean(
        string='Allow to Check Goods',
        compute='_compute_kh_handling_flags', store=True, readonly=False, precompute=True,
        help="The customer may open and check the goods before paying. The label says it either way "
             "('allowed' / 'not allowed'). New delivery orders take the default of their company.")
    kh_label_note = fields.Char(
        string='Label Note', help="Short instruction printed on the label, e.g. 'call before 5pm'.")

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('kh_parcel_count')
    def _check_kh_parcel_count(self):
        for picking in self:
            if not 1 <= (picking.kh_parcel_count or 0) <= MAX_PARCELS:
                raise ValidationError(_(
                    "The number of parcels of %(picking)s must be between 1 and %(max)s.",
                    picking=picking.display_name, max=MAX_PARCELS))

    @api.constrains('kh_cod_amount_manual')
    def _check_kh_cod_amount_manual(self):
        for picking in self:
            if picking.kh_cod_amount_manual and picking.kh_cod_amount_manual < 0:
                raise ValidationError(_("The manual COD amount of %s cannot be negative.", picking.display_name))

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('picking_type_id')
    def _compute_kh_handling_flags(self):
        """Defaults of the handling flags, from the settings of the company of the transfer.

        Only new transfers get them (a transfer created for company B while company A is the
        active company gets B's defaults); the flags of existing transfers are kept. The company
        of a transfer is the one of its operation type.
        """
        for picking in self:
            if picking._origin:
                continue
            company = picking.picking_type_id.company_id or self.env.company
            picking.kh_fragile = company.kh_label_fragile_default
            picking.kh_allow_check = company.kh_label_allow_check_default

    @api.depends('sale_id.currency_id', 'company_id.currency_id')
    def _compute_kh_cod_currency_id(self):
        for picking in self:
            pos_order = picking.sudo()._kh_pos_order()
            picking.kh_cod_currency_id = (
                picking.sudo().sale_id.currency_id or (pos_order and pos_order.currency_id)
                or picking.company_id.currency_id or self.env.company.currency_id)

    @api.depends(
        'picking_type_code', 'return_id', 'state', 'kh_payment_mode', 'kh_cod_amount_manual',
        'sale_id.amount_total', 'sale_id.currency_id', 'sale_id.invoice_ids.state',
        'sale_id.invoice_ids.amount_residual', 'sale_id.transaction_ids.state')
    def _compute_kh_cod(self):
        for picking in self:
            auto_amount, auto_state = picking._kh_cod_auto_amount()
            amount, state = picking._kh_cod_resolve(auto_amount, auto_state)
            picking.kh_cod_amount_auto = auto_amount
            picking.kh_cod_amount = amount
            picking.kh_cod_state = state

    @api.depends(
        'state', 'kh_payment_mode', 'kh_cod_amount_manual', 'kh_cod_state', 'sale_id.picking_ids.state',
        'sale_id.invoice_ids.state', 'sale_id.order_line.qty_delivered', 'move_ids.quantity',
        'move_ids.picked', 'move_ids.product_uom_qty')
    def _compute_kh_cod_warning(self):
        for picking in self:
            messages = [message for _blocking, message in picking._kh_cod_issues()]
            picking.kh_cod_warning = '\n'.join(messages) or False

    @api.depends(
        'kh_cod_state', 'kh_cod_amount', 'kh_cod_currency_id', 'partner_id.name',
        'sale_id.partner_invoice_id', 'company_id.kh_label_pay_qr', 'company_id.kh_label_pay_url_template')
    def _compute_kh_payment_link(self):
        for picking in self:
            mode, url = picking._kh_get_payment_url()
            picking.kh_payment_link = url if mode in ('odoo_link', 'custom_url') else False

    @api.depends('partner_id', 'partner_id.name', 'partner_id.phone', 'partner_id.mobile',
                 'partner_id.commercial_partner_id.phone', 'partner_id.commercial_partner_id.mobile')
    def _compute_kh_receiver_phone(self):
        for picking in self:
            picking.kh_receiver_phone = self._kh_extract_phones(picking.partner_id) or False

    # ------------------------------------------------------------------
    # COD rules
    # ------------------------------------------------------------------
    def _kh_pos_order(self):
        """Point of Sale order of the transfer when ``point_of_sale`` is installed, else None."""
        self.ensure_one()
        return self.pos_order_id if 'pos_order_id' in self._fields else None

    def _kh_is_cod_candidate(self):
        """Only deliveries to the customer can carry a COD amount.

        Not: receipts, internal transfers, cancelled transfers, returns to the vendor. A return of
        a customer return (the parcel is sent again after a failed delivery) is a delivery.
        """
        self.ensure_one()
        if self.picking_type_code != 'outgoing' or self.state == 'cancel':
            return False
        origin = self.return_id
        return not origin or (origin.picking_type_code == 'incoming'
                              and origin.return_id.picking_type_code == 'outgoing')

    @api.model
    def _kh_round_cod(self, amount, currency):
        """Round an amount to collect: with the currency, and to whole riel for KHR (half-up).

        Odoo rounds KHR to 0.01 but riel has no subunit: 13,579.50៛ is collected and printed as
        13,580៛, and the payment link asks for that very amount.
        """
        if currency.name == 'KHR':
            return float_round(amount, precision_rounding=max(currency.rounding, 1.0))
        return currency.round(amount)

    def _kh_cod_auto_amount(self):
        """Amount due on the linked sales order (or POS order), in ``kh_cod_currency_id``.

        :return: tuple ``(amount, state)`` where state is ``'cod'``, ``'paid'`` or ``'none'``
        """
        self.ensure_one()
        if not self._kh_is_cod_candidate():
            return 0.0, 'none'
        pos_order = self.sudo()._kh_pos_order()
        if pos_order:
            return self._kh_pos_order_due(pos_order)
        order = self.sudo().sale_id
        if not order:
            return 0.0, 'none'
        due = self._kh_sale_order_due(order)
        if order.currency_id.compare_amounts(due, 0.0) > 0:
            return due, 'cod'
        return 0.0, 'paid'

    @api.model
    def _kh_sale_order_due(self, order):
        """Amount still due on ``order`` (order currency, rounded, never negative)."""
        order = order.sudo()
        return self._kh_round_cod(max(order.amount_total - self._kh_order_paid_amount(order), 0.0), order.currency_id)

    @api.model
    def _kh_pos_order_due(self, pos_order):
        """Amount due on a Point of Sale order delivered later.

        POS orders are usually paid at the till, but a "ship later" order can be settled with the
        *Customer Account* payment method (type ``pay_later``): nothing has been paid then, the
        courier collects it.
        """
        pos_order = pos_order.sudo()
        currency = pos_order.currency_id
        paid = sum(payment.amount for payment in pos_order.payment_ids
                   if payment.payment_method_id.type != 'pay_later')
        due = self._kh_round_cod(max(pos_order.amount_total - paid, 0.0), currency)
        if currency.compare_amounts(due, 0.0) > 0:
            return due, 'cod'
        return 0.0, 'paid'

    @api.model
    def _kh_order_posted_invoices(self, order):
        return order.sudo().invoice_ids.filtered(
            lambda move: move.state == 'posted' and move.move_type in ('out_invoice', 'out_refund'))

    @api.model
    def _kh_order_paid_amount(self, order):
        """Paid part of ``order``, in the order currency.

        Sum over the posted customer invoices of ``amount_total - amount_residual`` minus the same
        for posted credit notes, plus the online payment transactions of the order (e.g. paid with
        the "scan to pay" QR of the label) for the part of their payment that is not reconciled
        with those invoices (that part is already in their paid amount). Draft invoices count as
        unpaid.
        """
        order = order.sudo()
        currency = order.currency_id
        today = fields.Date.context_today(self)
        paid = 0.0
        invoices = self._kh_order_posted_invoices(order)
        for move in invoices:
            amount = move.amount_total - move.amount_residual
            if move.currency_id != currency:
                amount = move.currency_id._convert(
                    amount, currency, order.company_id, move.invoice_date or today, round=False)
            paid += amount if move.move_type == 'out_invoice' else -amount
        return paid + self._kh_order_transactions_paid(order, invoices)

    @api.model
    def _kh_order_transactions_paid(self, order, invoices):
        """Online payments (authorized / done transactions) of ``order`` not counted in ``invoices``.

        A transaction made from the payment link of the order creates a payment that is not
        reconciled with an invoice that already exists: both are counted. When the payment is
        (partly) reconciled with one of ``invoices``, that part is not counted again.
        """
        currency = order.currency_id
        today = fields.Date.context_today(self)
        invoice_lines = invoices.line_ids
        total = 0.0
        for transaction in order.sudo().transaction_ids.filtered(lambda tx: tx.state in ('authorized', 'done')):
            amount = transaction.amount
            payment_lines = transaction.payment_id.move_id.line_ids
            if amount > 0 and payment_lines and invoice_lines:
                for partial in payment_lines.matched_debit_ids:
                    if partial.debit_move_id in invoice_lines:
                        amount -= partial.credit_amount_currency
                amount = max(amount, 0.0)
            if transaction.currency_id and transaction.currency_id != currency:
                amount = transaction.currency_id._convert(amount, currency, order.company_id, today, round=False)
            total += amount
        return total

    def _kh_cod_resolve(self, auto_amount, auto_state):
        """Apply the COD mode of the transfer to the automatic result."""
        self.ensure_one()
        if not self._kh_is_cod_candidate():
            return 0.0, 'none'
        currency = self.kh_cod_currency_id
        if self.kh_payment_mode == 'paid':
            return 0.0, 'paid'
        if self.kh_payment_mode == 'manual':
            amount = self._kh_round_cod(max(self.kh_cod_amount_manual or 0.0, 0.0), currency)
            if currency.compare_amounts(amount, 0.0) > 0:
                return amount, 'cod'
            return 0.0, 'paid'
        return auto_amount, auto_state

    @api.model
    def _kh_get_delivery_fee(self, order):
        """Total (tax included) of the delivery lines of ``order``."""
        return sum(order.sudo().order_line.filtered('is_delivery').mapped('price_total'))

    # ------------------------------------------------------------------
    # COD checks: what the automatic amount cannot know (never guess the money)
    # ------------------------------------------------------------------
    @api.model
    def _kh_order_deliveries(self, order):
        """Deliveries of ``order`` that can carry its COD amount (not cancelled, not returns).

        Multi-step pick / pack transfers are internal and do not count.
        """
        return self.browse(order.sudo().picking_ids.filtered(
            lambda picking: picking.state != 'cancel' and picking._kh_is_cod_candidate()).ids)

    def _kh_missing_goods(self, order):
        """Goods of ``order`` that this delivery does not carry: ``[(qty, product name)]``.

        Validated transfer: the delivered quantities of the order lines (a delivery validated
        with "No backorder" leaves goods behind). Not validated yet: the picked quantities, or the
        demand when nothing is picked yet.
        """
        self.ensure_one()
        missing = []
        for line in order.sudo().order_line:
            if (line.display_type or line.is_delivery or line.product_id.type != 'consu'
                    or not line.move_ids or line.product_uom_qty <= 0):
                continue
            if self.state == 'done':
                carried = line.qty_delivered
            else:
                moves = self.move_ids.filtered(lambda move: move.sale_line_id == line and move.state != 'cancel')
                if any(move.product_id != line.product_id for move in moves):
                    continue  # kit: the components cannot be compared with the ordered product
                carried = sum(
                    move.product_uom._compute_quantity(
                        move.quantity if move.picked else move.product_uom_qty, line.product_uom, round=False)
                    for move in moves)
            if float_compare(carried, line.product_uom_qty, precision_rounding=line.product_uom.rounding) < 0:
                missing.append((line.product_uom_qty - carried, line.product_id.with_context(
                    display_default_code=False).display_name))
        return missing

    @api.model
    def _kh_shared_partly_paid_invoices(self, order):
        """Posted invoices of ``order`` that also invoice other orders and are partly paid.

        The paid part of such an invoice cannot be split between the orders: credited to each of
        them, it could make an order look paid while money is still due.
        """
        invoices = self._kh_order_posted_invoices(order).filtered(lambda move: move.move_type == 'out_invoice')
        return invoices.filtered(
            lambda move: (move.invoice_line_ids.sale_line_ids.order_id - order)
            and not move.currency_id.is_zero(move.amount_residual)
            and not move.currency_id.is_zero(move.amount_total - move.amount_residual))

    def _kh_cod_issues(self):
        """Problems of the COD amount of this transfer: list of ``(blocking, message)``.

        Blocking issues refuse printing the label (the form shows them as a warning first):
        the automatic amount cannot be trusted (several deliveries, goods left behind, credit
        notes, invoice shared with other orders) or the manual amount is 0. A manual amount above
        the amount due on the order is only a warning.
        """
        self.ensure_one()
        issues = []
        if not self._kh_is_cod_candidate():
            return issues
        order = self.sudo().sale_id
        currency = self.kh_cod_currency_id
        if self.kh_payment_mode == 'manual':
            manual = self._kh_round_cod(self.kh_cod_amount_manual or 0.0, currency)
            if currency.compare_amounts(manual, 0.0) <= 0:
                issues.append((True, _(
                    "The manual COD amount of %(picking)s is 0: type the amount to collect, or set the COD "
                    "mode to 'Paid - do not collect' if nothing must be collected.", picking=self.name)))
            elif order and currency.compare_amounts(manual, self.kh_cod_amount_auto) > 0:
                issues.append((False, _(
                    "The manual COD amount of %(picking)s (%(manual)s) is more than the amount due on order "
                    "%(order)s (%(due)s).", picking=self.name, manual=self._kh_format_amount(manual, currency),
                    order=order.name, due=self._kh_format_amount(self.kh_cod_amount_auto, currency))))
            return issues
        if self.kh_payment_mode != 'auto' or not order:
            return issues
        shared = self._kh_shared_partly_paid_invoices(order)
        if shared:
            others = shared.invoice_line_ids.sale_line_ids.order_id - order
            issues.append((True, _(
                "Invoice %(invoices)s of order %(order)s also invoices %(others)s and is partly paid: its payment "
                "cannot be split between the orders. Set the COD mode to Manual or Paid before printing the "
                "COD label.", invoices=', '.join(shared.mapped('name')), order=order.name,
                others=', '.join(others.mapped('name')))))
            return issues
        if self.kh_cod_state != 'cod':
            return issues
        due = self._kh_format_amount(self.kh_cod_amount_auto, currency)
        deliveries = self._kh_order_deliveries(order)
        if len(deliveries) > 1:
            issues.append((True, _(
                "Order %(order)s has %(amount)s due over %(count)s deliveries (%(refs)s): set the COD mode to "
                "Manual (the amount to collect with each delivery) or Paid on these deliveries before printing "
                "the COD label, so the amount is not collected twice.",
                order=order.name, amount=due, count=len(deliveries), refs=', '.join(deliveries.mapped('name')))))
            return issues
        missing = self._kh_missing_goods(order)
        if missing:
            listed = ', '.join('%s × %s' % (kh_format_qty(qty), name) for qty, name in missing[:3])
            if len(missing) > 3:
                listed += ', …'
            issues.append((True, _(
                "%(picking)s does not carry the whole order %(order)s (missing: %(missing)s): the %(amount)s due "
                "on the order may include goods that are not in this parcel. Set the COD mode to Manual (amount "
                "for this parcel) or Paid before printing the COD label.",
                picking=self.name, order=order.name, missing=listed, amount=due)))
        refunds = self._kh_order_posted_invoices(order).filtered(lambda move: move.move_type == 'out_refund')
        if refunds:
            issues.append((True, _(
                "Order %(order)s has credit notes (%(refs)s): the amount still due cannot be computed safely "
                "(%(amount)s from the invoices and payments). Check it, then set the COD mode to Manual or Paid "
                "before printing the COD label.",
                order=order.name, refs=', '.join(refunds.mapped('name')), amount=due)))
        return issues

    def _kh_check_printable(self):
        """Raise a UserError listing every transfer whose COD label must not be printed as is."""
        messages = []
        for picking in self:
            if picking.state == 'cancel':
                found = [_("%s is cancelled: a cancelled transfer has no COD label.", picking.name)]
            else:
                found = [message for blocking, message in picking._kh_cod_issues() if blocking]
            messages += [message for message in found if message not in messages]
        if messages:
            raise UserError('\n'.join(messages))

    # ------------------------------------------------------------------
    # Payment QR ("scan to pay", COD labels only)
    # ------------------------------------------------------------------
    def _kh_odoo_payment_link(self, amount):
        """Standard Odoo payment link of the sales order for ``amount`` (order currency), or False.

        Built with the ``payment.link.wizard`` of the sale module, exactly like *Sales > Order >
        Generate a Payment Link*: ``{base_url}/payment/pay?amount=..&access_token=..&sale_order_id=..``
        where the token signs the invoicing partner, the amount and the currency of the order. The
        ``/payment/pay`` controller only checks that token (not the state of the order), so the link
        works for confirmed orders. See ``payment_link_wizard.py`` for the token outside a request
        and ``controllers/portal.py`` for the cap at the amount still due when the link is opened.
        """
        self.ensure_one()
        order = self.sudo().sale_id
        if not order:
            return False
        wizard = self.env['payment.link.wizard'].sudo().new({
            'res_model': 'sale.order',
            'res_id': order.id,
            'amount': self._kh_round_cod(amount, order.currency_id),
            'currency_id': order.currency_id.id,
            'partner_id': order.partner_invoice_id.id,
        })
        return wizard.link or False

    def _kh_custom_payment_url(self, template, amount, currency):
        """Fill the custom payment URL ``template`` of the company.

        Placeholders: ``{order}``, ``{picking}``, ``{amount}`` (e.g. ``17.00``), ``{currency}``
        (``USD``) and ``{partner}`` (receiver name as printed, without the phone numbers typed in
        the contact name), each URL-encoded. A format spec or conversion is ignored
        (``{amount:.2f}`` is ``{amount}``); unknown placeholders and other braces are left as typed:
        a wrong template never blocks the label. A URL longer than 1024 characters cannot be
        printed as a QR code: no payment QR then.
        """
        self.ensure_one()
        template = (template or '').strip()
        if not template:
            return False
        order = self.sudo().sale_id
        values = {
            'order': order.name or self.origin or '',
            'picking': self.name or '',
            'amount': float_repr(self._kh_round_cod(amount, currency), max(currency.decimal_places, 0)),
            'currency': currency.name or '',
            'partner': self._kh_clean_receiver_name(self.sudo().partner_id.name),
        }
        url = PAY_URL_PLACEHOLDER_RE.sub(lambda match: url_quote(str(values[match.group(1)]), safe=''), template)
        if len(url) > MAX_PAY_URL:
            _logger.warning("Payment URL of %s is too long for a QR code (%s characters): no payment QR",
                            self.name, len(url))
            return False
        return url

    def _kh_get_payment_url(self):
        """Effective payment QR of the transfer: ``(mode, url)``.

        ``mode`` is the company setting after fallbacks: ``'odoo_link'`` / ``'custom_url'`` with
        their URL, ``'khqr_image'`` (static image, no URL) or ``False`` when no payment QR prints:
        only COD labels with a positive amount get one. The Odoo link needs a sales order:
        transfers without one use the static KHQR image when it is set, else print no payment QR.
        """
        self.ensure_one()
        if self.kh_cod_state != 'cod' or not self._kh_is_cod_candidate():
            return False, False
        currency = self.kh_cod_currency_id
        amount = self._kh_round_cod(self.kh_cod_amount, currency)
        if currency.compare_amounts(amount, 0.0) <= 0:
            return False, False
        company = self.company_id or self.env.company
        mode = company.kh_label_pay_qr
        if mode == 'odoo_link':
            url = self._kh_odoo_payment_link(amount)
            if url:
                return mode, url
            mode = 'khqr_image'
        if mode == 'custom_url':
            url = self._kh_custom_payment_url(company.kh_label_pay_url_template, amount, currency)
            return (mode, url) if url else (False, False)
        if mode == 'khqr_image' and company.kh_label_khqr_image:
            return mode, False
        return False, False

    # ------------------------------------------------------------------
    # Receiver phone
    # ------------------------------------------------------------------
    @api.model
    def _kh_phone_candidates(self, partner):
        """``(display, key)`` phone candidates of one partner: phone, mobile, then numbers in the name.

        Numbers typed with Khmer digits (default number row of the Khmer keyboard) are read as
        ASCII digits.
        """
        candidates = []
        for value in (partner.phone, partner.mobile):
            value = re.sub(r'\s+', ' ', value or '').strip()
            if not value:
                continue
            ascii_value = kh_ascii_digits(value)
            numbers = [kh_normalize_phone(match.group(0)) for match in PHONE_RE.finditer(ascii_value)]
            numbers = [number for number in numbers if number]
            if not numbers:
                number = kh_normalize_phone(ascii_value)
                numbers = [number] if number else []
            if numbers:
                candidates += [(kh_format_phone(number), number) for number in numbers]
            else:
                # Not a Cambodian number: keep it as typed.
                candidates.append((value, re.sub(r'[^0-9]', '', ascii_value) or value.lower()))
        for match in PHONE_RE.finditer(kh_ascii_digits(partner.name)):
            number = kh_normalize_phone(match.group(0))
            if number:
                candidates.append((kh_format_phone(number), number))
        return candidates

    @api.model
    def _kh_extract_phone_list(self, partner, limit=2):
        """De-duplicated receiver phones (at most ``limit``), falling back to the commercial partner."""
        partner = partner.sudo()
        if not partner:
            return []
        candidates = self._kh_phone_candidates(partner)
        commercial = partner.commercial_partner_id
        if not candidates and commercial and commercial != partner:
            candidates = self._kh_phone_candidates(commercial)
        phones, seen = [], set()
        for display, key in candidates:
            if key in seen:
                continue
            seen.add(key)
            phones.append(display)
        return phones[:limit]

    @api.model
    def _kh_extract_phones(self, partner):
        """Receiver phone(s) of ``partner`` formatted for the label, joined with `` / ``."""
        return ' / '.join(self._kh_extract_phone_list(partner))

    @api.model
    def _kh_clean_receiver_name(self, name):
        """Remove the phone numbers staff typed in the contact name ("Dara 095634706" -> "Dara").

        Numbers typed with Khmer digits are removed too; other Khmer digits are kept as typed.
        """
        name = re.sub(r'\s+', ' ', name or '').strip()
        ascii_name = kh_ascii_digits(name)  # same length: the match positions apply to ``name``
        parts, start = [], 0
        for match in PHONE_RE.finditer(ascii_name):
            parts.append(name[start:match.start()])
            start = match.end()
        parts.append(name[start:])
        cleaned = re.sub(r'\s+', ' ', ' '.join(parts)).strip(' ,;:-/|()')
        return cleaned or name

    # ------------------------------------------------------------------
    # Formatting helpers and images
    # ------------------------------------------------------------------
    @api.model
    def _kh_format_amount(self, amount, currency):
        """``$17.00`` (currency symbol, position and decimals) or ``69,100៛`` for riel (half-up)."""
        if currency.name == 'KHR':
            return '{:,.0f}៛'.format(float_round(amount, precision_digits=0))
        text = '{:,.{prec}f}'.format(amount, prec=max(currency.decimal_places, 0))
        symbol = currency.symbol or currency.name
        if currency.position == 'after':
            return '%s %s' % (text, symbol)
        return '%s%s' % (symbol, text)

    @api.model
    def _kh_barcode_data_uri(self, barcode_type, value, **kwargs):
        """PNG barcode / QR code as a ``data:`` URI (rendered server side, no HTTP fetch).

        Uses ``ir.actions.report.barcode()``. When reportlab cannot draw it (no bitmap backend
        such as reportlab 4 from pip without ``rl_renderPM`` / ``rlPyCairo``, a backend installed
        while Odoo runs, ...), the image is drawn with PIL instead: a missing barcode library
        never blocks printing the label.
        """
        if not value:
            return False
        try:
            png = self.env['ir.actions.report'].barcode(barcode_type, value, **kwargs)
        except Exception:  # noqa: BLE001 - see the docstring
            _logger.info("reportlab cannot draw the %s code of %r: drawn with PIL", barcode_type, value,
                         exc_info=True)
            png = self._kh_barcode_png_fallback(barcode_type, value, **kwargs)
            if not png:
                return False
        return 'data:image/png;base64,%s' % base64.b64encode(png).decode()

    @api.model
    def _kh_code128_modules(self, value):
        """Code128 of ``value`` as ``[(is_bar, width_in_modules)]`` (no quiet zone), or False."""
        try:
            barcode = Code128(value)
            barcode.validate()
            barcode.encode()
            barcode.decompose()
        except Exception:  # noqa: BLE001 - e.g. characters Code128 cannot encode
            _logger.warning("Cannot encode %r as Code128 on the COD label", value, exc_info=True)
            return False
        return [(char.isupper(), ord(char.lower()) - ord('a') + 1) for char in barcode.decomposed]

    @api.model
    def _kh_barcode_png_fallback(self, barcode_type, value, **kwargs):
        """Pixel-exact PNG of a Code128 barcode or a QR code drawn with PIL (no reportlab bitmap backend).

        Honours the ``quiet`` / ``barBorder`` / ``barLevel`` options of ``ir.actions.report.barcode()``
        for QR codes (``quiet`` truthy means no border, like the standard method).
        """
        try:
            if barcode_type == 'QR':
                import qrcode  # noqa: PLC0415 - optional, only needed for the fallback
                levels = {
                    'L': qrcode.constants.ERROR_CORRECT_L, 'M': qrcode.constants.ERROR_CORRECT_M,
                    'Q': qrcode.constants.ERROR_CORRECT_Q, 'H': qrcode.constants.ERROR_CORRECT_H,
                }
                border = 0 if int(kwargs.get('quiet', 1)) else int(kwargs.get('barBorder', 4))
                qr = qrcode.QRCode(
                    error_correction=levels.get(kwargs.get('barLevel', 'L'), qrcode.constants.ERROR_CORRECT_L),
                    box_size=12, border=border)
                qr.add_data(value)
                qr.make(fit=True)
                image = qr.make_image(fill_color='black', back_color='white').get_image()
            else:
                widths = self._kh_code128_modules(value)
                if not widths:
                    return False
                module_px = 4
                image = Image.new('1', (sum(width for _bar, width in widths) * module_px, 160), 1)
                x = 0
                for is_bar, width in widths:
                    if is_bar:
                        image.paste(0, (x, 0, x + width * module_px, image.height))
                    x += width * module_px
            output = io.BytesIO()
            image.convert('L').save(output, format='PNG')
            return output.getvalue()
        except Exception:  # noqa: BLE001 - never block the label because of a barcode
            _logger.warning("Cannot draw %s barcode for %r on the COD label", barcode_type, value, exc_info=True)
            return False

    @api.model
    def _kh_label_barcode(self, value):
        """Code128 of the transfer reference drawn as black HTML bars, one CSS pixel per module.

        Vector bars print sharp at any printer resolution (an image is resampled by wkhtmltopdf
        and gets grey edges); the template scales them with a CSS transform (``BARCODE_SCALES``).

        :return: ``{'bars': [(space_before_px, bar_px)], 'width': modules, 'scale': float}`` or False
        """
        widths = self._kh_code128_modules(value)
        modules = sum(width for _bar, width in widths) if widths else 0
        scale = next((scale for max_modules, scale in BARCODE_SCALES if modules <= max_modules), False)
        if not widths or not scale:
            return False
        bars, space = [], 0
        for is_bar, width in widths:
            if is_bar:
                bars.append((space, width))
                space = 0
            else:
                space += width
        return {'bars': bars, 'width': modules, 'scale': scale}

    @api.model
    def _kh_label_logo(self, company):
        """Company logo converted to pure black and white (thermal printers print no grey), as a data URI."""
        if not company.logo or company.uses_default_logo:
            return False
        return self._kh_black_and_white_data_uri(company.logo, transparent=True)

    @api.model
    def _kh_black_and_white_data_uri(self, image_base64, max_px=400, trim=False, transparent=False):
        """Black and white PNG ``data:`` URI of a base64 image, at most ``max_px``.

        :param bool trim: crop the image to its ink (a QR code exported with a wide white margin
            prints larger in the same box)
        :param bool transparent: the white of the image is transparent (it never covers a rule)
        :return: ``(data_uri, width / height)``; an unreadable image (e.g. SVG) is returned as stored
        """
        try:
            image = Image.open(io.BytesIO(base64.b64decode(image_base64)))
            image.load()
            image = kh_to_black_and_white(image)
            if trim:
                box = Image.eval(image, lambda value: 255 - value).getbbox()
                if box:
                    image = image.crop(box)
            if max(image.size) > max_px:
                image.thumbnail((max_px, max_px), Image.Resampling.NEAREST)
            elif max(image.size) * 2 <= max_px:
                factor = max_px // max(image.size)
                image = image.resize((image.width * factor, image.height * factor), Image.Resampling.NEAREST)
            if transparent:
                image.putalpha(Image.eval(image, lambda value: 255 - value))
            output = io.BytesIO()
            image.save(output, format='PNG')
            uri = 'data:image/png;base64,%s' % base64.b64encode(output.getvalue()).decode()
            return uri, image.width / float(image.height or 1)
        except Exception:  # noqa: BLE001 - SVG or unreadable image: print it as stored
            return image_data_uri(image_base64), 1.0

    # ------------------------------------------------------------------
    # Label values
    # ------------------------------------------------------------------
    def _kh_get_money_values(self, width_px=LABEL_W - PAYQR_CELL_W - COD_PAD_X, extra_lines=()):
        """Payment block of the label: state, primary / secondary amounts and small lines.

        :param int width_px: width of the text of the COD box (depends on the QR code next to it)
        :param extra_lines: small lines printed in the COD box besides the fee breakdown, in
            priority order (``'parcels'``: total for N parcels, ``'pay'``: "scan to pay" caption).
            At most two small lines fit with the amount: the fee breakdown (``'fee'``), or the
            line of orders without delivery fee (``'nofee'``), is dropped first.
        """
        self.ensure_one()
        company = self.company_id or self.env.company
        currency = self.kh_cod_currency_id
        state = self.kh_cod_state
        amount = self.kh_cod_amount
        values = {
            'state': state,
            'amount': amount,
            'currency': currency,
            'amount_str': '',
            'amount_size': AMOUNT_SIZES[0],
            'amount_style': '',
            'secondary_str': '',
            'goods_str': '',
            'fee_str': '',
            'nofee_str': '',
            'lines': list(extra_lines),
        }
        if state != 'cod':
            return values
        values['amount_str'] = self._kh_format_amount(amount, currency)
        if company.kh_label_show_khr:
            if currency.name == 'KHR':
                other = company.currency_id if company.currency_id.name != 'KHR' else company._kh_get_usd_currency()
                if other and other != currency:
                    other_amount = other.round(company._kh_from_khr(amount, other))
                    values['secondary_str'] = '≈ %s' % self._kh_format_amount(other_amount, other)
            else:
                amount_khr = company._kh_round_khr(company._kh_to_khr(amount, currency))
                values['secondary_str'] = '≈ %s' % self._kh_format_amount(amount_khr, company._kh_get_khr_currency())
        order = self.sudo().sale_id
        if self.kh_payment_mode == 'auto' and order:
            # Only when goods + delivery is the amount printed: after a partial payment the
            # breakdown would show another total than the amount to collect.
            fee = self._kh_round_cod(self._kh_get_delivery_fee(order), currency)
            total = self._kh_round_cod(order.amount_total, currency)
            if currency.compare_amounts(fee, 0.0) > 0:
                if currency.compare_amounts(total, amount) == 0:
                    values['goods_str'] = self._kh_format_amount(total - fee, currency)
                    values['fee_str'] = self._kh_format_amount(fee, currency)
                    values['lines'].append('fee')
            elif company.kh_label_no_fee_note in NO_FEE_LINES:
                values['nofee_str'] = NO_FEE_LINES[company.kh_label_no_fee_note]
                values['lines'].append('nofee')
        values['lines'] = values['lines'][:PAY_SMALL_LINES_MAX]
        if 'fee' not in values['lines']:
            values['goods_str'] = values['fee_str'] = ''
        if 'nofee' not in values['lines']:
            values['nofee_str'] = ''
        # amount line: what the title and the small lines leave of the height of the COD box
        free_px = PAY_H - PAY_TITLE_H - len(values['lines']) * PAY_SMALL_H
        sizes = tuple(size for size in AMOUNT_SIZES if self._kh_amount_line_px(size) <= free_px) or AMOUNT_SIZES[-1:]
        secondary_mm = text_width_mm(values['secondary_str'], SECONDARY_SIZE, bold=True) + 2.0
        size = pick_size(values['amount_str'], px_to_mm(width_px) - secondary_mm, sizes, bold=True)
        values['amount_size'] = size
        values['amount_style'] = 'font-size: %spt; line-height: %dpx; height: %dpx;' % (
            size, self._kh_amount_line_px(size), self._kh_amount_line_px(size))
        return values

    @api.model
    def _kh_amount_line_px(self, size_pt):
        """Height of the amount line (digits only: no descent, no Khmer stack)."""
        return int(round(size_pt * 4 / 3.0 * 0.95)) + 1

    def _kh_get_label_items(self, max_lines, width_mm=px_to_mm(LABEL_W - CHIPS_CELL_W - ITEMS_PAD_X), lines=3):
        """Items summary: ``(text, more_count, total_qty)``, see :meth:`_kh_label_items_rows`."""
        rows, more, total_qty = self._kh_label_items_rows(max_lines, width_mm=width_mm, lines=lines)
        return ' '.join(rows), more, total_qty

    def _kh_label_items_rows(self, max_lines, width_mm=px_to_mm(LABEL_W - CHIPS_CELL_W - ITEMS_PAD_X), lines=3):
        """Items summary: ``(rows, more_count, total_qty)``.

        Demand quantities before validation, done quantities after; cancelled moves are skipped and
        lines of the same product are merged. Product names are printed without internal reference.
        At most ``max_lines`` products are listed, and only as many as fit ``lines`` rows of
        ``width_mm`` after the bold "ទំនិញ Items (ចំនួន Qty N):" prefix: the others are counted in
        ``more_count`` ("+N more"). When not even the first product fits, it is shortened, or only
        the number of products is printed.
        """
        self.ensure_one()
        done = self.state == 'done'
        quantities = {}
        for move in self.move_ids.filtered(lambda m: m.state != 'cancel'):
            qty = move.quantity if done else move.product_uom_qty
            if qty <= 0:
                continue
            key = (move.product_id.id, move.product_uom.id)
            if key not in quantities:
                quantities[key] = [move.product_id, 0.0]
            quantities[key][1] += qty
        lines_qty = list(quantities.values())
        total_qty = kh_format_qty(sum(qty for _product, qty in lines_qty))
        max_lines = max_lines if max_lines and max_lines > 0 else 3
        entries = ['%s× %s' % (kh_format_qty(qty), kh_truncate(
            product.with_context(display_default_code=False).display_name, MAX_PRODUCT))
            for product, qty in lines_qty[:max_lines]]
        prefix_mm = text_width_mm(ITEMS_PREFIX % total_qty, ITEMS_SIZE, bold=True)

        def wrap(text, more):
            reserve = text_width_mm(' … +%s more' % more, ITEMS_SIZE, bold=True) if more else 0.0
            return wrap_lines(text, width_mm, ITEMS_SIZE, max_lines=lines, indent_mm=prefix_mm, reserve_mm=reserve)

        for count in range(len(entries), 0, -1):
            rows, truncated = wrap(', '.join(entries[:count]), len(lines_qty) - count)
            if not truncated:
                return rows, len(lines_qty) - count, total_qty
        if entries:
            rows, _truncated = wrap(entries[0], len(lines_qty) - 1)
            if rows and count_units(rows[0]) >= 8:
                return rows, len(lines_qty) - 1, total_qty
        return [], len(lines_qty), total_qty

    def _kh_label_courier(self):
        """Courier name printed in the header (hook: override to use another field)."""
        self.ensure_one()
        return self.carrier_id.name or ''

    def _kh_label_date(self):
        """Effective date when done, else the scheduled date, in the user's timezone (dd/mm/yyyy)."""
        self.ensure_one()
        date = self.date_done if self.state == 'done' and self.date_done else self.scheduled_date
        if not date:
            return ''
        return fields.Datetime.context_timestamp(self, date).strftime('%d/%m/%Y')

    def _kh_label_weight(self):
        self.ensure_one()
        weight = self.shipping_weight or self.weight
        if not weight or weight <= 0:
            return ''
        return '%s %s' % (('%.2f' % weight).rstrip('0').rstrip('.'), self.weight_uom_name or 'kg')

    def _kh_label_info_qr(self, company):
        """Information QR code (``kh_label_qr_content``): Google Maps link of the receiver or reference.

        Drawn without border: the white cell of the template is its quiet zone.

        :return: ``{'kind': 'map'|'reference', 'value': encoded text, 'image': data URI}`` or False
        """
        self.ensure_one()
        mode = company.kh_label_qr_content
        if mode not in ('map', 'reference'):
            return False
        partner = self.partner_id.sudo()
        if mode == 'map' and partner and (partner.partner_latitude or partner.partner_longitude):
            url = MAP_URL % (partner.partner_latitude, partner.partner_longitude)
            image = self._kh_barcode_data_uri('QR', url, width=INFO_QR_PIXELS, height=INFO_QR_PIXELS, quiet=1)
            if image:
                return {'kind': 'map', 'value': url, 'image': image}
        # reference mode, or map mode for a contact without geolocation
        image = self._kh_barcode_data_uri('QR', self.name, width=INFO_QR_PIXELS, height=INFO_QR_PIXELS, quiet=1)
        return {'kind': 'reference', 'value': self.name, 'image': image} if image else False

    def _kh_label_pay_qr(self, company):
        """Payment QR code of a COD label (see :meth:`_kh_get_payment_url`).

        :return: ``{'mode', 'url', 'image', 'image_style', 'caption', 'amount_str'}`` or False.
            ``url`` is False for the static KHQR image. The QR codes are drawn without border: the
            white cell of the template is their quiet zone (1.6 mm, also next to the black COD
            box). Long URLs use the error correction level L, which keeps the modules large enough
            for 203 dpi. The uploaded KHQR image is cropped to its ink and converted to black and
            white, so the code itself prints 18 mm wide.
        """
        self.ensure_one()
        mode, url = self._kh_get_payment_url()
        if not mode:
            return False
        amount_str = self._kh_format_amount(self.kh_cod_amount, self.kh_cod_currency_id)
        image_style = 'width: 68px; height: 68px;'
        if mode == 'khqr_image':
            image, ratio = self._kh_black_and_white_data_uri(company.kh_label_khqr_image, max_px=544, trim=True)
            if ratio > 1:
                image_style = 'width: 68px; height: %dpx;' % max(int(68 / ratio), 1)
            elif ratio < 1:
                image_style = 'width: %dpx; height: 68px;' % max(int(68 * ratio), 1)
            caption = PAY_CAPTION_KHQR
        else:
            level = 'M' if len(url.encode()) <= PAY_QR_LEVEL_M_MAX_BYTES else 'L'
            image = self._kh_barcode_data_uri(
                'QR', url, width=PAY_QR_PIXELS, height=PAY_QR_PIXELS, quiet=1, barLevel=level)
            caption = PAY_CAPTION
        if not image:
            return False
        # the template appends " ▶" (pointing at the QR code)
        width = px_to_mm(LABEL_W - PAYQR_CELL_W - COD_PAD_X) - text_width_mm(' ▶', PAY_SMALL_SIZE, bold=True)
        return {
            'mode': mode,
            'url': url,
            'image': image,
            'image_style': image_style,
            'caption': fit_width('%s %s' % (caption, amount_str), width, PAY_SMALL_SIZE, bold=True),
            'amount_str': amount_str,
        }

    def _kh_info_qr_place(self, info_qr, pay_qr):
        """Where the information QR prints: ``'payment'`` (QR slot of the payment section),
        ``'receiver'`` (next to the province box) or False (omitted).

        The payment QR always wins the QR slot of the payment section. On such COD labels the map
        QR moves to the receiver section (the receiver texts get a narrower column), while a
        reference QR is omitted: the Code128 barcode of the footer carries the same reference, and
        the receiver name / address keep their full width. Override to change the rule.
        """
        self.ensure_one()
        if not info_qr:
            return False
        if not pay_qr:
            return 'payment'
        return 'receiver' if info_qr['kind'] == 'map' else False

    @api.model
    def _kh_split_province(self, province):
        """Split "ខេត្តកណ្តាល" into ("ខេត្ត", "កណ្តាល") so the box can break after the prefix."""
        for prefix in PROVINCE_PREFIXES:
            if province.startswith(prefix) and len(province) > len(prefix):
                return prefix, province[len(prefix):].strip()
        return '', province

    def _kh_header_values(self, company, logo):
        """Header: company name, tagline and phone; transfer / order references; courier."""
        self.ensure_one()
        values = {}
        # sender: the name gets the largest size at which it fits with the tagline and the phone
        name = kh_truncate(company.name, MAX_COMPANY)
        width = px_to_mm(HEAD_COMPANY_W - HEAD_PAD_X - (LOGO_CELL_W if logo else 0))
        tagline = kh_truncate(company.kh_label_tagline, MAX_TAGLINE)
        tagline_script = script_of(tagline)
        tagline_h = text_box(TAGLINE_SIZE, 1, tagline_script, clip=False)[1] if tagline else 0
        phone_h = text_box(COMPANY_PHONE_SIZE, 1, 'digits')[1]
        script = script_of(name)
        sizes = tuple(size for size in COMPANY_SIZES
                      if text_box(size, 1, script)[1] + tagline_h + phone_h <= HEAD_H) or COMPANY_SIZES[-1:]
        size = pick_size(name, width, sizes, bold=True)
        values.update({
            'company_name': fit_width(name, width, size, bold=True),
            'company_style': box_style(size, 1, script),
            'tagline': fit_width(tagline, width, TAGLINE_SIZE) if tagline else '',
            'tagline_style': box_style(TAGLINE_SIZE, 1, tagline_script, clip=False),
            'company_phone_style': box_style(COMPANY_PHONE_SIZE, 1, 'digits'),
        })
        # references: the end of the transfer reference (its number) is never cut
        width = px_to_mm(HEAD_REF_W - HEAD_SEP - HEAD_PAD_X)
        size = pick_size(self.name, width, REF_SIZES, bold=True)
        values.update({
            'reference_header': fit_start(self.name, width, size, bold=True),
            'reference_style': box_style(size),
            'meta_style': box_style(META_SIZE),
            'parcel_label_style': box_style(6, 1, 'caption'),
        })
        # courier
        courier = kh_truncate(self._kh_label_courier(), MAX_COURIER)
        script = script_of(courier)
        width = px_to_mm(HEAD_COURIER_W - HEAD_SEP - HEAD_PAD_X)
        sizes = tuple(size for size in COURIER_SIZES
                      if text_box(size, 1, script)[1] + PARCEL_LABEL_H + PARCEL_LINE_H <= HEAD_H) or COURIER_SIZES[-1:]
        size = pick_size(courier, width, sizes, bold=True)
        values.update({
            'courier': fit_width(courier, width, size, bold=True) if courier else '',
            'courier_style': box_style(size, 1, script),
        })
        return values

    def _kh_receiver_values(self, partner, company, with_info_qr=False):
        """Receiver block: cleaned name, phones, address and province box with their sizes.

        The heights of the name, phone and address boxes are computed so that Khmer subscripts
        and lower vowels are never clipped: a Khmer name prints at most 11 pt in a taller box, and
        the address gets as many lines (3 Latin / 2 Khmer at most) as the room left allows.

        :param bool with_info_qr: the information QR code prints in the receiver section (next to
            the province box, which is then narrower), so the texts get less room.
        """
        self.ensure_one()
        raw_name = partner.name or partner.commercial_partner_id.name or partner.display_name or ''
        phones = [phone.strip() for phone in PHONE_SPLIT_RE.split(self.kh_receiver_phone or '') if phone.strip()][:2]
        province = (partner.state_id.name or '').strip()
        city = (partner.city or '').strip()
        if not province:
            province, city = city, ''
        address_parts = [partner.street, partner.street2]
        if partner.country_id and company.country_id and partner.country_id != company.country_id:
            address_parts.append(partner.country_id.name)
        province = kh_truncate(province, MAX_PROVINCE)
        province_cell = (PROVINCE_CELL_W_INFO if with_info_qr else PROVINCE_CELL_W) if province else 0
        width_px = LABEL_W - RECV_PAD_X - province_cell - (INFO_CELL_W if with_info_qr else 0)
        width = px_to_mm(width_px)

        name = kh_truncate(self._kh_clean_receiver_name(raw_name), MAX_NAME)
        name_script = script_of(name)
        name_size = pick_size(name, width, NAME_SIZES_KHMER if name_script == 'khmer' else NAME_SIZES, bold=True)
        name = fit_width(name, width, name_size, bold=True)
        name_h = text_box(name_size, 1, name_script)[1]

        # One phone line; a second number shares it unless the first one would get too small,
        # then it moves to its own line (and the address keeps less room).
        phone_size, phone2_size, phone2_line = PHONE_SIZES[0], PHONE2_SIZE, False
        if phones:
            first = '☎ ' + phones[0]
            phone_size = pick_size(first, width, PHONE_SIZES, bold=True)
            if len(phones) > 1:
                second_mm = text_width_mm(' / ' + phones[1], PHONE2_SIZE, bold=True)
                shared_size = pick_size(first, width - second_mm, PHONE_SIZES, bold=True)
                fits = text_width_mm(first, shared_size, bold=True) + second_mm <= width
                if fits and shared_size >= PHONE_MIN_SIZE_SHARED:
                    phone_size = shared_size
                else:
                    phone2_line = True
        phone_h = self._kh_phone_line_px(phone_size)
        phone2_h = text_box(PHONE2_SIZE, 1, 'digits')[1] if phone2_line else 0

        room = RECV_H - RECV_TAG_H - name_h - phone_h - phone2_h
        address = kh_truncate(', '.join(part.strip() for part in address_parts if part and part.strip()), MAX_ADDRESS)
        address_script = script_of(address)
        address_rows, address_size = [], ADDRESS_SIZES[0]
        for size in (ADDRESS_SIZES if address else ()):
            # the largest size, unless a smaller one gets more of the address on the label
            max_rows = next((lines for lines in range(ADDRESS_LINES, 0, -1)
                             if text_box(size, lines, address_script)[1] <= room), 0)
            rows, truncated = wrap_lines(address, width, size, max_lines=max_rows)
            if not address_rows or len(rows) > len(address_rows) or (len(rows) == len(address_rows) and not truncated):
                address_rows, address_size = rows, size
            if not truncated:
                break
        blank_lines = 0
        if not address_rows:
            blank_lines = next((lines for lines in (2, 1)
                                if text_box(ADDRESS_BLANK_SIZE, lines, 'caption')[1] <= room), 0)

        # province box: prefix ("ខេត្ត"), name, then the city / district (riders route by it)
        prefix, main = self._kh_split_province(province)
        province_width = px_to_mm(province_cell - PROVINCE_TEXT_PAD)
        province_size = pick_size(main, province_width, PROVINCE_SIZES, bold=True)
        province_rows, _truncated = wrap_lines(main, province_width, province_size, bold=True, max_lines=2)
        city = kh_truncate(city, MAX_PROVINCE)
        city_rows, _truncated = wrap_lines(city, province_width, PROVINCE_CITY_SIZE, bold=True, max_lines=2)
        return {
            'receiver_name': name,
            'name_size': name_size,
            'name_style': box_style(name_size, 1, name_script),
            'phones': phones,
            'phone_size': phone_size,
            'phone_style': 'font-size: %spt; line-height: %dpx; height: %dpx;' % (phone_size, phone_h, phone_h),
            'phone2_size': phone2_size,
            'phone2_line': phone2_line,
            'phone2_style': box_style(PHONE2_SIZE, 1, 'digits'),
            'address': ' '.join(address_rows),
            'address_rows': address_rows,
            'address_lines': len(address_rows),
            'address_style': box_style(address_size, max(len(address_rows), 1), address_script, max_height=True),
            'address_blank_lines': blank_lines,
            'address_blank_style': box_style(ADDRESS_BLANK_SIZE, max(blank_lines, 1), 'caption'),
            'province': province,
            'province_prefix': prefix,
            'province_main': main,
            'province_size': province_size,
            'province_rows': province_rows,
            'province_style': box_style(province_size, max(len(province_rows), 1), script_of(main)),
            'province_city': ' '.join(city_rows),
            'province_city_rows': city_rows,
            'province_city_style': box_style(PROVINCE_CITY_SIZE, max(len(city_rows), 1), script_of(city)),
        }

    @api.model
    def _kh_phone_line_px(self, size_pt):
        """Height of the phone line: digits and the ☎ icon (no descent, no Khmer stack)."""
        return int(round(size_pt * 4 / 3.0 * 0.88))

    def _kh_items_values(self, company):
        """Items summary and note: their rows share the height of the items section.

        The note has priority: one row when it fits, else two (all the rows when the items
        summary is disabled); the items summary gets the rows left. Khmer texts get a taller line
        pitch so their subscripts do not overprint the next row.
        """
        self.ensure_one()
        width = px_to_mm(LABEL_W - ITEMS_PAD_X - CHIPS_CELL_W)  # the "allow check" chip always prints
        show_items = company.kh_label_show_items
        note = kh_truncate(self.kh_label_note, MAX_NOTE)
        # the bold Khmer prefix of the note / items summary ("ចំណាំ", "ទំនិញ") is on their first row
        note_script = 'khmer' if has_khmer(note) else 'prefixed'
        note_rows = []
        room = ITEMS_H
        if note:
            prefix_mm = text_width_mm(NOTE_PREFIX, NOTE_SIZE, bold=True)
            if not show_items:
                max_rows = next((lines for lines in (3, 2, 1)
                                 if text_box(NOTE_SIZE, lines, note_script)[1] <= room), 1)
            else:
                max_rows = 2
            note_rows, _truncated = wrap_lines(note, width, NOTE_SIZE, max_lines=max_rows, indent_mm=prefix_mm)
            room -= text_box(NOTE_SIZE, len(note_rows), note_script)[1]
        items_rows, items_more, total_qty = [], 0, '0'
        items_lines = 0
        items_script = 'khmer' if has_khmer(' '.join(self.move_ids.product_id.mapped('name'))) else 'prefixed'
        if show_items:
            items_lines = next((lines for lines in (3, 2, 1)
                                if text_box(ITEMS_SIZE, lines, items_script)[1] <= room), 0)
            if items_lines:
                items_rows, items_more, total_qty = self._kh_label_items_rows(
                    company.kh_label_max_item_lines, width_mm=width, lines=items_lines)
        return {
            'show_items': show_items,
            'items': ' '.join(items_rows),
            'items_rows': items_rows,
            'items_more': items_more,
            'total_qty': total_qty,
            'items_lines': items_lines,
            'items_style': box_style(ITEMS_SIZE, max(len(items_rows), 1), items_script, max_height=True),
            'note': ' '.join(note_rows),
            'note_rows': note_rows,
            'note_lines': len(note_rows),
            'note_style': box_style(NOTE_SIZE, max(len(note_rows), 1), note_script, max_height=True),
        }

    def _kh_footer_values(self, company):
        """Footer: Code128 barcode of the transfer (HTML bars) and the thank-you text (2 lines)."""
        self.ensure_one()
        barcode = self._kh_label_barcode(self.name)
        scale = barcode['scale'] if barcode else 1.0
        quiet = int(math.ceil(BARCODE_QUIET_MODULES * scale))
        printed = int(math.ceil((barcode['width'] if barcode else 172) * scale))
        barcode_cell = printed + 2 * quiet
        footer = kh_truncate(company.kh_label_footer, MAX_FOOTER)
        width = px_to_mm(LABEL_W - barcode_cell - FOOTER_PAD_X)
        footer_rows, _truncated = wrap_lines(footer, width, FOOTER_SIZE, max_lines=2)
        return {
            'barcode_bars': barcode and barcode['bars'],
            'barcode_width': barcode and barcode['width'],
            'barcode_style': barcode and (
                'width: %(w)dpx; -webkit-transform: scale(%(s)s, 1); transform: scale(%(s)s, 1);' % {
                    'w': barcode['width'], 's': barcode['scale']}),
            'barcode_text_style': 'width: %dpx;' % printed,
            'barcode_cell_style': 'width: %dpx; padding-left: %dpx;' % (barcode_cell, quiet),
            # fallback when the reference cannot be drawn as bars: the PNG of ir.actions.report
            'barcode': not barcode and self._kh_barcode_data_uri('Code128', self.name, width=1400, height=160, quiet=0),
            'footer': ' '.join(footer_rows),
            'footer_rows': footer_rows,
            'footer_style': box_style(FOOTER_SIZE, max(len(footer_rows), 1), script_of(footer)),
        }

    def _kh_prepare_label_values(self, cache=None):
        """Values of one label of this transfer (parcel numbering is added by the caller)."""
        self.ensure_one()
        cache = cache if cache is not None else {}
        company = self.company_id or self.env.company
        partner = self.partner_id.sudo()
        order = self.sudo().sale_id

        # Sender (computed once per company and print job)
        if company.id not in cache:
            company_phone = (self._kh_extract_phone_list(company.partner_id, limit=1) or [company.phone or ''])[0]
            logo = self._kh_label_logo(company)
            cache[company.id] = {
                'logo': logo and logo[0],
                'company_phone': company_phone,
            }
        company_values = cache[company.id]

        # QR codes: the payment QR (COD labels only) takes the QR slot of the payment section; the
        # information QR then moves to the receiver section (map) or is omitted (reference).
        pay_qr = self._kh_label_pay_qr(company)
        info_qr = self._kh_label_info_qr(company)
        info_place = self._kh_info_qr_place(info_qr, pay_qr)
        if not info_place:
            info_qr = False
        if pay_qr:
            pay_width = LABEL_W - PAYQR_CELL_W - COD_PAD_X
        elif info_place == 'payment':
            pay_width = LABEL_W - INFO_PAY_CELL_W - COD_PAD_X
        else:
            pay_width = LABEL_W - COD_PAD_X
        parcel_count = min(max(self.kh_parcel_count or 1, 1), MAX_PARCELS)
        extra_lines = (['parcels'] if parcel_count > 1 else []) + (['pay'] if pay_qr else [])
        money = self._kh_get_money_values(pay_width, extra_lines=extra_lines)
        if info_qr and info_qr['kind'] == 'map':
            caption_lines = MAP_CAPTION_LINES_SHORT if info_place == 'receiver' else MAP_CAPTION_LINES
        elif info_qr:
            caption_lines = (kh_truncate(info_qr['value'], MAX_REFERENCE),)
        else:
            caption_lines = ()

        values = {
            'picking': self,
            'company': company,
            # header
            'logo': company_values['logo'],
            'company_phone': company_values['company_phone'],
            'reference': self.name,
            'order_ref': order.name or self.origin or '',
            'date': self._kh_label_date(),
            'weight': self._kh_label_weight(),
            # payment and QR codes
            'money': money,
            'pay_qr': pay_qr,
            'info_qr': info_qr and info_qr['image'],
            'info_qr_kind': info_qr and info_qr['kind'],
            'info_qr_place': info_place,
            'info_qr_caption_lines': caption_lines,
            # handling instructions
            'allow_check': self.kh_allow_check,
            'fragile': self.kh_fragile,
        }
        values.update(self._kh_header_values(company, values['logo']))
        values.update(self._kh_items_values(company))
        values.update(self._kh_footer_values(company))
        values.update(self._kh_receiver_values(partner, company, with_info_qr=info_place == 'receiver'))
        return values

    def _kh_get_label_values(self):
        """One dict per label to print: ``kh_parcel_count`` labels per transfer, numbered 1/N ... N/N.

        The payment QR prints on parcel 1/N only (the amount is the total of the shipment, paid
        once): the other parcels say where it is.
        """
        labels = []
        cache = {}
        for picking in self:
            values = picking._kh_prepare_label_values(cache)
            count = min(max(picking.kh_parcel_count or 1, 1), MAX_PARCELS)
            for index in range(1, count + 1):
                labels.append(dict(
                    values, parcel_index=index, parcel_count=count, parcel_label='%s/%s' % (index, count),
                    pay_qr_here=bool(values['pay_qr']) and index == 1))
        return labels

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_print_cod_label(self):
        """Print the 100 x 80 mm COD label(s) of the selected transfers.

        ``config=False``: the label has its own header and does not use the company document
        layout, so administrators are never sent to the document layout configurator first.
        """
        self._kh_check_printable()
        return self.env.ref('delivery_label_cod_kh.action_report_cod_label').report_action(self, config=False)
