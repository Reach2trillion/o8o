# -*- coding: utf-8 -*-
import io

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import HttpCase, tagged
from odoo.tools.pdf import PdfFileReader

from odoo.addons.delivery_label_cod_kh.models.kh_label_text import count_units, fit_width, text_width_mm, truncate
from .common import CodLabelCommon, FIXED_KHR_RATE

REPORT = 'delivery_label_cod_kh.action_report_cod_label'
MM = 72.0 / 25.4  # PDF points per millimetre


@tagged('post_install', '-at_install')
class TestCodAmount(CodLabelCommon):
    """COD rules of section 4 of the specification: never guess the money."""

    def test_auto_unpaid_without_invoice(self):
        order = self._create_order()
        picking = self._deliveries(order)
        self.assertEqual(order.amount_total, 17.0)
        self.assertEqual(picking.kh_payment_mode, 'auto')
        self.assertEqual(picking.kh_cod_currency_id, self.usd)
        self.assertEqual(picking.kh_cod_state, 'cod')
        self.assertAlmostEqual(picking.kh_cod_amount_auto, 17.0)
        self.assertAlmostEqual(picking.kh_cod_amount, 17.0)

    def test_auto_unpaid_draft_invoice(self):
        order = self._create_order()
        self._create_invoice(order, post=False)
        picking = self._deliveries(order)
        self.assertEqual(picking.kh_cod_state, 'cod')
        self.assertAlmostEqual(picking.kh_cod_amount, 17.0)

    def test_auto_posted_unpaid_invoice(self):
        order = self._create_order()
        self._create_invoice(order)
        self.assertAlmostEqual(self._deliveries(order).kh_cod_amount, 17.0)

    def test_auto_paid_invoice(self):
        order = self._create_order()
        invoice = self._create_invoice(order)
        self._register_payment(invoice)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        picking = self._deliveries(order)
        self.assertEqual(picking.kh_cod_state, 'paid')
        self.assertEqual(picking.kh_cod_amount, 0.0)
        self.assertEqual(picking.kh_cod_amount_auto, 0.0)

    def test_auto_partial_payment(self):
        order = self._create_order()
        invoice = self._create_invoice(order)
        self._register_payment(invoice, amount=10.0)
        picking = self._deliveries(order)
        self.assertEqual(picking.kh_cod_state, 'cod')
        self.assertAlmostEqual(picking.kh_cod_amount, 7.0)

    def test_auto_credit_note_deducted(self):
        """A paid invoice and a refunded part: the net paid amount is computed (spec formula), but a
        credit note can be a discount or an invoice to redo, so the automatic amount is not printed."""
        order = self._create_order()
        invoice = self._create_invoice(order)
        self._register_payment(invoice)
        refund = invoice._reverse_moves([{'ref': 'partial refund'}])
        refund.invoice_line_ids.filtered(lambda line: line.price_unit != 2.0).unlink()  # refund the fee only
        refund.action_post()
        self._register_payment(refund)
        picking = self._deliveries(order)
        self.assertAlmostEqual(picking.kh_cod_amount_auto, 2.0)
        self.assertIn(refund.name, picking.kh_cod_warning)
        with self.assertRaises(UserError):
            picking.action_print_cod_label()

    def test_auto_down_payment(self):
        order = self._create_order()
        wizard = self.env['sale.advance.payment.inv'].with_context(
            active_model='sale.order', active_ids=order.ids,
        ).create({'advance_payment_method': 'fixed', 'fixed_amount': 5.0})
        wizard.create_invoices()
        down_payment = order.invoice_ids
        self.assertEqual(len(down_payment), 1)
        down_payment.action_post()
        self._register_payment(down_payment)
        picking = self._deliveries(order)
        self.assertEqual(picking.kh_cod_state, 'cod')
        self.assertAlmostEqual(picking.kh_cod_amount, 12.0)

    def test_manual_mode(self):
        picking = self._deliveries(self._create_order())
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 12.5})
        self.assertEqual(picking.kh_cod_state, 'cod')
        self.assertAlmostEqual(picking.kh_cod_amount, 12.5)
        self.assertAlmostEqual(picking.kh_cod_amount_auto, 17.0, msg="the form still shows what Auto would print")
        picking.kh_cod_amount_manual = 0.0
        self.assertEqual(picking.kh_cod_state, 'paid')
        self.assertEqual(picking.kh_cod_amount, 0.0)

    def test_manual_amount_cannot_be_negative(self):
        picking = self._deliveries(self._create_order())
        with self.assertRaises(ValidationError):
            picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': -1.0})

    def test_paid_mode(self):
        picking = self._deliveries(self._create_order())
        picking.kh_payment_mode = 'paid'
        self.assertEqual(picking.kh_cod_state, 'paid')
        self.assertEqual(picking.kh_cod_amount, 0.0)

    def test_no_sale_order(self):
        picking = self._create_picking(self.picking_type_out)
        self.assertFalse(picking.sale_id)
        self.assertEqual(picking.kh_cod_state, 'none')
        self.assertEqual(picking.kh_cod_amount, 0.0)
        # an explicit manual amount still prints (the courier collects what the shop typed)
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 5.0})
        self.assertEqual(picking.kh_cod_state, 'cod')
        self.assertAlmostEqual(picking.kh_cod_amount, 5.0)

    def test_not_outgoing(self):
        receipt = self._create_picking(self.picking_type_in)
        self.assertEqual(receipt.kh_cod_state, 'none')
        receipt.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 5.0})
        self.assertEqual(receipt.kh_cod_state, 'none', "a receipt never collects money")
        self.assertEqual(receipt.kh_cod_amount, 0.0)

    def test_paid_mode_and_copy(self):
        picking = self._deliveries(self._create_order())
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 3.0, 'kh_parcel_count': 2})
        copy = picking.copy()
        self.assertEqual(copy.kh_payment_mode, 'auto')
        self.assertFalse(copy.kh_cod_amount_manual)
        self.assertEqual(copy.kh_parcel_count, 1)

    def test_parcel_count_constraint(self):
        picking = self._deliveries(self._create_order())
        for wrong in (0, -1, 51):
            with self.assertRaises(ValidationError):
                picking.kh_parcel_count = wrong
        picking.kh_parcel_count = 50
        self.assertEqual(picking.kh_parcel_count, 50)


