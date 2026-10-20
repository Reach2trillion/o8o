# -*- coding: utf-8 -*-
"""Tests of the receipt documents printed by the phone and of the ABJ additions to the phone
screen RPCs."""
import babel.dates

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import new_test_user, tagged
from odoo.tools.misc import formatLang

from .common import CheckinmeCommon, MAIL_CONTEXT, TZ

BLOCK_KEYS = {
    'text': {'text'},
    'separator': set(),
    'pair': {'left', 'right'},
    'item': {'name', 'detail', 'amount'},
    'total': {'left', 'right'},
    'qr': {'value', 'size'},
    'feed': {'lines'},
}
KHMER_NOTES = 'អតិថិជនចង់បានតម្លៃពិសេស សម្រាប់ការបញ្ជាទិញលើកក្រោយ'
KHMER_CUSTOMER = 'ផ្សារ ABC'


@tagged('post_install', '-at_install')
class TestReceipts(CheckinmeCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write({'name': 'ABJ SkinCare', 'phone': '012 345 678', 'street': 'Street 271',
                           'city': 'Phnom Penh', 'email': 'sales@abj.example', 'vat': 'K001-123456789'})
        cls.khmer_partner = cls.env['res.partner'].with_context(**MAIL_CONTEXT).create({
            'name': KHMER_CUSTOMER, 'is_company': True, 'phone': '097 111 2222', 'street': 'ផ្លូវ ២៧១',
            'city': 'Phnom Penh', 'partner_latitude': 11.5564, 'partner_longitude': 104.9282,
        })

    def _rep_model(self):
        return self.env['checkinme.checkin'].with_user(self.rep).with_context(**MAIL_CONTEXT)

    def _visit_done(self):
        visit = self._create_checkin(self.rep, partner_id=self.khmer_partner.id, purpose='Present the new serum',
                                     notes=KHMER_NOTES)
        self._rep_model().mobile_checkout(visit.id, {
            'outcome': 'order', 'sale_amount': '150.5', 'next_action_date': '2026-12-01',
        })
        return visit

    def _assert_valid_document(self, doc):
        self.assertEqual(set(doc), {'title', 'name', 'blocks'})
        for block in doc['blocks']:
            self.assertIn(block['type'], BLOCK_KEYS, block)
            self.assertLessEqual(BLOCK_KEYS[block['type']], set(block), block)
            for key in ('text', 'left', 'right', 'name', 'detail', 'amount', 'value'):
                if key in block:
                    self.assertIsInstance(block[key], str, block)
            if 'align' in block:
                self.assertIn(block['align'], ('left', 'center', 'right'))
            if 'size' in block and block['type'] == 'text':
                self.assertIn(block['size'], ('s', 'm', 'l'))

    @staticmethod
    def _pairs(doc):
        return {block['left']: block['right'] for block in doc['blocks'] if block['type'] == 'pair'}

    @staticmethod
    def _texts(doc):
        return [block['text'] for block in doc['blocks'] if block['type'] == 'text']

    def test_visit_slip(self):
        visit = self._visit_done()
        doc = self._rep_model().mobile_get_receipt(visit.id)
        self._assert_valid_document(doc)
        self.assertEqual((doc['title'], doc['name']), ('VISIT SLIP', visit.name))
        blocks = doc['blocks']
        self.assertEqual(blocks[0], {'type': 'text', 'text': 'ABJ SkinCare', 'align': 'center', 'size': 'l',
                                     'bold': True})
        texts = self._texts(doc)
        self.assertIn('Street 271, Phnom Penh', texts)
        self.assertIn('012 345 678 · sales@abj.example', texts)
        self.assertIn('Tax ID: K001-123456789', texts)
        self.assertIn('VISIT SLIP', texts)
        self.assertIn(visit.name, texts)
        pairs = self._pairs(doc)
        checkin_local = visit._to_local(visit.checkin_time)
        self.assertEqual(pairs['Date'], checkin_local.strftime('%d %b %Y'))
        self.assertEqual(pairs['Check-in'], checkin_local.strftime('%H:%M'))
        self.assertEqual(pairs['Check-out'], visit._to_local(visit.checkout_time).strftime('%H:%M'))
        self.assertEqual(pairs['Salesperson'], 'Sok Dara')
        # Khmer strings pass through untouched (the phone renders them to a bitmap)
        self.assertEqual(pairs['Customer'], KHMER_CUSTOMER)
        self.assertIn(KHMER_NOTES, texts)
        self.assertTrue(any('ផ្លូវ ២៧១' in text for text in texts))
        self.assertEqual(pairs['Phone'], '097 111 2222')
        self.assertEqual(pairs['Activity'], self.type_visit.name)
        self.assertTrue(pairs['Location'].startswith('Verified On Site ('), pairs['Location'])
        self.assertEqual(pairs['Outcome'], 'Order Taken')
        self.assertEqual(pairs['Sales'], formatLang(self.env, 150.5, currency_obj=self.company.currency_id))
        self.assertTrue(pairs['Follow-up'])
        self.assertIn('Present the new serum', texts)
        qr = [block for block in blocks if block['type'] == 'qr']
        self.assertEqual(qr, [{'type': 'qr', 'value': visit.google_maps_url, 'size': 180}])
        self.assertTrue(any(text.startswith('Printed ') for text in texts))
        self.assertEqual(blocks[-1], {'type': 'feed', 'lines': 3})

    def test_visit_slip_without_position_or_customer(self):
        self._set_param('checkinme.require_gps', 'False')
        visit = self._create_checkin(self.rep, partner_id=False, checkin_type_id=self.type_other.id,
                                     latitude=0.0, longitude=0.0)
        doc = self._rep_model().mobile_get_receipt(visit.id, kind='visit')
        self._assert_valid_document(doc)
        self.assertFalse([block for block in doc['blocks'] if block['type'] == 'qr'])
        pairs = self._pairs(doc)
        self.assertNotIn('Customer', pairs)
        self.assertNotIn('Check-out', pairs)
        self.assertEqual(pairs['Location'], 'No GPS Data')

    def test_order_receipt(self):
        visit = self._visit_done()
        order = self.env['sale.order'].with_context(**MAIL_CONTEXT).create({
            'partner_id': self.khmer_partner.id,
            'user_id': self.rep.id,
            'checkinme_checkin_id': visit.id,
            'payment_term_id': self.env.ref('account.account_payment_term_30days').id,
            'order_line': [
                Command.create({'display_type': 'line_section', 'name': 'សេរ៉ូម Serums'}),
                Command.create({'product_id': self.product.id, 'name': 'Serum 30ml', 'product_uom_qty': 2.0,
                                'price_unit': 12.0}),
                Command.create({'product_id': self.product.id, 'name': 'Serum 50ml', 'product_uom_qty': 1.5,
                                'price_unit': 20.0, 'discount': 10.0}),
                Command.create({'display_type': 'line_note', 'name': 'Delivery on Friday'}),
            ],
        })
        Checkin = self._rep_model()
        doc = Checkin.mobile_get_receipt(visit.id, kind='order', order_id=order.id)
        self._assert_valid_document(doc)
        self.assertEqual((doc['title'], doc['name']), ('QUOTATION', order.name))
        currency = order.currency_id

        def amount(value):
            return formatLang(self.env, value, currency_obj=currency)
        items = [block for block in doc['blocks'] if block['type'] == 'item']
        uom = order.order_line[1].product_uom.name
        self.assertEqual(items, [
            {'type': 'item', 'name': 'Serum 30ml', 'detail': '2 %s × %s' % (uom, amount(12.0)), 'amount': amount(24.0)},
            {'type': 'item', 'name': 'Serum 50ml', 'detail': '1.50 %s × %s (-10%%)' % (uom, amount(20.0)),
             'amount': amount(27.0)},
        ])
        texts = self._texts(doc)
        self.assertIn('សេរ៉ូម Serums', texts)
        self.assertIn('Delivery on Friday', texts)
        section = next(block for block in doc['blocks'] if block.get('text') == 'សេរ៉ូម Serums')
        self.assertTrue(section.get('bold'))
        pairs = self._pairs(doc)
        self.assertEqual(pairs['Customer'], KHMER_CUSTOMER)
        self.assertEqual(pairs['Salesperson'], self.rep.name)
        self.assertEqual(pairs['Visit'], visit.name)
        self.assertEqual(pairs['Untaxed Amount'], amount(order.amount_untaxed))
        self.assertEqual(pairs['Taxes'], amount(order.amount_tax))
        totals = [block for block in doc['blocks'] if block['type'] == 'total']
        self.assertEqual(totals, [{'type': 'total', 'left': 'Total', 'right': amount(order.amount_total)}])
        self.assertTrue(any(text.startswith('Payment terms: ') for text in texts))
        # once confirmed it is a sales order
        order.action_confirm()
        self.assertEqual(Checkin.mobile_get_receipt(visit.id, kind='order', order_id=order.id)['title'], 'SALES ORDER')

    def test_cancelled_order_is_not_printed(self):
        """server-correctness-4: a cancelled quotation is neither offered nor printed as a sales order."""
        visit = self._visit_done()
        order = self._create_confirmed_order(self.rep, partner=self.khmer_partner, checkinme_checkin_id=visit.id)
        Checkin = self._rep_model()
        self.assertEqual([o['id'] for o in Checkin.mobile_get_checkin(visit.id)['sale_orders']], [order.id])
        order._action_cancel()
        self.assertEqual(order.state, 'cancel')
        self.assertEqual(Checkin.mobile_get_checkin(visit.id)['sale_orders'], [])
        with self.assertRaisesRegex(UserError, 'cancelled'):
            Checkin.mobile_get_receipt(visit.id, kind='order', order_id=order.id)

    def test_order_receipt_precisions(self):
        """server-correctness-5: quantities and unit prices use the configured precisions."""
        self.env.ref('product.decimal_product_uom').digits = 3
        self.env.ref('product.decimal_price').digits = 3
        visit = self._visit_done()
        order = self.env['sale.order'].with_context(**MAIL_CONTEXT).create({
            'partner_id': self.khmer_partner.id, 'user_id': self.rep.id, 'checkinme_checkin_id': visit.id,
            'order_line': [Command.create({'product_id': self.product.id, 'name': 'Rice (kg)',
                                           'product_uom_qty': 0.125, 'price_unit': 0.125})],
        })
        doc = self._rep_model().mobile_get_receipt(visit.id, kind='order', order_id=order.id)
        item = next(block for block in doc['blocks'] if block['type'] == 'item')
        uom = order.order_line.product_uom.name
        price = formatLang(self.env, 0.125, dp='Product Price', currency_obj=order.currency_id)
        self.assertEqual(item['detail'], '0.125 %s × %s' % (uom, price))
        self.assertIn('0.125', price)

    def test_receipt_dates_in_the_user_language(self):
        """C5: receipt dates use the month names of the user's language."""
        self.env['res.lang']._activate_lang('km_KH')
        visit = self._visit_done()
        english = self._pairs(self._rep_model().with_context(lang='en_US').mobile_get_receipt(visit.id))
        khmer = self._pairs(self._rep_model().with_context(lang='km_KH').mobile_get_receipt(visit.id))
        local = visit._to_local(visit.checkin_time)
        self.assertEqual(english['Date'], local.strftime('%d %b %Y'))
        self.assertIn(babel.dates.format_date(local.date(), 'dd MMM y', locale='km_KH'), khmer.values())
        self.assertNotIn(english['Date'], khmer.values())

    def test_receipt_access(self):
        Checkin = self._rep_model()
        visit = self._visit_done()
        # an order of another visit
        other_order = self._create_confirmed_order(self.rep)
        with self.assertRaises(UserError):
            Checkin.mobile_get_receipt(visit.id, kind='order', order_id=other_order.id)
        with self.assertRaises(UserError):
            Checkin.mobile_get_receipt(visit.id, kind='order', order_id=999999)
        with self.assertRaises(UserError):
            Checkin.mobile_get_receipt(visit.id, kind='order')
        with self.assertRaises(UserError):
            Checkin.mobile_get_receipt(visit.id, kind='invoice')
        with self.assertRaises(UserError):
            Checkin.mobile_get_receipt(999999)
        # linked to the visit but owned by another salesperson ("own documents only")
        foreign_order = self._create_confirmed_order(self.other_rep, checkinme_checkin_id=visit.id)
        with self.assertRaises(AccessError):
            Checkin.mobile_get_receipt(visit.id, kind='order', order_id=foreign_order.id)
        # somebody else's visit
        other_visit = self._create_checkin(self.other_rep)
        with self.assertRaises(AccessError):
            Checkin.mobile_get_receipt(other_visit.id)
        # the Sale Check manager prints the visit slip of the team; an order needs the Sales rights
        self.assertEqual(self.env['checkinme.checkin'].with_user(self.manager).mobile_get_receipt(visit.id)['name'],
                         visit.name)
        with self.assertRaises(AccessError):
            self.env['checkinme.checkin'].with_user(self.manager).mobile_get_receipt(
                visit.id, kind='order', order_id=foreign_order.id)
        doc = self.env['checkinme.checkin'].mobile_get_receipt(visit.id, kind='order', order_id=foreign_order.id)
        self.assertEqual(doc['name'], foreign_order.name)


@tagged('post_install', '-at_install')
class TestMobileAbj(CheckinmeCommon):

    def test_dashboard_app_info(self):
        self.company.write({'name': 'ABJ SkinCare', 'phone': '012 345 678', 'city': 'Phnom Penh'})
        self._set_param('checkinme.fcm_service_account', False)
        app = self.env['checkinme.checkin'].with_user(self.rep).mobile_get_dashboard()['app']
        self.assertEqual(app['name'], 'ABJ')
        self.assertEqual(app['push'], {'web_push_ready': True, 'native_ready': False})
        self.assertEqual(app['company']['name'], 'ABJ SkinCare')
        self.assertEqual(app['company']['phone'], '012 345 678')
        self.assertEqual(set(app['company']), {'name', 'phone', 'email', 'website', 'vat', 'street', 'city'})
        self.assertEqual(app['server_version'], '18.0')
        # also without employee
        user = new_test_user(self.env, login='checkinme_test_noemp_abj',
                             groups='base.group_user,checkinme_sales_activity.group_checkinme_user',
                             context=MAIL_CONTEXT, tz=TZ)
        self.assertEqual(self.env['checkinme.checkin'].with_user(user).mobile_get_dashboard()['app']['name'], 'ABJ')

    def test_sale_orders_in_mobile_vals(self):
        visit = self._create_checkin(self.rep)
        own = self._create_confirmed_order(self.rep, checkinme_checkin_id=visit.id, price_unit=250.0)
        self._create_confirmed_order(self.other_rep, checkinme_checkin_id=visit.id)
        vals = self.env['checkinme.checkin'].with_user(self.rep).mobile_get_checkin(visit.id)
        self.assertEqual(vals['sale_orders'], [{
            'id': own.id, 'name': own.name, 'state_label': 'Sales Order',
            'amount_total_display': formatLang(self.env, own.amount_total, currency_obj=own.currency_id),
        }], "only the orders the salesperson may read")
        self.assertEqual(len(self.env['checkinme.checkin'].mobile_get_checkin(visit.id)['sale_orders']), 2)
        # a user without any Sales right still gets the screen
        user = new_test_user(self.env, login='checkinme_test_nosale',
                             groups='base.group_user,checkinme_sales_activity.group_checkinme_user',
                             context=MAIL_CONTEXT, tz=TZ)
        employee = self.env['hr.employee'].create({'name': 'No Sale', 'user_id': user.id, 'tz': TZ})
        no_sale_visit = self._create_checkin(user, employee_id=employee.id)
        self.assertEqual(self.env['checkinme.checkin'].with_user(user).mobile_get_checkin(no_sale_visit.id)['sale_orders'], [])
