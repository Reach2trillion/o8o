# -*- coding: utf-8 -*-
import base64
import io
import logging
import re

from PIL import Image
from reportlab.graphics.barcode.code128 import Code128

try:
    from reportlab.graphics.utils import RenderPMError
except ImportError:  # pragma: no cover - older reportlab
    class RenderPMError(Exception):
        pass

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.image import image_data_uri

from .kh_label_text import fit_width, pick_size, text_width_mm, truncate as kh_truncate

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

# Server-side truncation of the label texts (characters); the texts are also fitted to the width
# of their box (see kh_label_text.py) and the template clips every box as a safety net.
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

# Cambodian phone numbers: +855 / 855 / 00855 (optionally followed by "0" or "(0)") or a leading 0,
# then 8 or 9 digits (9-10 digits in local format), optionally separated by spaces, dots or dashes.
PHONE_RE = re.compile(
    r'(?<![\d+])'
    r'(?:(?:\+|00)?\s*855[\s.\-]*(?:\(0\)|0)?[\s.\-]*|0)'
    r'\d(?:[\s.\-]?\d){7,8}'
    r'(?!\d)'
)
PHONE_SPLIT_RE = re.compile(r'\s*[/,;|]\s*')

MAP_URL = 'https://maps.google.com/?q=%.7f,%.7f'

# Usable text widths (mm) of the label boxes and font sizes (pt), mirrored from the template CSS.
W_RECEIVER = 65.5       # receiver column next to the province box
W_RECEIVER_FULL = 93.0  # receiver column when there is no province
W_PROVINCE = 23.0       # inside the province box
W_PAYMENT = 67.0        # COD box next to the QR code
W_PAYMENT_FULL = 92.0   # COD box without QR code
W_ITEMS = 52.5          # items / note column next to the handling chips
W_ITEMS_FULL = 93.0     # items / note column without chips
NAME_SIZES = (13, 11, 9)
PHONE_SIZES = (18, 16, 14, 12)
PHONE2_SIZE = 10
AMOUNT_SIZES = (25, 22, 19, 16)
AMOUNT_MAX_SIZE_MULTI_PARCEL = 21  # leaves room for the "total for N parcels" line
SECONDARY_SIZE = 12
PROVINCE_SIZES = (15, 13, 11.5, 10)
ADDRESS_SIZE = 8
ITEMS_SIZE = 6.5
NOTE_SIZE = 7
ITEMS_TEXT_LINES = 3    # lines shared by the items summary and the note
NOTE_PREFIX = 'ចំណាំ Note: '
PROVINCE_PREFIXES = ('រាជធានី', 'ខេត្ត', 'ក្រុង')