@tagged('post_install', '-at_install')
class TestCodRiel(CodLabelCommon):
    """Riel conversion, rounding and formatting."""

    def test_format_amount(self):
        Picking = self.env['stock.picking']
        self.assertEqual(Picking._kh_format_amount(17.0, self.usd), '$17.00')
        self.assertEqual(Picking._kh_format_amount(1234567.5, self.usd), '$1,234,567.50')
        self.assertEqual(Picking._kh_format_amount(69100.0, self.khr), '69,100៛')
        self.assertEqual(Picking._kh_format_amount(5014100.0, self.khr), '5,014,100៛')

    def test_fixed_rate(self):
        self.company.kh_label_khr_rate_source = 'fixed'
        self.assertAlmostEqual(self.company._kh_to_khr(17.0, self.usd), 17.0 * FIXED_KHR_RATE)
        self.assertEqual(self.company._kh_round_khr(self.company._kh_to_khr(17.0, self.usd)), 69700.0)
        money = self._deliveries(self._create_order())._kh_get_money_values()
        self.assertEqual(money['amount_str'], '$17.00')
        self.assertEqual(money['secondary_str'], '≈ 69,700៛')

    def test_odoo_rate_and_rounding(self):
        company = self.company
        self.assertAlmostEqual(company._kh_to_khr(17.0, self.usd), 69042.44, places=2)
        self.assertEqual(company._kh_round_khr(69042.44), 69000.0)
        self.assertEqual(company._kh_round_khr(69050.0), 69100.0, "half-up")
        company.kh_label_khr_rounding = 1
        self.assertEqual(company._kh_round_khr(69042.44), 69042.0)
        company.kh_label_khr_rounding = 0
        self.assertEqual(company._kh_round_khr(69042.6), 69043.0, "0 is treated as 1")
        company.kh_label_khr_rounding = -5
        self.assertEqual(company._kh_round_khr(69042.6), 69043.0, "negative is treated as 1")
        company.kh_label_khr_rounding = 100
        money = self._deliveries(self._create_order())._kh_get_money_values()
        self.assertEqual(money['secondary_str'], '≈ 69,000៛')

    def test_odoo_rate_falls_back_to_fixed_rate(self):
        self.khr.rate_ids.unlink()
        self.assertAlmostEqual(self.company._kh_to_khr(17.0, self.usd), 17.0 * FIXED_KHR_RATE, msg="no rate")
        self.setup_other_currency('KHR', rates=[('2000-01-01', 4061.32)])
        self.khr.active = False
        self.assertAlmostEqual(self.company._kh_to_khr(17.0, self.usd), 17.0 * FIXED_KHR_RATE, msg="inactive")

    def test_order_in_riel(self):
        pricelist = self.env['product.pricelist'].create({'name': 'Riel (test)', 'currency_id': self.khr.id})
        order = self._create_order(
            lines=[(self.serum, 1, 40000.0), (self.lip, 2, 10000.0)], carrier=8000.0, pricelist=pricelist)
        self.assertEqual(order.currency_id, self.khr)
        self.assertAlmostEqual(order.amount_total, 68000.0)
        picking = self._deliveries(order)
        self.assertEqual(picking.kh_cod_currency_id, self.khr)
        money = picking._kh_get_money_values()
        self.assertEqual(money['amount_str'], '68,000៛')
        self.assertEqual(money['secondary_str'], '≈ $16.74')  # 68000 / 4061.32
        self.assertEqual(money['goods_str'], '60,000៛')
        self.assertEqual(money['fee_str'], '8,000៛')

    def test_hide_riel(self):
        self.company.kh_label_show_khr = False
        money = self._deliveries(self._create_order())._kh_get_money_values()
        self.assertEqual(money['amount_str'], '$17.00')
        self.assertFalse(money['secondary_str'])

    def test_no_riel_when_paid(self):
        picking = self._deliveries(self._create_order())
        picking.kh_payment_mode = 'paid'
        money = picking._kh_get_money_values()
        self.assertEqual(money['state'], 'paid')
        self.assertFalse(money['amount_str'])
        self.assertFalse(money['secondary_str'])

    def test_delivery_fee_breakdown(self):
        picking = self._deliveries(self._create_order())
        money = picking._kh_get_money_values()
        self.assertEqual((money['goods_str'], money['fee_str']), ('$15.00', '$2.00'))
        # manual amount: no breakdown (the amount does not come from the order)
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 10.0})
        money = picking._kh_get_money_values()
        self.assertFalse(money['goods_str'])
        # no delivery line: no breakdown
        money = self._deliveries(self._create_order(carrier=False))._kh_get_money_values()
        self.assertEqual(money['amount_str'], '$15.00')
        self.assertFalse(money['fee_str'])


