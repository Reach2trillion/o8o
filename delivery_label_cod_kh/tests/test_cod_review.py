# -*- coding: utf-8 -*-
"""Regression tests of the first review: money edge cases, phones, handling flags and layout."""
import base64
import io
import re
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.delivery_label_cod_kh.models import stock_picking as picking_module
from odoo.addons.delivery_label_cod_kh.models.kh_label_text import (
    count_units, fit_width, text_width_mm, truncate, wrap_lines,
)
from .common import CodLabelCommon

REPORT = 'delivery_label_cod_kh.action_report_cod_label'


def _query(link):
    return {key: values[0] for key, values in parse_qs(urlsplit(link).query).items()}


def _image(data_uri):
    return Image.open(io.BytesIO(base64.b64decode(data_uri.split(',', 1)[1])))


def _px(style, prop='height'):
    match = re.search(r'(?:^|; )%s: (\d+)px' % prop, style)
    return int(match.group(1)) if match else 0


class ReviewCommon(CodLabelCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        payment_method = cls.env.ref('payment.payment_method_unknown')
        cls.provider = cls.env['payment.provider'].create({
            'name': 'Dummy (test)', 'code': 'none', 'state': 'test', 'company_id': cls.company.id,
            'payment_method_ids': [Command.set(payment_method.ids)],
        })
        payment_method.active = True

    def _done_transaction(self, order, amount, payment=None):
        """Online payment of ``order`` (e.g. made with the "scan to pay" QR code of the label)."""
        transaction = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.provider.payment_method_ids[:1].id,
            'amount': amount,
            'currency_id': order.currency_id.id,
            'partner_id': order.partner_invoice_id.id,
            'reference': '%s-tx-%s' % (order.name, len(order.transaction_ids)),
            'sale_order_ids': [Command.set(order.ids)],
        })
        transaction.write({'state': 'done', 'payment_id': payment and payment.id})
        return transaction

    def _reverse(self, invoice, price_unit=None):
        """Credit note of ``invoice`` (one line at ``price_unit`` when given), posted."""
        refund = invoice._reverse_moves([{'ref': 'credit note'}])
        if price_unit is not None:
            refund.invoice_line_ids[1:].unlink()
            refund.invoice_line_ids.price_unit = price_unit
        refund.action_post()
        return refund


