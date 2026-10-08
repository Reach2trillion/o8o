# -*- coding: utf-8 -*-
"""Payment QR code ("scan to pay") of COD labels: section 9 of the specification."""
import base64
import io
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from odoo.tests.common import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.payment import utils as payment_utils
from .common import CodLabelCommon

REPORT = 'delivery_label_cod_kh.action_report_cod_label'


def _png(size=64):
    """Small black and white PNG (base64) standing for the shop's static ABA KHQR image."""
    image = Image.new('L', (size, size), 255)
    for x in range(0, size, 8):
        image.paste(0, (x, x, x + 8, x + 8))
    output = io.BytesIO()
    image.save(output, format='PNG')
    return base64.b64encode(output.getvalue())


class PaymentQrCommon(CodLabelCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write({'kh_label_pay_qr': 'odoo_link', 'kh_label_khqr_image': False})

    def _fake_request(self):
        """What ``payment.utils`` reads from the HTTP request: its environment (same database secret)."""
        return SimpleNamespace(env=self.env)

    def _check_token(self, token, order, amount):
        with patch('odoo.addons.payment.utils.request', self._fake_request()):
            return payment_utils.check_access_token(
                token, order.partner_invoice_id.id, amount, order.currency_id.id)

    @staticmethod
    def _query(link):
        return {key: values[0] for key, values in parse_qs(urlsplit(link).query).items()}


@tagged('post_install', '-at_install')
class TestPaymentQr(PaymentQrCommon):

    def test_settings_default(self):
        company = self.env['res.company'].create({'name': 'New Shop (test)'})
        self.assertEqual(company.kh_label_pay_qr, 'odoo_link')
        self.assertFalse(company.kh_label_pay_url_template)

    def test_odoo_payment_link(self):
        order = self._create_order()
        picking = self._deliveries(order)
        link = picking.kh_payment_link
        self.assertTrue(link.startswith('%s/payment/pay?' % order.get_base_url()), link)
        query = self._query(link)
        self.assertEqual(set(query), {'amount', 'access_token', 'sale_order_id'})
        self.assertEqual(query['sale_order_id'], str(order.id))
        self.assertEqual(float(query['amount']), 17.0, "the link amount is the printed COD amount")
        self.assertTrue(self._check_token(query['access_token'], order, 17.0))
        self.assertFalse(self._check_token(query['access_token'], order, 17.01), "the token signs the amount")

        label = self._labels(picking)[0]
        pay_qr = label['pay_qr']
        self.assertEqual(pay_qr['mode'], 'odoo_link')
        self.assertEqual(pay_qr['url'], link)
        self.assertTrue(pay_qr['image'].startswith('data:image/png;base64,'))
        self.assertEqual(pay_qr['caption'], 'ស្កេនដើម្បីទូទាត់ · SCAN TO PAY $17.00')
        # the information QR (map) moves to the receiver section, with its own caption
        self.assertEqual(label['info_qr_kind'], 'map')
        self.assertEqual(label['info_qr_place'], 'receiver')
        self.assertIn('MAP', label['info_qr_caption_lines'])

    def test_token_identical_to_http_request_token(self):
        """Outside a request the wizard signs the very same token as inside one."""
        order = self._create_order()
        offline_link = self._deliveries(order).kh_payment_link
        fake = self._fake_request()
        with patch('odoo.addons.payment.utils.request', fake), \
                patch('odoo.addons.delivery_label_cod_kh.models.payment_link_wizard.request', fake):
            wizard = self.env['payment.link.wizard'].sudo().new({
                'res_model': 'sale.order', 'res_id': order.id, 'amount': 17.0,
                'currency_id': order.currency_id.id, 'partner_id': order.partner_invoice_id.id,
            })
            self.assertEqual(wizard.link, offline_link)

    def test_link_amount_follows_partial_payment(self):
        order = self._create_order()
        self._register_payment(self._create_invoice(order), amount=10.0)
        picking = self._deliveries(order)
        query = self._query(picking.kh_payment_link)
        self.assertEqual(float(query['amount']), 7.0)
        self.assertTrue(self._check_token(query['access_token'], order, 7.0))
        self.assertIn('$7.00', self._labels(picking)[0]['pay_qr']['caption'])

    def test_link_of_order_in_riel(self):
        pricelist = self.env['product.pricelist'].create({'name': 'Riel (test)', 'currency_id': self.khr.id})
        order = self._create_order(lines=[(self.serum, 1, 60000.0)], carrier=8000.0, pricelist=pricelist)
        query = self._query(self._deliveries(order).kh_payment_link)
        self.assertEqual(float(query['amount']), 68000.0)
        self.assertTrue(self._check_token(query['access_token'], order, 68000.0))

    def test_manual_amount_link(self):
        order = self._create_order()
        picking = self._deliveries(order)
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 12.5})
        query = self._query(picking.kh_payment_link)
        self.assertEqual(float(query['amount']), 12.5)
        self.assertTrue(self._check_token(query['access_token'], order, 12.5))

    def test_no_payment_qr_on_paid_or_no_cod_labels(self):
        paid = self._deliveries(self._create_order())
        paid.kh_payment_mode = 'paid'
        manual_zero = self._deliveries(self._create_order())
        manual_zero.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 0.0})
        invoiced = self._create_order()
        self._register_payment(self._create_invoice(invoiced))
        no_cod = self._create_picking(self.picking_type_out)
        receipt = self._create_picking(self.picking_type_in)
        self.company.kh_label_khqr_image = _png()  # even with a KHQR image
        for picking in (paid, manual_zero, self._deliveries(invoiced), no_cod, receipt):
            self.assertNotEqual(picking.kh_cod_state, 'cod')
            self.assertFalse(picking.kh_payment_link)
            label = self._labels(picking)[0]
            self.assertFalse(label['pay_qr'], picking.name)
            self.assertEqual(label['info_qr_place'], 'payment', "PAID / NO COD labels keep the info QR")

    def test_fallback_without_sale_order(self):
        picking = self._create_picking(self.picking_type_out)
        picking.write({'kh_payment_mode': 'manual', 'kh_cod_amount_manual': 5.0})
        self.assertEqual(picking.kh_cod_state, 'cod')
        # no sales order and no KHQR image: no payment QR
        self.assertFalse(picking.kh_payment_link)
        self.assertFalse(self._labels(picking)[0]['pay_qr'])
        # no sales order, KHQR image set: the static image is printed instead
        self.company.kh_label_khqr_image = _png()
        pay_qr = self._labels(picking)[0]['pay_qr']
        self.assertEqual(pay_qr['mode'], 'khqr_image')
        self.assertFalse(pay_qr['url'])
        self.assertTrue(pay_qr['image'].startswith('data:image/png;base64,'))
        self.assertIn('$5.00', pay_qr['caption'], "the customer types the amount: the caption shows it")
        self.assertFalse(picking.kh_payment_link)

    def test_khqr_image_mode(self):
        picking = self._deliveries(self._create_order())
        self.company.kh_label_pay_qr = 'khqr_image'
        self.assertFalse(self._labels(picking)[0]['pay_qr'], "no image uploaded: no payment QR")
        self.company.kh_label_khqr_image = _png()
        pay_qr = self._labels(picking)[0]['pay_qr']
        self.assertEqual(pay_qr['mode'], 'khqr_image')
        self.assertEqual(pay_qr['caption'], 'ABA KHQR · ស្កេនដើម្បីទូទាត់ · SCAN TO PAY $17.00')
        self.assertFalse(picking.kh_payment_link)

    def test_none_mode(self):
        picking = self._deliveries(self._create_order())
        self.company.write({'kh_label_pay_qr': 'none', 'kh_label_khqr_image': _png()})
        label = self._labels(picking)[0]
        self.assertFalse(label['pay_qr'])
        self.assertFalse(picking.kh_payment_link)
        self.assertEqual(label['info_qr_place'], 'payment')

    def test_custom_url_template(self):
        order = self._create_order()
        picking = self._deliveries(order)
        self.company.write({
            'kh_label_pay_qr': 'custom_url',
            'kh_label_pay_url_template':
                'https://pay.example.com/?ref={order}&p={picking}&a={amount}&c={currency}&n={partner}&x={unknown}',
        })
        expected = 'https://pay.example.com/?ref=%s&p=%s&a=17.00&c=USD&n=Sok%%20Dara&x={unknown}' % (
            order.name, picking.name.replace('/', '%2F'))
        self.assertEqual(picking.kh_payment_link, expected)
        pay_qr = self._labels(picking)[0]['pay_qr']
        self.assertEqual((pay_qr['mode'], pay_qr['url']), ('custom_url', expected))
        # every value is URL-encoded
        self.customer.name = 'Dara & Co / ផ្ទះ'
        self.assertIn('n=Dara%20%26%20Co%20%2F%20', picking.kh_payment_link)

    def test_custom_url_template_never_crashes(self):
        picking = self._deliveries(self._create_order())
        self.company.kh_label_pay_qr = 'custom_url'
        for template, expected in (
            ('https://x.test/{amount:.2f}/{currency}', 'https://x.test/{amount:.2f}/USD'),
            ('https://x.test/{0}/{}/{amount}', 'https://x.test/{0}/{}/17.00'),
            ('https://x.test/{order.name}?a={amount}', 'https://x.test/{order.name}?a=17.00'),
            ('https://x.test/{?a={amount}', 'https://x.test/{?a=17.00'),
            ('https://x.test/}{currency}', 'https://x.test/}USD'),
        ):
            self.company.kh_label_pay_url_template = template
            self.assertEqual(picking._kh_get_payment_url(), ('custom_url', expected), template)
        # an empty template prints no payment QR
        self.company.kh_label_pay_url_template = '  '
        self.assertFalse(self._labels(picking)[0]['pay_qr'])
        self.assertFalse(picking.kh_payment_link)

    def test_reference_info_qr_omitted_next_to_payment_qr(self):
        """The payment QR wins: a reference QR (already in the Code128 barcode) is omitted on COD
        labels with a payment QR, so the receiver texts keep their full width."""
        partner = self.env['res.partner'].create({'name': 'No Geo', 'phone': '012 111 222'})
        picking = self._deliveries(self._create_order(partner=partner))
        label = self._labels(picking)[0]
        self.assertTrue(label['pay_qr'])
        self.assertFalse(label['info_qr'])
        self.assertFalse(label['info_qr_place'])
        self.assertTrue(label['barcode'])
        # without payment QR (PAID label) the reference QR prints in the payment section as before
        picking.kh_payment_mode = 'paid'
        label = self._labels(picking)[0]
        self.assertEqual((label['info_qr_kind'], label['info_qr_place']), ('reference', 'payment'))
        self.assertEqual(label['info_qr_caption_lines'], (picking.name,))
        # same with the "Transfer reference" setting for a contact with a geolocation
        self.company.kh_label_qr_content = 'reference'
        cod = self._deliveries(self._create_order())
        self.assertFalse(self._labels(cod)[0]['info_qr'])
        self.company.kh_label_qr_content = 'none'
        self.assertFalse(self._labels(cod)[0]['info_qr'])
        self.assertTrue(self._labels(cod)[0]['pay_qr'])

    def test_amount_fits_with_caption_and_parcels(self):
        picking = self._deliveries(self._create_order())
        self.assertEqual(self._labels(picking)[0]['money']['amount_size'], 25)
        picking.kh_parcel_count = 2
        sizes = {label['money']['amount_size'] for label in self._labels(picking)}
        self.assertEqual(len(sizes), 1)
        self.assertLessEqual(sizes.pop(), 19, "fee breakdown + parcels + scan-to-pay lines: smaller amount")

    def test_html(self):
        cod = self._deliveries(self._create_order())
        paid = self._deliveries(self._create_order())
        paid.kh_payment_mode = 'paid'
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, (cod | paid).ids)[0].decode()
        self.assertEqual(html.count('SCAN TO PAY'), 1, "only the COD label has a payment QR")
        self.assertEqual(html.count('class="o_kh_payqr_cell'), 1)
        self.assertEqual(html.count('class="o_kh_info_cell"'), 1, "COD label: info QR in the receiver section")
        self.assertEqual(html.count('class="o_kh_qr_cell"'), 1, "PAID label: info QR in the payment section")
        self.assertIn('ស្កេនមើលទីតាំង', html)


@tagged('post_install', '-at_install')
class TestPaymentLinkHttp(PaymentQrCommon, HttpCase):
    """The printed link opens the payment form of the order (public user, as the customer)."""

    def test_printed_link_opens_payment_form(self):
        order = self._create_order()
        link = self._deliveries(order).kh_payment_link
        self.assertTrue(link.startswith(self.base_url()), "HttpCase sets web.base.url to the test server")
        response = self.url_open(link)
        self.assertEqual(response.status_code, 200)
        self.assertIn(order.name, response.text, "the payment form shows the order reference")
        self.assertIn('17.00', response.text, "the payment form shows the amount")
        self.assertNotIn('There is nothing to pay', response.text)
        # a tampered amount is rejected by the controller
        with mute_logger('odoo.http'):
            tampered = self.url_open(link.replace('amount=17.0', 'amount=1.0'))
        self.assertNotEqual(tampered.status_code, 200)
        self.assertNotIn(order.name, tampered.text)