def kh_normalize_phone(text):
    """Return the local 0XXXXXXXX(X) digits of a Cambodian number, or False."""
    digits = re.sub(r'\D', '', text or '')
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


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    kh_payment_mode = fields.Selection(
        PAYMENT_MODES, string='COD Mode', default='auto', required=True, copy=False, tracking=True,
        help="Auto: amount still due on the sales order (total minus posted invoice payments).\n"
             "Manual: the amount entered below.\n"
             "Paid: the label says PAID - DO NOT COLLECT.")
    kh_cod_currency_id = fields.Many2one(
        'res.currency', string='COD Currency', compute='_compute_kh_cod_currency_id')
    kh_cod_amount_manual = fields.Monetary(
        string='Manual COD Amount', currency_field='kh_cod_currency_id', copy=False, tracking=True,
        help="Amount the courier must collect, used when the COD mode is 'Manual amount'. "
             "Zero prints the label as PAID.")
    kh_cod_amount_auto = fields.Monetary(
        string='Amount Due on Order', currency_field='kh_cod_currency_id', compute='_compute_kh_cod',
        help="What the 'Auto' mode prints: sales order total minus the paid part of its posted "
             "invoices (credit notes deducted) or its online payment transactions.")
    kh_cod_amount = fields.Monetary(
        string='COD Amount', currency_field='kh_cod_currency_id', compute='_compute_kh_cod',
        help="Amount printed on the COD label.")
    kh_cod_state = fields.Selection(
        COD_STATES, string='COD Status', compute='_compute_kh_cod')
    kh_cod_warning = fields.Char(string='COD Warning', compute='_compute_kh_cod_warning')
    kh_receiver_phone = fields.Char(
        string='Receiver Phone', compute='_compute_kh_receiver_phone', store=True, readonly=False,
        help="Phone number(s) printed on the label, taken from the contact's phone, mobile or from "
             "a number typed in the contact name. Editable; recomputed when the contact changes.")
    kh_parcel_count = fields.Integer(
        string='Parcels', default=1, copy=False,
        help="Number of parcels: one label is printed per parcel (1/N ... N/N).")
    kh_fragile = fields.Boolean(
        string='Fragile', default=lambda self: self.env.company.kh_label_fragile_default)
    kh_allow_check = fields.Boolean(
        string='Allow to Check Goods',
        default=lambda self: self.env.company.kh_label_allow_check_default,
        help="The customer may open and check the goods before paying.")
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
    @api.depends('sale_id.currency_id', 'company_id.currency_id')
    def _compute_kh_cod_currency_id(self):
        for picking in self:
            picking.kh_cod_currency_id = (
                picking.sudo().sale_id.currency_id or picking.company_id.currency_id or self.env.company.currency_id)

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

    @api.depends('kh_payment_mode', 'kh_cod_state', 'sale_id.picking_ids.state')
    def _compute_kh_cod_warning(self):
        for picking in self:
            deliveries = picking._kh_get_ambiguous_deliveries()
            picking.kh_cod_warning = (
                self._kh_ambiguity_message(picking.sudo().sale_id, deliveries) if deliveries else False)

    @api.depends('partner_id', 'partner_id.name', 'partner_id.phone', 'partner_id.mobile',
                 'partner_id.commercial_partner_id.phone', 'partner_id.commercial_partner_id.mobile')
    def _compute_kh_receiver_phone(self):
        for picking in self:
            picking.kh_receiver_phone = self._kh_extract_phones(picking.partner_id) or False

    # ------------------------------------------------------------------
    # COD rules
    # ------------------------------------------------------------------
    def _kh_is_cod_candidate(self):
        """Only outgoing transfers that are not returns can carry a COD amount."""
        self.ensure_one()
        return self.picking_type_code == 'outgoing' and not self.return_id

    def _kh_cod_auto_amount(self):
        """Amount due on the linked sales order, in ``kh_cod_currency_id``.

        :return: tuple ``(amount, state)`` where state is ``'cod'``, ``'paid'`` or ``'none'``
        """
        self.ensure_one()
        if not self._kh_is_cod_candidate():
            return 0.0, 'none'
        if 'pos_order_id' in self._fields and self.sudo().pos_order_id:
            # Point of Sale orders are paid at the till.
            return 0.0, 'paid'
        order = self.sudo().sale_id
        if not order:
            return 0.0, 'none'
        currency = order.currency_id
        due = currency.round(max(order.amount_total - self._kh_order_paid_amount(order), 0.0))
        if currency.compare_amounts(due, 0.0) > 0:
            return due, 'cod'
        return 0.0, 'paid'

    @api.model
    def _kh_order_paid_amount(self, order):
        """Paid part of ``order``, in the order currency.

        Sum over the posted customer invoices of ``amount_total - amount_residual`` minus the same
        for posted credit notes, or the online payment transactions of the order when higher
        (``max`` avoids counting twice a transaction that is also reconciled with an invoice).
        Draft invoices count as unpaid.
        """
        order = order.sudo()
        currency = order.currency_id
        today = fields.Date.context_today(self)
        paid = 0.0
        moves = order.invoice_ids.filtered(
            lambda m: m.state == 'posted' and m.move_type in ('out_invoice', 'out_refund'))
        for move in moves:
            amount = move.amount_total - move.amount_residual
            if move.currency_id != currency:
                amount = move.currency_id._convert(
                    amount, currency, order.company_id, move.invoice_date or today, round=False)
            paid += amount if move.move_type == 'out_invoice' else -amount
        return max(paid, order.amount_paid or 0.0)

    def _kh_cod_resolve(self, auto_amount, auto_state):
        """Apply the COD mode of the transfer to the automatic result."""
        self.ensure_one()
        if not self._kh_is_cod_candidate():
            return 0.0, 'none'
        currency = self.kh_cod_currency_id
        if self.kh_payment_mode == 'paid':
            return 0.0, 'paid'
        if self.kh_payment_mode == 'manual':
            amount = currency.round(max(self.kh_cod_amount_manual or 0.0, 0.0))
            if currency.compare_amounts(amount, 0.0) > 0:
                return amount, 'cod'
            return 0.0, 'paid'
        return auto_amount, auto_state

    @api.model
    def _kh_get_delivery_fee(self, order):
        """Total (tax included) of the delivery lines of ``order``."""
        return sum(order.sudo().order_line.filtered('is_delivery').mapped('price_total'))

    def _kh_get_ambiguous_deliveries(self):
        """Deliveries of the same sales order when the automatic COD amount cannot be printed.

        When an order with money to collect has several non-cancelled outgoing transfers
        (backorders, partial deliveries), printing the full due amount on each label would
        collect it twice: the mode must be set to Manual or Paid on those transfers.
        Multi-step pick / pack transfers are internal and do not count.
        """
        self.ensure_one()
        empty = self.browse()
        if self.kh_payment_mode != 'auto' or self.kh_cod_state != 'cod':
            return empty
        order = self.sudo().sale_id
        if not order:
            return empty
        deliveries = order.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state != 'cancel' and not p.return_id)
        if len(deliveries) > 1:
            return self.browse(deliveries.ids)
        return empty

    @api.model
    def _kh_ambiguity_message(self, order, deliveries):
        return _(
            "Order %(order)s has %(count)s deliveries (%(refs)s): set the COD mode to Manual or Paid on "
            "these deliveries before printing the COD label, so the amount is not collected twice.",
            order=order.name, count=len(deliveries), refs=', '.join(deliveries.sudo().mapped('name')))

    def _kh_check_cod_ambiguity(self):
        """Raise a UserError listing every order whose automatic COD amount cannot be split."""
        messages = []
        seen_orders = set()
        for picking in self:
            deliveries = picking._kh_get_ambiguous_deliveries()
            order = picking.sudo().sale_id
            if deliveries and order.id not in seen_orders:
                seen_orders.add(order.id)
                messages.append(self._kh_ambiguity_message(order, deliveries))
        if messages:
            raise UserError('\n'.join(messages))

    # ------------------------------------------------------------------
    # Receiver phone
    # ------------------------------------------------------------------
    @api.model
    def _kh_phone_candidates(self, partner):
        """``(display, key)`` phone candidates of one partner: phone, mobile, then numbers in the name."""
        candidates = []
        for value in (partner.phone, partner.mobile):
            value = re.sub(r'\s+', ' ', value or '').strip()
            if not value:
                continue
            numbers = [kh_normalize_phone(match.group(0)) for match in PHONE_RE.finditer(value)]
            numbers = [number for number in numbers if number]
            if not numbers:
                number = kh_normalize_phone(value)
                numbers = [number] if number else []
            if numbers:
                candidates += [(kh_format_phone(number), number) for number in numbers]
            else:
                # Not a Cambodian number: keep it as typed.
                candidates.append((value, re.sub(r'\D', '', value) or value.lower()))
        for match in PHONE_RE.finditer(partner.name or ''):
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
        """Remove the phone numbers staff typed in the contact name ("Dara 095634706" -> "Dara")."""
        name = re.sub(r'\s+', ' ', name or '').strip()
        cleaned = PHONE_RE.sub(' ', name)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ,;:-/|()')
        return cleaned or name

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------
    @api.model
    def _kh_format_amount(self, amount, currency):
        """``$17.00`` (currency symbol, position and decimals) or ``69,100៛`` for riel."""
        if currency.name == 'KHR':
            return '{:,.0f}៛'.format(round(amount))
        text = '{:,.{prec}f}'.format(amount, prec=max(currency.decimal_places, 0))
        symbol = currency.symbol or currency.name
        if currency.position == 'after':
            return '%s %s' % (text, symbol)
        return '%s%s' % (symbol, text)

    @api.model
    def _kh_barcode_data_uri(self, barcode_type, value, **kwargs):
        """PNG barcode / QR code as a ``data:`` URI (rendered server side, no HTTP fetch).

        Uses ``ir.actions.report.barcode()``. When reportlab has no bitmap backend (reportlab 4
        installed from pip without ``rl_renderPM`` / ``rlPyCairo``), the image is drawn with PIL
        instead so the label still carries a scannable code.
        """
        if not value:
            return False
        try:
            png = self.env['ir.actions.report'].barcode(barcode_type, value, **kwargs)
        except (ValueError, AttributeError):
            _logger.warning("Cannot render %s barcode for %r on the COD label", barcode_type, value)
            return False
        except RenderPMError:
            png = self._kh_barcode_png_fallback(barcode_type, value, **kwargs)
            if not png:
                return False
        return 'data:image/png;base64,%s' % base64.b64encode(png).decode()

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
                barcode = Code128(value)
                barcode.validate()
                barcode.encode()
                barcode.decompose()
                module_px = 4
                widths = [(char.isupper(), ord(char.lower()) - ord('a') + 1) for char in barcode.decomposed]
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
    def _kh_label_logo(self, company):
        """Company logo converted to grayscale (thermal printers dither colours), as a data URI."""
        if not company.logo or company.uses_default_logo:
            return False
        try:
            image = Image.open(io.BytesIO(base64.b64decode(company.logo)))
            image.load()
            if image.mode == 'P':
                image = image.convert('RGBA')
            if image.mode in ('RGBA', 'LA'):
                alpha = image.getchannel('A')
                gray = image.convert('L')
                gray.putalpha(alpha)
            else:
                gray = image.convert('L')
            gray.thumbnail((400, 400))
            output = io.BytesIO()
            gray.save(output, format='PNG')
            return 'data:image/png;base64,%s' % base64.b64encode(output.getvalue()).decode()
        except Exception:  # noqa: BLE001 - SVG or unreadable image: print it as stored
            return image_data_uri(company.logo)

    # ------------------------------------------------------------------
    # Label values
    # ------------------------------------------------------------------
    def _kh_get_money_values(self, width_mm=W_PAYMENT):
        """Payment block of the label: state, primary / secondary amounts and fee breakdown."""
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
            'secondary_str': '',
            'goods_str': '',
            'fee_str': '',
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
        secondary_mm = text_width_mm(values['secondary_str'], SECONDARY_SIZE, bold=True) + 2.0
        values['amount_size'] = pick_size(
            values['amount_str'], width_mm - secondary_mm, AMOUNT_SIZES, bold=True)
        order = self.sudo().sale_id
        if self.kh_payment_mode == 'auto' and order:
            fee = currency.round(self._kh_get_delivery_fee(order))
            if currency.compare_amounts(fee, 0.0) > 0:
                values['goods_str'] = self._kh_format_amount(order.amount_total - fee, currency)
                values['fee_str'] = self._kh_format_amount(fee, currency)
        return values

    def _kh_get_label_items(self, max_lines, width_mm=W_ITEMS_FULL, lines=3):
        """Items summary: ``(text, more_count, total_qty)``.

        Demand quantities before validation, done quantities after; cancelled moves are skipped and
        lines of the same product are merged. Product names are printed without internal reference.
        At most ``max_lines`` products are listed, and only as many as fit ``lines`` lines of
        ``width_mm``: the others are counted in ``more_count`` ("+N more").
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
        # room left after the bold "ទំនិញ Items (ចំនួន Qty N):" prefix and a "+N more" suffix
        budget = width_mm * lines * 0.9 - text_width_mm(
            'ទំនិញ Items (ចំនួន Qty %s): … +9 more' % total_qty, ITEMS_SIZE, bold=True)
        texts = []
        for product, qty in lines_qty[:max_lines]:
            name = product.with_context(display_default_code=False).display_name
            text = '%s× %s' % (kh_format_qty(qty), kh_truncate(name, MAX_PRODUCT))
            candidate = ', '.join(texts + [text])
            if texts and text_width_mm(candidate, ITEMS_SIZE) > budget:
                break
            texts.append(text)
        if texts and text_width_mm(', '.join(texts), ITEMS_SIZE) > budget:
            # not even the first product fits: shorten it, or only print the number of products
            texts = [fit_width(texts[0], budget, ITEMS_SIZE)] if budget >= 14 else []
        return ', '.join(texts), len(lines_qty) - len(texts), total_qty

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

    def _kh_label_qr(self, company, money_state):
        """``(data_uri, caption)`` of the QR slot according to the company setting."""
        self.ensure_one()
        mode = company.kh_label_qr_content
        if mode == 'none':
            return False, ''
        partner = self.partner_id
        if mode == 'khqr' and money_state == 'cod' and company.kh_label_khqr_image:
            return image_data_uri(company.kh_label_khqr_image), 'ABA KHQR'
        if mode == 'map' and partner and (partner.partner_latitude or partner.partner_longitude):
            url = MAP_URL % (partner.partner_latitude, partner.partner_longitude)
            qr = self._kh_barcode_data_uri('QR', url, width=360, height=360, quiet=0, barBorder=2)
            if qr:
                return qr, 'ស្កេនមើលទីតាំង\nScan for map'
        qr = self._kh_barcode_data_uri('QR', self.name, width=360, height=360, quiet=0, barBorder=2)
        return qr, kh_truncate(self.name, MAX_REFERENCE) if qr else ''

    @api.model
    def _kh_split_province(self, province):
        """Split "ខេត្តកណ្តាល" into ("ខេត្ត", "កណ្តាល") so the box can break after the prefix."""
        for prefix in PROVINCE_PREFIXES:
            if province.startswith(prefix) and len(province) > len(prefix):
                return prefix, province[len(prefix):].strip()
        return '', province

    def _kh_receiver_values(self, partner, company):
        """Receiver block: cleaned name, phones, address and province with their font sizes."""
        self.ensure_one()
        raw_name = partner.name or partner.commercial_partner_id.name or partner.display_name or ''
        phones = [phone.strip() for phone in PHONE_SPLIT_RE.split(self.kh_receiver_phone or '') if phone.strip()][:2]
        province = (partner.state_id.name or '').strip()
        address_parts = [partner.street, partner.street2]
        if province:
            address_parts.append(partner.city)
        else:
            province = (partner.city or '').strip()
        if partner.country_id and company.country_id and partner.country_id != company.country_id:
            address_parts.append(partner.country_id.name)
        province = kh_truncate(province, MAX_PROVINCE)
        width = W_RECEIVER if province else W_RECEIVER_FULL

        name = kh_truncate(self._kh_clean_receiver_name(raw_name), MAX_NAME)
        name_size = pick_size(name, width, NAME_SIZES, bold=True)
        name = fit_width(name, width, name_size, bold=True)

        phone_size, phone2_size = PHONE_SIZES[0], PHONE2_SIZE
        if phones:
            second_mm = text_width_mm(' / ' + phones[1], PHONE2_SIZE, bold=True) if len(phones) > 1 else 0.0
            phone_size = pick_size('☎ ' + phones[0], width - second_mm, PHONE_SIZES, bold=True)

        address = kh_truncate(', '.join(part.strip() for part in address_parts if part and part.strip()), MAX_ADDRESS)
        address = fit_width(address, width, ADDRESS_SIZE, lines=3)

        prefix, main = self._kh_split_province(province)
        province_size = pick_size(main, W_PROVINCE, PROVINCE_SIZES, bold=True)
        return {
            'receiver_name': name,
            'name_size': name_size,
            'phones': phones,
            'phone_size': phone_size,
            'phone2_size': phone2_size,
            'address': address,
            'province': province,
            'province_prefix': prefix,
            'province_main': main,
            'province_size': province_size,
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
            cache[company.id] = {
                'logo': self._kh_label_logo(company),
                'company_phone': company_phone,
            }
        company_values = cache[company.id]

        # Payment and QR code
        qr_state = self.kh_cod_state
        qr, qr_caption = self._kh_label_qr(company, qr_state)
        money = self._kh_get_money_values(W_PAYMENT if qr else W_PAYMENT_FULL)

        # Items and handling instructions: the note has priority over the items summary
        has_chips = self.kh_fragile or self.kh_allow_check
        items_width = W_ITEMS if has_chips else W_ITEMS_FULL
        show_items = company.kh_label_show_items
        note = kh_truncate(self.kh_label_note, MAX_NOTE)
        note_lines = 0
        if note:
            if not show_items:
                note_lines = ITEMS_TEXT_LINES
            elif text_width_mm(NOTE_PREFIX + note, NOTE_SIZE, bold=True) <= items_width:
                note_lines = 1
            else:
                note_lines = 2
            note = fit_width(NOTE_PREFIX + note, items_width, NOTE_SIZE, bold=True, lines=note_lines)
            note = note[len(NOTE_PREFIX):] if note.startswith(NOTE_PREFIX) else note
        items, items_more, total_qty = ('', 0, '0')
        items_lines = ITEMS_TEXT_LINES - note_lines if show_items else 0
        if items_lines:
            items, items_more, total_qty = self._kh_get_label_items(
                company.kh_label_max_item_lines, width_mm=items_width, lines=items_lines)

        # Several parcels: the amount is the total of the shipment, to collect once
        if self.kh_parcel_count and self.kh_parcel_count > 1 and money['state'] == 'cod':
            money['amount_size'] = min(money['amount_size'], AMOUNT_MAX_SIZE_MULTI_PARCEL)

        values = {
            'picking': self,
            'company': company,
            # header
            'logo': company_values['logo'],
            'company_name': kh_truncate(company.name, MAX_COMPANY),
            'tagline': kh_truncate(company.kh_label_tagline, MAX_TAGLINE),
            'company_phone': company_values['company_phone'],
            'reference': self.name,
            'order_ref': order.name or self.origin or '',
            'date': self._kh_label_date(),
            'courier': kh_truncate(self._kh_label_courier(), MAX_COURIER),
            'weight': self._kh_label_weight(),
            # payment
            'money': money,
            'qr': qr,
            'qr_caption': qr_caption,
            # items & instructions
            'show_items': show_items,
            'items': items,
            'items_more': items_more,
            'total_qty': total_qty,
            'allow_check': self.kh_allow_check,
            'fragile': self.kh_fragile,
            'note': note,
            'note_lines': note_lines,
            'items_lines': items_lines,
            # footer
            'barcode': self._kh_barcode_data_uri('Code128', self.name, width=1400, height=160, quiet=0),
            'footer': kh_truncate(company.kh_label_footer, MAX_FOOTER),
        }
        values.update(self._kh_receiver_values(partner, company))
        return values

    def _kh_get_label_values(self):
        """One dict per label to print: ``kh_parcel_count`` labels per transfer, numbered 1/N ... N/N."""
        labels = []
        cache = {}
        for picking in self:
            values = picking._kh_prepare_label_values(cache)
            count = min(max(picking.kh_parcel_count or 1, 1), MAX_PARCELS)
            for index in range(1, count + 1):
                labels.append(dict(
                    values, parcel_index=index, parcel_count=count, parcel_label='%s/%s' % (index, count)))
        return labels

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_print_cod_label(self):
        """Print the 100 x 80 mm COD label(s) of the selected transfers."""
        self._kh_check_cod_ambiguity()
        return self.env.ref('delivery_label_cod_kh.action_report_cod_label').report_action(self)