@tagged('post_install', '-at_install')
class TestReviewMoney(ReviewCommon):

    def test_partial_invoice_payment_then_online_payment(self):
        """$5 paid on the invoice, then the $12 left paid with the label's payment link: paid in full."""
        order = self._create_order()
        invoice = self._create_invoice(order)
        self._register_payment(invoice, amount=5.0)
        picking = self._deliveries(order)
        self.assertEqual((picking.kh_cod_state, picking.kh_cod_amount), ('cod', 12.0))
        self._done_transaction(order, 12.0)
        picking.invalidate_recordset()
        self.assertEqual((picking.kh_cod_state, picking.kh_cod_amount), ('paid', 0.0))

    def test_online_payment_reconciled_with_invoice_is_counted_once(self):
        order = self._create_order()
        invoice = self._create_invoice(order)
        payment = self._register_payment(invoice)  # reconciled with the invoice
        transaction = self._done_transaction(order, 17.0, payment=payment)
        self.assertEqual(self.env['stock.picking']._kh_order_transactions_paid(order, invoice), 0.0)
        self.assertEqual(self._deliveries(order).kh_cod_state, 'paid')
        # only the part reconciled with the invoice is already counted
        transaction.amount = 20.0
        self.assertAlmostEqual(self.env['stock.picking']._kh_order_transactions_paid(order, invoice), 3.0)
        # an online payment not reconciled yet with the open invoice
        order2 = self._create_order()
        invoice2 = self._create_invoice(order2)
        self._done_transaction(order2, 17.0)
        self.assertEqual(self._deliveries(order2).kh_cod_state, 'paid')
        self.assertEqual(invoice2.amount_residual, 17.0)

    def test_breakdown_only_when_it_adds_up(self):
        order = self._create_order()
        self._register_payment(self._create_invoice(order), amount=5.0)
        money = self._deliveries(order)._kh_get_money_values()
        self.assertEqual(money['amount_str'], '$12.00')
        self.assertFalse(money['goods_str'] or money['fee_str'], "15 + 2 is not the 12 to collect")
        self.assertNotIn('fee', money['lines'])

    def test_no_backorder_partial_delivery_is_refused(self):
        """Validated with 'No backorder': the label must not ask for the goods left behind."""
        order = self._create_order()
        picking = self._deliveries(order)
        for move in picking.move_ids:
            move.quantity = 1.0 if move.product_id == self.serum else 0.0
            move.picked = True
        picking.with_context(cancel_backorder=True)._action_done()
        self.assertEqual(picking.state, 'done')
        self.assertFalse(picking.backorder_ids)
        self.assertEqual(picking.kh_cod_amount, 17.0)
        self.assertIn('missing: 1 × Night Cream', picking.kh_cod_warning)
        with self.assertRaises(UserError):
            picking.action_print_cod_label()
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 9.5})
        picking.action_print_cod_label()

    def test_short_picked_delivery_is_refused(self):
        order = self._create_order()
        picking = self._deliveries(order)
        self.assertFalse(picking.kh_cod_warning, "nothing picked yet: the demand is the whole order")
        cream_move = picking.move_ids.filtered(lambda move: move.product_id == self.cream)
        cream_move.write({'quantity': 0.0, 'picked': True})
        self.assertIn('Night Cream', picking.kh_cod_warning)

    def test_invoice_shared_with_another_order(self):
        """One invoice for two orders, partly paid: neither label may trust its own share."""
        order1 = self._create_order()
        order2 = self._create_order(lines=[(self.serum, 4, 7.5)])  # 30 + 2
        invoice = (order1 | order2)._create_invoices(grouped=False)
        self.assertEqual(len(invoice), 1)
        invoice.action_post()
        self._register_payment(invoice, amount=20.0)
        for order in (order1, order2):
            picking = self._deliveries(order)
            self.assertIn(invoice.name, picking.kh_cod_warning)
            with self.assertRaises(UserError):
                picking.action_print_cod_label()
        self.assertEqual(self._deliveries(order1).kh_cod_state, 'paid', "the false PAID is refused too")
        # fully paid: each order is paid
        self._register_payment(invoice)
        for order in (order1, order2):
            picking = self._deliveries(order)
            self.assertEqual(picking.kh_cod_state, 'paid')
            self.assertFalse(picking.kh_cod_warning)

    def test_credit_note_on_open_invoice(self):
        """A $5 credit note reconciled with the open invoice: auto must not print $17."""
        order = self._create_order()
        invoice = self._create_invoice(order)
        refund = self._reverse(invoice, price_unit=5.0)
        (invoice | refund).line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable' and not line.reconciled).reconcile()
        self.assertEqual(invoice.amount_residual, 12.0)
        picking = self._deliveries(order)
        self.assertIn(refund.name, picking.kh_cod_warning)
        with self.assertRaises(UserError):
            picking.action_print_cod_label()

    def test_cancelled_delivery(self):
        order = self._create_order()
        picking = self._deliveries(order)
        picking.action_cancel()
        self.assertEqual((picking.kh_cod_state, picking.kh_cod_amount), ('none', 0.0))
        self.assertFalse(picking.kh_payment_link)
        with self.assertRaises(UserError):
            picking.action_print_cod_label()
        with self.assertRaises(UserError):
            self.env['ir.actions.report']._render_qweb_html(REPORT, picking.ids)

    def test_redelivery_after_return(self):
        """A failed delivery comes back (return) and is sent again (return of the return)."""
        order = self._create_order()
        delivery = self._deliveries(order)
        for move in delivery.move_ids:
            move.write({'quantity': move.product_uom_qty, 'picked': True})
        delivery.button_validate()
        wizard = self.env['stock.return.picking'].with_context(
            active_id=delivery.id, active_ids=delivery.ids, active_model='stock.picking').create({})
        for line in wizard.product_return_moves:
            line.quantity = line.move_id.quantity
        back = wizard._create_return()
        self.assertEqual((back.picking_type_code, back.kh_cod_state), ('incoming', 'none'))
        for move in back.move_ids:
            move.write({'quantity': move.product_uom_qty, 'picked': True})
        back.button_validate()
        wizard = self.env['stock.return.picking'].with_context(
            active_id=back.id, active_ids=back.ids, active_model='stock.picking').create({})
        for line in wizard.product_return_moves:
            line.quantity = line.move_id.quantity
        again = wizard._create_return()
        self.assertEqual((again.picking_type_code, again.sale_id), ('outgoing', order))
        self.assertEqual((again.kh_cod_state, again.kh_cod_amount), ('cod', 17.0))
        # two deliveries of the same order now: the staff decides what each label collects
        for picking in (delivery, again):
            self.assertIn(again.name, picking.kh_cod_warning)
            with self.assertRaises(UserError):
                picking.action_print_cod_label()

    def test_return_to_vendor_is_not_a_delivery(self):
        receipt = self._create_picking(self.picking_type_in)
        for move in receipt.move_ids:
            move.write({'quantity': move.product_uom_qty, 'picked': True})
        receipt.button_validate()
        wizard = self.env['stock.return.picking'].with_context(
            active_id=receipt.id, active_ids=receipt.ids, active_model='stock.picking').create({})
        for line in wizard.product_return_moves:
            line.quantity = line.move_id.quantity
        vendor_return = wizard._create_return()
        self.assertEqual(vendor_return.picking_type_code, 'outgoing')
        vendor_return.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 5.0})
        self.assertEqual(vendor_return.kh_cod_state, 'none')

    def test_riel_amount_rounded_to_whole_riel(self):
        """KHR has 0.01 rounding in Odoo: 21,579.50៛ + 0.50៛ fee is collected as whole riel."""
        pricelist = self.env['product.pricelist'].create({'name': 'Riel (test)', 'currency_id': self.khr.id})
        order = self._create_order(lines=[(self.serum, 1, 13579.5)], carrier=8000.5, pricelist=pricelist)
        self.assertAlmostEqual(order.amount_total, 21580.0)
        order.order_line.filtered(lambda line: not line.is_delivery).price_unit = 13579.0
        self.assertAlmostEqual(order.amount_total, 21579.5)
        picking = self._deliveries(order)
        self.assertEqual(picking.kh_cod_amount, 21580.0, "half-up to whole riel")
        money = picking._kh_get_money_values()
        # the three numbers of the label agree: 13,579 + 8,001 = 21,580
        self.assertEqual((money['amount_str'], money['goods_str'], money['fee_str']),
                         ('21,580៛', '13,579៛', '8,001៛'))
        self.assertEqual(float(_query(picking.kh_payment_link)['amount']), 21580.0, "link = printed amount")
        self.assertEqual(self.env['stock.picking']._kh_format_amount(13590.5, self.khr), '13,591៛')
        self.assertEqual(self.env['stock.picking']._kh_format_amount(13579.4, self.khr), '13,579៛')

    def test_manual_amount_checks(self):
        order = self._create_order()
        self._register_payment(self._create_invoice(order), amount=5.0)
        picking = self._deliveries(order)
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 50.0})
        self.assertIn('$50.00', picking.kh_cod_warning)
        self.assertIn('$12.00', picking.kh_cod_warning)
        picking.action_print_cod_label()  # a warning only: the staff may add other costs
        # 0 prints PAID: refused, the clerk who followed the "set Manual" advice must type the amount
        picking.kh_cod_amount_manual = 0.0
        self.assertEqual(picking.kh_cod_state, 'paid')
        self.assertIn("'Paid - do not collect'", picking.kh_cod_warning)
        with self.assertRaises(UserError):
            picking.action_print_cod_label()
        picking.kh_payment_mode = 'paid'
        self.assertFalse(picking.kh_cod_warning)
        picking.action_print_cod_label()

    def test_ambiguity_message_has_the_amount_due(self):
        order = self._create_order()
        first = self._deliveries(order)
        first.copy()
        self.assertIn('$17.00 due over 2 deliveries', first.kh_cod_warning)

    def test_pos_order_paid_with_customer_account(self):
        """POS "ship later" order settled with the Customer Account (pay later): to collect."""
        def payment(amount, method_type):
            return SimpleNamespace(amount=amount, payment_method_id=SimpleNamespace(type=method_type))

        pos_order = SimpleNamespace(currency_id=self.usd, amount_total=15.0, payment_ids=[payment(15.0, 'pay_later')])
        pos_order.sudo = lambda: pos_order
        Picking = self.env['stock.picking']
        self.assertEqual(Picking._kh_pos_order_due(pos_order), (15.0, 'cod'))
        pos_order.payment_ids = [payment(20.0, 'cash'), payment(-5.0, 'cash')]  # change given back
        self.assertEqual(Picking._kh_pos_order_due(pos_order), (0.0, 'paid'))
        pos_order.payment_ids = [payment(5.0, 'bank'), payment(10.0, 'pay_later')]
        self.assertEqual(Picking._kh_pos_order_due(pos_order), (10.0, 'cod'))

    def test_no_fee_note(self):
        picking = self._deliveries(self._create_order(carrier=False))
        self.assertNotIn('nofee', picking._kh_get_money_values()['lines'], "default: print nothing")
        self.company.kh_label_no_fee_note = 'free'
        money = picking._kh_get_money_values()
        self.assertIn('nofee', money['lines'])
        self.assertIn('Free delivery', money['nofee_str'])
        self.company.kh_label_no_fee_note = 'receiver'
        self.assertIn('not included', picking._kh_get_money_values()['nofee_str'])
        # orders with a delivery fee keep the breakdown
        self.assertIn('fee', self._deliveries(self._create_order())._kh_get_money_values()['lines'])