@tagged('post_install', '-at_install')
class TestCodAmbiguity(CodLabelCommon):
    """One order, several deliveries: the automatic amount must not be printed twice."""

    def _order_with_backorder(self):
        order = self._create_order(lines=[(self.serum, 2, 7.5), (self.cream, 1, 7.5)])
        first = self._deliveries(order)
        backorder = self._validate_partially(first, {self.serum: 1.0, self.cream: 1.0})
        self.assertEqual(first.state, 'done', "validating is never blocked by the COD guard")
        self.assertTrue(backorder)
        return order, first, backorder

    def test_print_raises_with_both_references(self):
        order, first, backorder = self._order_with_backorder()
        self.assertEqual(len(self._deliveries(order)), 2)
        for picking in (first, backorder):
            self.assertIn(order.name, picking.kh_cod_warning)
            with self.assertRaises(UserError) as error:
                picking.action_print_cod_label()
            message = str(error.exception)
            self.assertIn(order.name, message)
            self.assertIn(first.name, message)
            self.assertIn(backorder.name, message)
        with self.assertRaises(UserError):
            self.env['ir.actions.report']._render_qweb_html(REPORT, backorder.ids)

    def test_manual_or_paid_mode_unlocks_printing(self):
        _order, first, backorder = self._order_with_backorder()
        first.kh_payment_mode = 'paid'
        backorder.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 7.5})
        self.assertFalse(backorder.kh_cod_warning)
        action = (first | backorder).action_print_cod_label()
        self.assertEqual(action['report_name'], 'delivery_label_cod_kh.report_cod_label')

    def test_fully_paid_order_is_not_ambiguous(self):
        order, _first, backorder = self._order_with_backorder()
        invoice = self._create_invoice(order)
        self._register_payment(invoice)
        self.assertEqual(backorder.kh_cod_state, 'paid')
        self.assertFalse(backorder.kh_cod_warning)
        backorder.action_print_cod_label()

    def test_cancelled_delivery_does_not_count(self):
        order = self._create_order()
        first = self._deliveries(order)
        second = first.copy()
        self.assertEqual(second.sale_id, order)
        self.assertIn(second.name, first.kh_cod_warning)
        second.action_cancel()
        self.assertFalse(first.kh_cod_warning)
        first.action_print_cod_label()

    def test_cancelled_backorder_leaves_goods_behind(self):
        """Partial delivery whose backorder is cancelled: the goods left behind must not be collected."""
        order, first, backorder = self._order_with_backorder()
        backorder.action_cancel()
        self.assertIn('missing: 1 × Serum 30ml', first.kh_cod_warning)
        with self.assertRaises(UserError):
            first.action_print_cod_label()