@tagged('post_install', '-at_install')
class TestReviewPaymentQr(ReviewCommon):

    def test_custom_url_partner_without_phone(self):
        picking = self._deliveries(self._create_order(partner=self.messy_customer))
        url = picking._kh_custom_payment_url('https://x.test/?who={partner}', 17.0, self.usd)
        self.assertEqual(url, 'https://x.test/?who=Andyyvathhh')

    def test_custom_url_too_long(self):
        picking = self._deliveries(self._create_order())
        with mute_logger('odoo.addons.delivery_label_cod_kh.models.stock_picking'):
            url = picking._kh_custom_payment_url('https://x.test/?a={amount}&' + 'x' * 1100, 17.0, self.usd)
        self.assertFalse(url)

    def test_pay_qr_on_first_parcel_only(self):
        picking = self._deliveries(self._create_order())
        picking.kh_parcel_count = 3
        labels = self._labels(picking)
        self.assertEqual([label['pay_qr_here'] for label in labels], [True, False, False])
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, picking.ids)[0].decode()
        self.assertEqual(html.count('SCAN TO PAY'), 1)
        self.assertEqual(html.count('class="o_kh_payqr"'), 1)
        self.assertEqual(html.count('Pay QR on'), 2)

    def test_khqr_image_trimmed(self):
        """The uploaded KHQR image is cropped to its ink and printed in black and white."""
        image = Image.new('RGB', (200, 200), (255, 255, 255))
        image.paste((200, 0, 0), (60, 60, 140, 140))  # the "code", red, in a wide white margin
        output = io.BytesIO()
        image.save(output, format='PNG')
        self.company.write({'kh_label_pay_qr': 'khqr_image', 'kh_label_khqr_image': base64.b64encode(output.getvalue())})
        pay_qr = self._labels(self._deliveries(self._create_order()))[0]['pay_qr']
        printed = _image(pay_qr['image']).convert('L')
        self.assertEqual({color for _count, color in printed.getcolors()}, {0}, "only the ink is left, pure black")
        self.assertEqual(pay_qr['image_style'], 'width: 68px; height: 68px;')

    def test_barcode_fallback_when_reportlab_fails(self):
        def broken(*args, **kwargs):
            raise RuntimeError("This installation of reportLab has neither PYCAIRO or RENDERPM extras installed")
        with patch.object(type(self.env['ir.actions.report']), 'barcode', broken), \
                mute_logger('odoo.addons.delivery_label_cod_kh.models.stock_picking'):
            uri = self.env['stock.picking']._kh_barcode_data_uri('QR', 'https://example.com', quiet=1)
        self.assertTrue(uri.startswith('data:image/png;base64,'))