@tagged('post_install', '-at_install')
class TestReceiverPhone(CodLabelCommon):

    def _phones(self, **values):
        partner = self.env['res.partner'].create(dict({'name': 'Test Receiver'}, **values))
        return self.env['stock.picking']._kh_extract_phones(partner)

    def test_phone_in_name(self):
        self.assertEqual(self._phones(name='Andyyvathhh 095634706'), '095 634 706')
        self.assertEqual(self.env['stock.picking']._kh_clean_receiver_name('Andyyvathhh 095634706'), 'Andyyvathhh')

    def test_international_prefix(self):
        self.assertEqual(self._phones(phone='+855 70 648 675'), '070 648 675')
        self.assertEqual(self._phones(phone='855 12-345-678'), '012 345 678')
        self.assertEqual(self._phones(phone='+855 (0)12 345 678'), '012 345 678')

    def test_ten_digits(self):
        self.assertEqual(self._phones(mobile='0961234567'), '096 123 4567')
        self.assertEqual(self._phones(mobile='+855 96 123 4567'), '096 123 4567')

    def test_garbage_kept_as_typed(self):
        self.assertEqual(self._phones(phone='  ask at shop  '), 'ask at shop')
        self.assertEqual(self._phones(phone='(870)-931-0505'), '(870)-931-0505')
        self.assertEqual(self._phones(name='Customer S00868'), '')

    def test_duplicates_and_limit(self):
        self.assertEqual(
            self._phones(name='Dara 012 345 678', phone='+85512345678', mobile='098.765.432'),
            '012 345 678 / 098 765 432')
        self.assertEqual(
            self._phones(phone='012345678 / 098765432', mobile='077 111 222'),
            '012 345 678 / 098 765 432', "at most two numbers")

    def test_commercial_partner_fallback(self):
        company = self.env['res.partner'].create({'name': 'Shop', 'is_company': True, 'mobile': '010 222 333'})
        contact = self.env['res.partner'].create({
            'name': 'Delivery point', 'parent_id': company.id, 'type': 'delivery'})
        self.assertEqual(self.env['stock.picking']._kh_extract_phones(contact), '010 222 333')

    def test_stored_and_editable(self):
        picking = self._deliveries(self._create_order(partner=self.messy_customer))
        self.assertEqual(picking.kh_receiver_phone, '095 634 706')
        picking.kh_receiver_phone = '011 111 111'
        picking.invalidate_recordset()
        self.assertEqual(picking.kh_receiver_phone, '011 111 111', "manual edit survives")
        self.messy_customer.mobile = '012 000 111'
        self.assertEqual(picking.kh_receiver_phone, '012 000 111 / 095 634 706', "partner change recomputes")


@tagged('post_install', '-at_install')
class TestLabelValues(CodLabelCommon):

    def test_parcels(self):
        picking = self._deliveries(self._create_order())
        picking.kh_parcel_count = 3
        labels = self._labels(picking)
        self.assertEqual([label['parcel_label'] for label in labels], ['1/3', '2/3', '3/3'])
        self.assertEqual({label['money']['amount_str'] for label in labels}, {'$17.00'})
        two = self._deliveries(self._create_order())
        self.assertEqual(len((picking | two)._kh_get_label_values()), 4)

    def test_receiver_block(self):
        label = self._labels(self._deliveries(self._create_order()))[0]
        self.assertEqual(label['receiver_name'], 'Sok Dara')
        self.assertEqual(label['phones'], ['012 345 678'])
        self.assertEqual(label['province'], 'ខេត្តកណ្តាល')
        self.assertEqual((label['province_prefix'], label['province_main']), ('ខេត្ត', 'កណ្តាល'))
        self.assertEqual(label['province_city'], 'ក្រុងតាខ្មៅ', "the city / district prints in the province box")
        self.assertNotIn('ក្រុងតាខ្មៅ', label['address'])
        self.assertIn('ផ្ទះលេខ ១២៣', label['address'])
        self.assertNotIn('Cambodia', label['address'], "country of the company is not printed")
        self.assertNotIn('ខេត្តកណ្តាល', label['address'])
        self.assertEqual(label['company_phone'], '031 266 3333')
        self.assertEqual(label['courier'], 'Delivery')

    def test_messy_partner(self):
        label = self._labels(self._deliveries(self._create_order(partner=self.messy_customer)))[0]
        self.assertEqual(label['receiver_name'], 'Andyyvathhh')
        self.assertEqual(label['phones'], ['095 634 706'])
        self.assertFalse(label['address'])
        self.assertFalse(label['province'])

    def test_province_falls_back_to_city(self):
        partner = self.env['res.partner'].create({'name': 'Dara', 'city': 'Phnom Penh', 'street': 'Street 2004'})
        label = self._labels(self._deliveries(self._create_order(partner=partner)))[0]
        self.assertEqual(label['province'], 'Phnom Penh')
        self.assertEqual(label['address'], 'Street 2004')

    def test_long_texts_are_fitted(self):
        partner = self.env['res.partner'].create({
            'name': 'លោកស្រី ហេង សុវណ្ណារ៉ា (ហាងលក់គ្រឿងសំអាង) សាខាទី២ ផ្សារថ្មី',
            'street': 'ផ្ទះលេខ ៨៨ក ផ្លូវលេខ ៣៧១ ភូមិត្រពាំងឈូក សង្កាត់ទឹកថ្លា ខណ្ឌសែនសុខ' * 3,
            'state_id': self.kandal.id,
        })
        picking = self._deliveries(self._create_order(partner=partner))
        picking.kh_label_note = 'Call 30 min before arrival, deliver after 5pm only, leave at the pharmacy next door'
        label = self._labels(picking)[0]
        self.assertTrue(label['receiver_name'].endswith('…'))
        self.assertLessEqual(count_units(label['receiver_name']), 40)
        self.assertLessEqual(count_units(label['address']), 110)
        self.assertLessEqual(len(label['address_rows']), 3)
        self.assertLessEqual(len(label['note']), 60)
        # a cut never leaves a dangling Khmer sign or COENG before the ellipsis
        for text in (label['receiver_name'], label['address']):
            self.assertNotEqual(text[-2], '្')

    def test_items_summary(self):
        order = self._create_order(lines=[
            (self.serum, 2, 7.5), (self.cream, 1, 7.5), (self.lip, 3, 2.0),
            (self.env['product.product'].create({'name': 'Toner 150ml', 'type': 'consu'}), 1, 5.0),
        ])
        picking = self._deliveries(order)
        picking.kh_allow_check = False
        items, more, total = picking._kh_get_label_items(3)
        self.assertEqual(total, '7')
        self.assertTrue(items.startswith('2× Serum 30ml, 1× Night Cream'), items)
        self.assertNotIn('SER30', items, "no internal reference")
        self.assertEqual(more, 1)
        # done quantities after validation, cancelled moves skipped
        picking.move_ids.filtered(lambda m: m.product_id == self.lip)._action_cancel()
        for move in picking.move_ids.filtered(lambda m: m.state != 'cancel'):
            move.quantity = 1.0
            move.picked = True
        picking.with_context(cancel_backorder=True)._action_done()
        self.assertEqual(picking.state, 'done')
        items, more, total = picking._kh_get_label_items(5)
        self.assertEqual(total, '3')
        self.assertNotIn('Lip Balm', items)
        self.assertIn('1× Serum 30ml', items)

    def test_items_hidden(self):
        self.company.kh_label_show_items = False
        picking = self._deliveries(self._create_order())
        label = self._labels(picking)[0]
        self.assertFalse(label['items'])
        self.assertEqual(label['items_lines'], 0)
        picking.kh_label_note = 'Call before 5pm'
        self.assertEqual(self._labels(picking)[0]['note_rows'], ['Call before 5pm'])
        picking.kh_label_note = 'Call 30 min before arrival, deliver after 5pm only, leave at the pharmacy next door'
        label = self._labels(picking)[0]
        self.assertGreaterEqual(label['note_lines'], 2, "the note gets the whole items area")
        self.assertEqual(label['items_lines'], 0)

    def test_items_line_kept_next_to_long_note(self):
        """A 2-line note leaves one line to the items: the quantity and number of products stay."""
        products = [
            self.env['product.product'].create({
                'name': 'Very Long Product Name Number %s For The Label' % i, 'type': 'consu'})
            for i in range(6)
        ]
        picking = self._deliveries(self._create_order(lines=[(product, 2, 1.0) for product in products]))
        picking.write({
            'kh_fragile': True,
            'kh_label_note': 'Call 30 min before arrival, deliver after 5pm only, leave at the pharmacy next door',
        })
        label = self._labels(picking)[0]
        self.assertEqual((label['note_lines'], label['items_lines']), (2, 1))
        self.assertEqual(label['total_qty'], '12')
        self.assertEqual(label['items_more'] + len(label['items'].split('× ')) - 1, 6)
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, picking.ids)[0].decode()
        self.assertIn('Qty 12', html)
        self.assertIn('o_kh_items o_kh_rows o_kh_lines_1', html)
        self.assertIn('o_kh_note o_kh_rows o_kh_lines_2', html)
        if not label['items']:
            self.assertIn('6 <span class="o_kh_kh">មុខ</span> products', html)

    def test_qr(self):
        picking = self._deliveries(self._create_order())
        label = self._labels(picking)[0]
        self.assertTrue(label['info_qr'].startswith('data:image/png;base64,'), "map QR from the geolocation")
        self.assertEqual(label['info_qr_kind'], 'map')
        self.assertTrue(label['barcode_bars'], "Code128 drawn as HTML bars")
        # PAID label: the map QR stays in the payment section with its long caption
        picking.kh_payment_mode = 'paid'
        label = self._labels(picking)[0]
        self.assertEqual(label['info_qr_place'], 'payment')
        self.assertEqual(label['info_qr_caption_lines'], ('ស្កេនមើលទីតាំង · MAP',))
        self.company.kh_label_qr_content = 'none'
        label = self._labels(picking)[0]
        self.assertFalse(label['info_qr'])
        self.assertFalse(label['info_qr_place'])

    def test_handling_defaults(self):
        picking = self._deliveries(self._create_order())
        self.assertTrue(picking.kh_allow_check)
        self.assertFalse(picking.kh_fragile)
        self.company.write({'kh_label_allow_check_default': False, 'kh_label_fragile_default': True})
        picking = self._deliveries(self._create_order())
        self.assertFalse(picking.kh_allow_check)
        self.assertTrue(picking.kh_fragile)

    def test_date_format(self):
        picking = self._deliveries(self._create_order())
        picking.scheduled_date = fields.Datetime.to_datetime('2026-03-04 20:00:00')  # 05/03 in Phnom Penh
        self.assertEqual(picking.with_context(tz='Asia/Phnom_Penh')._kh_label_date(), '05/03/2026')