@tagged('post_install', '-at_install')
class TestReviewPaymentLinkHttp(ReviewCommon, HttpCase):
    """An old label's payment link never charges more than what is still due."""

    def setUp(self):
        super().setUp()
        self.provider.is_published = True

    def test_link_capped_at_amount_due(self):
        order = self._create_order()
        link = self._deliveries(order).kh_payment_link
        invoice = self._create_invoice(order)
        self._register_payment(invoice, amount=5.0)
        response = self.url_open(link)
        self.assertEqual(response.status_code, 200)
        self.assertIn('12.00', response.text)
        self.assertNotIn('17.00', response.text)
        self._register_payment(invoice)
        response = self.url_open(link)
        self.assertIn('There is nothing to pay', response.text)


@tagged('post_install', '-at_install')
class TestReviewPartner(ReviewCommon):

    def test_khmer_digits(self):
        Picking = self.env['stock.picking']
        partner = self.env['res.partner'].create({'name': 'ចាន់ សុខា ០៩៥៦៣៤៧០៦'})
        self.assertEqual(Picking._kh_extract_phones(partner), '095 634 706')
        self.assertEqual(Picking._kh_clean_receiver_name(partner.name), 'ចាន់ សុខា')
        self.assertEqual(Picking._kh_clean_receiver_name('ផ្ទះលេខ ១២ Dara'), 'ផ្ទះលេខ ១២ Dara', "not a phone")
        partner = self.env['res.partner'].create({'name': 'Dara', 'phone': '០១២ ៣៤៥ ៦៧៨', 'mobile': '012345678'})
        self.assertEqual(Picking._kh_extract_phones(partner), '012 345 678', "formatted and de-duplicated")

    def test_handling_defaults_of_the_transfer_company(self):
        """A transfer created for company B while company A is active gets B's defaults."""
        company_b = self.env['res.company'].sudo().create({
            'name': 'Company B (test)', 'kh_label_fragile_default': True, 'kh_label_allow_check_default': False})
        self.env.user.sudo().company_ids |= company_b
        out_type_b = self.env['stock.warehouse'].search([('company_id', '=', company_b.id)], limit=1).out_type_id
        Picking = self.env['stock.picking'].with_context(allowed_company_ids=[self.company.id, company_b.id])
        self.assertEqual(Picking.env.company, self.company)
        picking = Picking.create({
            'picking_type_id': out_type_b.id,
            'location_id': out_type_b.default_location_src_id.id,
            'location_dest_id': self.customer_location.id,
        })
        self.assertEqual(picking.company_id, company_b)
        self.assertEqual((picking.kh_fragile, picking.kh_allow_check), (True, False))
        # the flags of an existing transfer are kept
        picking.kh_fragile = False
        company_b.kh_label_fragile_default = True
        picking.invalidate_recordset()
        self.assertFalse(picking.kh_fragile)
        # an explicit value wins
        picking = Picking.create({
            'picking_type_id': out_type_b.id, 'kh_allow_check': True,
            'location_id': out_type_b.default_location_src_id.id, 'location_dest_id': self.customer_location.id,
        })
        self.assertTrue(picking.kh_allow_check)