@tagged('post_install', '-at_install')
class TestLabelText(CodLabelCommon):

    def test_truncate_keeps_khmer_clusters(self):
        """Limits count visible characters: a Khmer syllable is one character per base consonant."""
        text = 'ខេត្តបន្ទាយមានជ័យ'
        self.assertEqual(count_units(text), 9)
        for limit in range(1, count_units(text)):
            cut = truncate(text, limit)
            self.assertTrue(cut.endswith('…'))
            self.assertLessEqual(count_units(cut), limit)
            if len(cut) > 1:  # a bare ellipsis when not even one syllable fits
                self.assertNotEqual(cut[-2], '្', cut)
        self.assertEqual(truncate(text, 1), '…')
        self.assertEqual(truncate(text, 2), 'ខេ…', "a consonant keeps its vowel sign")
        self.assertEqual(truncate(text, 3), 'ខេត្ត…', "a subscript consonant stays with its base")
        self.assertEqual(truncate(text, 9), text)

    def test_khmer_is_wider_than_latin(self):
        self.assertGreater(text_width_mm('ខេត្តកណ្តាល', 10), text_width_mm('Kandal Pro', 10))
        fitted = fit_width('Sophea Chanthavy Rattanakmonyroth Kim and family', 40, 9, bold=True)
        self.assertTrue(fitted.endswith('…'))
        self.assertLessEqual(text_width_mm(fitted, 9, bold=True), 40)