@tagged('post_install', '-at_install')
class TestReviewLayout(ReviewCommon):
    """Texts and boxes of the label: nothing important cut, Khmer stacks never clipped."""

    def _khmer_partner(self, **values):
        return self.env['res.partner'].create(dict({
            'name': 'សុខ បញ្ញា',
            'phone': '012 345 678',
            'street': 'ផ្ទះលេខ ១២៣ ភូមិស្ពានថ្មី ទល់មុខវត្តព្រែកហូរ ក្បែរស្ពានថ្មី',
            'street2': 'ក្បែរផ្សារត្បូងឃ្មុំ',
            'city': 'ស្រុកពញាក្រែក',
            'state_id': self.kandal.id,
            'country_id': self.cambodia.id,
        }, **values))

    def test_vertical_budget(self):
        """The boxes of the receiver and items sections always fit their section."""
        cases = [
            self._khmer_partner(),
            self._khmer_partner(mobile='097 555 4444', partner_latitude=11.5, partner_longitude=104.9),
            self.customer,
            self.messy_customer,
        ]
        for partner in cases:
            picking = self._deliveries(self._create_order(partner=partner))
            for note in ('', 'Call 30 min before arrival, deliver after 5pm only, leave at the pharmacy',
                         'សូមតេមុនពេលដឹក១៥នាទី ហើយកុំទុកឥវ៉ាន់នៅមុខផ្ទះ ព្រោះគ្មាននរណានៅផ្ទះពេលថ្ងៃ'):
                picking.kh_label_note = note
                label = self._labels(picking)[0]
                receiver = (picking_module.RECV_TAG_H + _px(label['name_style']) + _px(label['phone_style'])
                            + (_px(label['phone2_style']) if label['phone2_line'] else 0)
                            + (_px(label['address_style'], 'max-height') if label['address_rows']
                               else _px(label['address_blank_style']) if label['address_blank_lines'] else 0))
                self.assertLessEqual(receiver, picking_module.RECV_H, (partner.name, note))
                items = ((_px(label['note_style'], 'max-height') if label['note_rows'] else 0)
                         + (_px(label['items_style'], 'max-height') if label['items_lines'] else 0))
                self.assertLessEqual(items, picking_module.ITEMS_H, (partner.name, note))

    def test_khmer_boxes_are_taller(self):
        label = self._labels(self._deliveries(self._create_order(partner=self._khmer_partner())))[0]
        self.assertLessEqual(label['name_size'], 11, "Khmer names: at most 11 pt")
        self.assertGreaterEqual(_px(label['name_style']), 27, "room for the stack of ញ្ញ")
        self.assertEqual(len(label['address_rows']), 2)
        self.assertEqual(label['province_city'], 'ស្រុកពញាក្រែក')
        self.assertIn('ក្បែរផ្សារត្បូងឃ្មុំ', label['address'], "street 2 (landmark) is printed")

    def test_wrap_lines(self):
        rows, truncated = wrap_lines('ផ្ទះលេខ ១២៣ ភូមិស្ពានថ្មី ទល់មុខវត្តព្រែកហូរ', 30, 8, max_lines=2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(truncated and rows[-1].endswith('…'))
        for row in rows:
            self.assertLessEqual(text_width_mm(row, 8), 30)
        self.assertEqual(wrap_lines('Short text', 50, 8, max_lines=2), (['Short text'], False))
        rows, truncated = wrap_lines('បន្ទាយមានជ័យ', 12, 13, True, max_lines=2)  # a Khmer word without spaces
        self.assertEqual(''.join(rows).replace('…', '')[:4], 'បន្ទ')
        self.assertNotIn(rows[0][-1:], ('្',))

    def test_word_boundary_keeps_most_of_the_room(self):
        name = 'Sophea Chanthavy Rattanakmonyroth Kim'
        fitted = fit_width(name, 50, 9, bold=True)
        self.assertTrue(fitted.startswith('Sophea Chanthavy Rattana'), fitted)
        self.assertLessEqual(text_width_mm(fitted, 9, bold=True), 50)
        width = text_width_mm('Sok Dara Chan…', 9, bold=True) + 0.3
        self.assertEqual(fit_width('Sok Dara Chan Thy', width, 9, bold=True), 'Sok Dara Chan…')

    def test_khmer_note_cap_counts_letters(self):
        note = 'សូមតេមុនពេលដឹក១៥នាទី ហើយកុំទុកឥវ៉ាន់នៅមុខផ្ទះ ព្រោះគ្មាននរណានៅផ្ទះពេលថ្ងៃ'
        self.assertGreater(len(note), picking_module.MAX_NOTE)
        self.assertLess(count_units(note), picking_module.MAX_NOTE)
        self.assertEqual(truncate(note, picking_module.MAX_NOTE), note)

    def test_header_fitting(self):
        picking = self._deliveries(self._create_order())
        self.company.name = 'Very Long Company Name Cosmetics Wholesale (San Francisco)'
        picking.name = 'ABJSK/OUT/00620'
        self.carrier.name = 'L192 delivery express'
        label = self._labels(picking)[0]
        self.assertTrue(label['company_name'].endswith('…'))
        self.assertEqual(label['reference_header'], 'ABJSK/OUT/00620', "the reference is never cut")
        self.assertNotIn('8.5pt', label['reference_style'])
        self.assertTrue(label['courier'].startswith('L192 delivery'))
        self.assertTrue(label['courier'].endswith('…'))
        picking.name = 'WAREHOUSE-LONG-CODE/OUT/2026/000620'
        self.assertTrue(self._labels(picking)[0]['reference_header'].endswith('/000620'))

    def test_barcode_bars(self):
        picking = self._deliveries(self._create_order())
        picking.name = 'SO/OUT/00620'  # the reference format of the shop: 156 modules
        label = self._labels(picking)[0]
        modules = self.env['stock.picking']._kh_code128_modules(picking.name)
        self.assertEqual(label['barcode_width'], sum(width for _bar, width in modules))
        self.assertEqual(sum(space + bar for space, bar in label['barcode_bars']), label['barcode_width'])
        self.assertFalse(label['barcode'], "no resampled PNG when the bars are drawn")
        self.assertEqual(label['barcode_width'], 156)
        self.assertIn('scale(1.2, 1)', label['barcode_style'], "0.32 mm per module")
        self.assertEqual(label['barcode_cell_style'], 'width: 212px; padding-left: 12px;', "quiet zones of 10 modules")
        picking.name = 'ABJSK/OUT/00620'  # 189 modules: 0.29 mm per module
        self.assertIn('scale(1.107, 1)', self._labels(picking)[0]['barcode_style'])

    def test_logo_black_and_white(self):
        image = Image.new('RGBA', (120, 120), (0, 0, 0, 0))
        image.paste((186, 85, 211, 255), (10, 10, 110, 110))  # purple square, transparent margin
        output = io.BytesIO()
        image.save(output, format='PNG')
        self.company.logo = base64.b64encode(output.getvalue())
        logo = self._labels(self._deliveries(self._create_order()))[0]['logo']
        printed = _image(logo)
        self.assertEqual(printed.mode, 'LA')
        self.assertEqual({color for _count, color in printed.getcolors()}, {(0, 255), (255, 0)},
                         "black ink or nothing: no grey, thermal printers drop or dither it")
        self.assertEqual(printed.getpixel((printed.width // 2, printed.height // 2)), (0, 255), "the purple ink prints")
        self.assertEqual(printed.getpixel((0, 0)), (255, 0), "the background stays transparent")

    def test_handling_chip_when_check_not_allowed(self):
        picking = self._deliveries(self._create_order())
        picking.kh_allow_check = False
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, picking.ids)[0].decode()
        self.assertIn('No check', html)
        self.assertNotIn('Allow check', html)