@tagged('post_install', '-at_install')
class TestCodLabelReport(CodLabelCommon):

    def test_print_action(self):
        picking = self._deliveries(self._create_order())
        action = picking.action_print_cod_label()
        self.assertEqual(action['type'], 'ir.actions.report')
        self.assertEqual(action['report_name'], 'delivery_label_cod_kh.report_cod_label')
        report = self.env.ref(REPORT)
        self.assertEqual(report.binding_model_id.model, 'stock.picking')
        self.assertEqual(report.paperformat_id.page_width, 100)
        self.assertEqual(report.paperformat_id.page_height, 80)

    def test_html_content(self):
        cod = self._deliveries(self._create_order())
        cod.kh_parcel_count = 2
        paid = self._deliveries(self._create_order())
        paid.kh_payment_mode = 'paid'
        none = self._create_picking(self.picking_type_out)
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, (cod | paid | none).ids)[0].decode()
        self.assertEqual(html.count('o_kh_label_article'), 4, "one article per label")
        for text in ('$17.00', '≈ 69,000', '៛', 'ប្រាក់ត្រូវប្រមូល', 'PAID · ', 'បានទូទាត់រួច',
                     'DO NOT COLLECT', 'NO COD', 'កណ្តាល', '012 345 678', '1/2', '2/2', cod.name, none.name,
                     'Total for 2 parcels', 'Nothing to collect'):
            self.assertIn(text, html)
        # Khmer runs are printed in their own font stack (see kh_markup)
        self.assertIn('<span class="o_kh_kb">កណ្តាល</span>', html)


@tagged('post_install', '-at_install')
class TestCodLabelPdf(CodLabelCommon, HttpCase):
    """Real PDF rendering. An HttpCase, so that wkhtmltopdf loads the report CSS bundle from the
    running test server (``web.base.url`` points to it) like in production."""

    def test_pdf_pages(self):
        """Real PDF: one 100 x 80 mm page per label, no blank page."""
        if self.env['ir.actions.report'].get_wkhtmltopdf_state() != 'ok':
            self.skipTest("wkhtmltopdf is not available")
        # wkhtmltopdf fetches the CSS bundle from report.url (when set) or web.base.url
        self.env['ir.config_parameter'].sudo().set_param('report.url', self.base_url())
        first = self._deliveries(self._create_order())
        second = self._deliveries(self._create_order(partner=self.messy_customer))
        second.kh_parcel_count = 3
        third = self._create_picking(self.picking_type_out)
        pickings = first | second | third
        expected_pages = len(pickings._kh_get_label_values())
        self.assertEqual(expected_pages, 5)
        pdf, report_type = self.env['ir.actions.report'].with_context(force_report_rendering=True)._render_qweb_pdf(
            REPORT, res_ids=pickings.ids)
        self.assertEqual(report_type, 'pdf')
        reader = PdfFileReader(io.BytesIO(pdf), strict=False)
        self.assertEqual(reader.getNumPages(), expected_pages)
        for index in range(reader.getNumPages()):
            box = reader.getPage(index).mediaBox
            width, height = float(box.getWidth()), float(box.getHeight())
            self.assertAlmostEqual(width / MM, 100.0, delta=0.5, msg="page %s width" % (index + 1))
            self.assertAlmostEqual(height / MM, 80.0, delta=0.5, msg="page %s height" % (index + 1))


@tagged('post_install', '-at_install')
class TestCodLabelSettings(CodLabelCommon):

    def test_unticking_default_true_booleans_persists(self):
        Settings = self.env['res.config.settings'].sudo()
        settings = Settings.create({})
        self.assertTrue(settings.kh_label_show_khr)
        self.assertTrue(settings.kh_label_allow_check_default)
        settings.write({
            'kh_label_show_khr': False, 'kh_label_allow_check_default': False, 'kh_label_show_items': False,
            'kh_label_khr_rate_source': 'fixed', 'kh_label_khr_rate': 4000.0,
        })
        settings.execute()
        self.assertFalse(self.company.kh_label_show_khr)
        self.assertFalse(self.company.kh_label_allow_check_default)
        self.assertFalse(self.company.kh_label_show_items)
        self.assertEqual(self.company.kh_label_khr_rate_source, 'fixed')
        reopened = Settings.create({})
        self.assertFalse(reopened.kh_label_show_khr)
        self.assertFalse(reopened.kh_label_allow_check_default)
        self.assertFalse(reopened.kh_label_show_items)
        self.assertEqual(reopened.kh_label_khr_rate, 4000.0)
