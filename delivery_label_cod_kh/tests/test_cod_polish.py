# -*- coding: utf-8 -*-
"""Regression tests of the final polish round: receiver name, KHQR card image, items / note rows,
payment providers of the payment QR, Khmer widths."""
import base64
import io
import re

import qrcode
from PIL import Image, ImageDraw

from odoo import Command
from odoo.modules.neutralize import get_neutralization_queries
from odoo.tests.common import tagged

from odoo.addons.delivery_label_cod_kh.models import stock_picking as picking_module
from odoo.addons.delivery_label_cod_kh.models.kh_label_text import text_width_mm
from .common import CodLabelCommon

REPORT = 'delivery_label_cod_kh.action_report_cod_label'
KHQR = ('00020101021129370016abaakhppxxx@abaa01090123456785204599953038405802KH5912ABJ SKINCARE'
        '6010Phnom Penh6304ABCD')


def _image(data_uri):
    return Image.open(io.BytesIO(base64.b64decode(data_uri.split(',', 1)[1])))


def _px(style, prop='height'):
    match = re.search(r'(?:^|; )%s: (\d+)px' % prop, style)
    return int(match.group(1)) if match else 0


def _khqr_card():
    """Uncropped ABA KHQR card: red banner with white text, merchant name and amount, a dashed line,
    then the code (7 px per module) with a round logo in its centre. ``(base64 PNG, QR matrix)``."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=7, border=0)
    qr.add_data(KHQR)
    qr.make(fit=True)
    code = qr.make_image(fill_color='black', back_color='white').get_image().convert('RGB')
    width = code.width + 80
    card = Image.new('RGB', (width, code.height + 200), (255, 255, 255))
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, width, 60], fill=(225, 35, 46))
    for x in range(width // 2 - 40, width // 2 + 40, 12):  # white "KHQR" letters on the banner
        draw.rectangle([x, 18, x + 7, 42], fill=(255, 255, 255))
    for x in range(40, 200, 10):  # merchant name and amount
        draw.rectangle([x, 75, x + 6, 88], fill=(0, 0, 0))
        draw.rectangle([x, 100, x + 7, 125], fill=(0, 0, 0))
    for x in range(10, width - 10, 16):
        draw.line([x, 150, x + 8, 150], fill=(160, 160, 160), width=2)
    card.paste(code, (40, 170))
    centre_x, centre_y = 40 + code.width // 2, 170 + code.height // 2
    draw.ellipse([centre_x - 18, centre_y - 18, centre_x + 18, centre_y + 18], fill=(0, 0, 0))
    output = io.BytesIO()
    card.save(output, format='PNG')
    return base64.b64encode(output.getvalue()), qr.get_matrix()


@tagged('post_install', '-at_install')
class TestPolishReceiverName(CodLabelCommon):

    def test_brackets_kept_without_phone(self):
        clean = self.env['stock.picking']._kh_clean_receiver_name
        for name, expected in (
            ('Dara (Toul Kork)', 'Dara (Toul Kork)'),  # no phone: kept as typed
            ('(Mr) Dara', '(Mr) Dara'),
            ('Dara (095634706)', 'Dara'),  # brackets of the number alone
            ('Dara - 095634706', 'Dara'),
            ('095634706 / Dara', 'Dara'),
            ('Dara (Toul Kork) 095634706', 'Dara (Toul Kork)'),
            ('Dara (095634706 Toul Kork)', 'Dara (Toul Kork)'),
            ('Dara (095634706', 'Dara'),
            ('Dara 095634706 / 012345678', 'Dara'),
            ('Andyyvathhh 095634706', 'Andyyvathhh'),
            ('095634706', '095634706'),
        ):
            self.assertEqual(clean(name), expected, name)

    def test_custom_url_partner_keeps_brackets(self):
        partner = self.env['res.partner'].create({'name': 'Dara (Toul Kork)', 'phone': '012 345 678'})
        picking = self._deliveries(self._create_order(partner=partner))
        url = picking._kh_custom_payment_url('https://x.test/?who={partner}', 17.0, self.usd)
        self.assertEqual(url, 'https://x.test/?who=Dara%20%28Toul%20Kork%29')
        self.assertEqual(self._labels(picking)[0]['receiver_name'], 'Dara (Toul Kork)')


@tagged('post_install', '-at_install')
class TestPolishKhqrImage(CodLabelCommon):

    def test_card_image_cropped_to_its_code(self):
        """An uncropped ABA card: only the code prints, 18 mm wide, module for module."""
        card, matrix = _khqr_card()
        self.company.write({'kh_label_pay_qr': 'khqr_image', 'kh_label_khqr_image': card})
        pay_qr = self._labels(self._deliveries(self._create_order()))[0]['pay_qr']
        self.assertEqual(pay_qr['mode'], 'khqr_image')
        self.assertEqual(pay_qr['image_style'], 'width: 68px; height: 68px;', "a square code, 18 mm wide")
        printed = _image(pay_qr['image']).convert('L')
        self.assertEqual({color for _count, color in printed.getcolors()}, {0, 255}, "black and white")
        # every module of the code is printed where it belongs (the round logo hides a few)
        count = len(matrix)
        self.assertAlmostEqual(printed.width / count, printed.height / count, delta=0.01)
        module = printed.width / float(count)
        centre = count / 2.0
        wrong = [
            (row, col) for row in range(count) for col in range(count)
            if (row + 0.5 - centre) ** 2 + (col + 0.5 - centre) ** 2 > (18 / 7.0 + 1) ** 2
            and (printed.getpixel((int((col + 0.5) * module), int((row + 0.5) * module))) == 0) != matrix[row][col]
        ]
        self.assertFalse(wrong, "modules printed wrong: %s" % wrong[:10])
        settings = self.env['res.config.settings'].create({})
        self.assertFalse(settings.kh_label_khqr_image_warning)

    def test_no_code_found_falls_back_to_ink_and_warns(self):
        image = Image.new('RGB', (200, 120), (255, 255, 255))
        ImageDraw.Draw(image).rectangle([40, 30, 160, 90], fill=(0, 0, 0))  # no finder patterns
        output = io.BytesIO()
        image.save(output, format='PNG')
        self.company.write({'kh_label_pay_qr': 'khqr_image', 'kh_label_khqr_image': base64.b64encode(output.getvalue())})
        pay_qr = self._labels(self._deliveries(self._create_order()))[0]['pay_qr']
        printed = _image(pay_qr['image']).convert('L')
        self.assertEqual({color for _count, color in printed.getcolors()}, {0}, "cropped to its ink as before")
        self.assertEqual(pay_qr['image_style'], 'width: 68px; height: 34px;')
        settings = self.env['res.config.settings'].create({})
        self.assertIn('Upload only the square QR part', settings.kh_label_khqr_image_warning)
        self.company.kh_label_pay_qr = 'custom_url'
        self.assertFalse(self.env['res.config.settings'].create({}).kh_label_khqr_image_warning,
                         "the image is not used in this mode")

    def test_find_qr_box(self):
        """The box is the outer edge of the code, also in a larger white margin or rotated."""
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=5, border=6)
        qr.add_data('https://example.com/payment/pay?amount=17.0')
        qr.make(fit=True)
        image = qr.make_image(fill_color='black', back_color='white').get_image().convert('L')
        side = len(qr.get_matrix()) - 12  # get_matrix() includes the border
        expected = (30, 30, 30 + 5 * side, 30 + 5 * side)
        self.assertEqual(picking_module.kh_find_qr_box(image), expected)
        self.assertEqual(picking_module.kh_find_qr_box(image.rotate(90)), expected)
        self.assertIsNone(picking_module.kh_find_qr_box(Image.new('L', (100, 100), 255)))


@tagged('post_install', '-at_install')
class TestPolishItems(CodLabelCommon):

    def _check_budget(self, label):
        items = ((_px(label['note_style'], 'max-height') if label['note_rows'] else 0)
                 + (_px(label['items_style'], 'max-height') if label['items_lines'] else 0))
        self.assertLessEqual(items, picking_module.ITEMS_H)

    def test_khmer_note_keeps_the_items_row(self):
        """A short Khmer note no longer takes the quantity riders check on 'allow check' deliveries."""
        picking = self._deliveries(self._create_order(lines=[(self.serum, 2, 7.5), (self.cream, 1, 7.5)]))
        for note in ('សូមតេមុនដឹក ហើយទុកនៅមុខហាងកាហ្វេ',  # ~27 letters
                     'សូមតេមុនពេលដឹក១៥នាទី ហើយកុំទុកឥវ៉ាន់នៅមុខផ្ទះ ព្រោះគ្មាននរណានៅផ្ទះពេលថ្ងៃ'):
            picking.kh_label_note = note
            label = self._labels(picking)[0]
            self.assertEqual(label['note_lines'], 1, note)
            self.assertGreaterEqual(label['items_lines'], 1, note)
            self.assertTrue(label['items_rows'] or label['items_more'])
            self._check_budget(label)
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, picking.ids)[0].decode()
        self.assertIn('Qty 3', html)
        self.assertIn('o_kh_items o_kh_rows', html)

    def test_khmer_note_and_khmer_products(self):
        """Khmer product names need a taller items row: the note shrinks to 6 pt to leave it."""
        product = self.env['product.product'].create({'name': 'ក្រែមលាបមុខ ពេលយប់', 'type': 'consu'})
        picking = self._deliveries(self._create_order(lines=[(product, 2, 5.0)]))
        picking.kh_label_note = 'សូមតេមុនដឹក ហើយទុកនៅមុខហាងកាហ្វេ'
        label = self._labels(picking)[0]
        self.assertEqual((label['note_lines'], label['items_lines']), (1, 1))
        self.assertIn('font-size: 6pt', label['note_style'])
        self._check_budget(label)
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, picking.ids)[0].decode()
        self.assertIn('Qty 2', html)

    def test_latin_note_still_gets_two_rows(self):
        picking = self._deliveries(self._create_order())
        picking.kh_label_note = 'Call 30 min before arrival, deliver after 5pm only, leave at the pharmacy next door'
        label = self._labels(picking)[0]
        self.assertEqual((label['note_lines'], label['items_lines']), (2, 1))
        self.assertIn('font-size: 6.5pt', label['note_style'])

    def test_more_products_without_double_ellipsis(self):
        long_name = self.env['product.product'].create({
            'name': 'Hyaluronic Acid Deep Hydration Night Cream 50g', 'type': 'consu'})
        picking = self._deliveries(self._create_order(lines=[
            (long_name, 1, 5.0), (self.serum, 1, 7.5), (self.cream, 1, 7.5)]))
        self.company.kh_label_max_item_lines = 1
        label = self._labels(picking)[0]
        self.assertTrue(label['items_rows'][-1].endswith('…'), label['items_rows'])
        self.assertEqual((label['items_more'], label['items_more_text']), (2, '+2 more'))
        html = self.env['ir.actions.report']._render_qweb_html(REPORT, picking.ids)[0].decode()
        self.assertIn('…<b> +2 more</b>', html)
        self.assertNotIn('<b> … +2 more</b>', html)
        # a name that is not cut keeps the ellipsis before "+N more"
        self.company.kh_label_max_item_lines = 2
        picking = self._deliveries(self._create_order(lines=[
            (self.serum, 1, 7.5), (self.cream, 1, 7.5), (self.lip, 1, 2.0)]))
        label = self._labels(picking)[0]
        self.assertEqual(label['items_more_text'], '… +1 more')


@tagged('post_install', '-at_install')
class TestPolishPaymentProviders(CodLabelCommon):

    def _check_no_link(self, picking, printed='no payment QR'):
        self.assertFalse(picking.kh_payment_link)
        self.assertIn('No online payment provider', picking.kh_pay_qr_warning)
        self.assertIn(printed, picking.kh_pay_qr_warning)

    def test_link_printed_with_a_published_provider(self):
        picking = self._deliveries(self._create_order())
        self.assertTrue(picking.kh_payment_link)
        self.assertFalse(picking.kh_pay_qr_warning)
        self.assertEqual(self._labels(picking)[0]['pay_qr']['mode'], 'odoo_link')
        self.assertFalse(self.env['res.config.settings'].create({}).kh_label_pay_provider_warning)

    def test_no_provider_no_payment_qr(self):
        picking = self._deliveries(self._create_order())
        self.provider.state = 'disabled'
        self._check_no_link(picking)
        label = self._labels(picking)[0]
        self.assertFalse(label['pay_qr'])
        self.assertEqual(label['info_qr_place'], 'payment', "the info QR takes the slot back")
        self.assertIn('Payment Providers', self.env['res.config.settings'].create({}).kh_label_pay_provider_warning)
        # the static KHQR image, when set, is printed instead
        card, _matrix = _khqr_card()
        self.company.kh_label_khqr_image = card
        self._check_no_link(picking, printed='static ABA KHQR image')
        self.assertEqual(self._labels(picking)[0]['pay_qr']['mode'], 'khqr_image')
        # PAID labels: nothing to warn about
        picking.kh_payment_mode = 'paid'
        self.assertFalse(picking.kh_pay_qr_warning)

    def test_unpublished_provider(self):
        """Customers do not see unpublished providers (test mode is unpublished by default)."""
        picking = self._deliveries(self._create_order())
        self.provider.is_published = False
        self._check_no_link(picking)
        self.assertTrue(self.env['res.config.settings'].create({}).kh_label_pay_provider_warning)

    def test_provider_of_another_currency(self):
        self.provider.available_currency_ids = [Command.set(self.khr.ids)]
        self._check_no_link(self._deliveries(self._create_order()))
        pricelist = self.env['product.pricelist'].create({'name': 'Riel (test)', 'currency_id': self.khr.id})
        riel = self._deliveries(self._create_order(lines=[(self.serum, 1, 60000.0)], carrier=8000.0, pricelist=pricelist))
        self.assertTrue(riel.kh_payment_link)
        self.assertFalse(riel.kh_pay_qr_warning)

    def test_neutralized_copy(self):
        """Odoo's neutralization (Duplicate / Restore with 'Neutralize') disables the enabled providers:
        no QR code to a dead payment page. Providers in test mode are left as they are."""
        picking = self._deliveries(self._create_order())
        self.provider.state = 'enabled'
        self.assertTrue(picking.kh_payment_link)
        self.env.flush_all()
        for query in get_neutralization_queries(['payment']):
            self.env.cr.execute(query)
        self.env.invalidate_all()
        self.assertEqual(self.provider.state, 'disabled')
        self._check_no_link(picking)
        self.provider.state = 'test'
        self.env.flush_all()
        for query in get_neutralization_queries(['payment']):
            self.env.cr.execute(query)
        self.env.invalidate_all()
        self.assertEqual(self.provider.state, 'test')
        self.assertTrue(picking.kh_payment_link)


@tagged('post_install', '-at_install')
class TestPolishKhmerWidths(CodLabelCommon):

    def test_shop_tagline_is_not_cut(self):
        """The shop's tagline fits the header (about 7 mm were left free when it was cut)."""
        tagline = 'ស្បែកភ្លឺថ្លា ទំនុកចិត្តកាន់តែខ្លាំង'
        self.company.write({'name': 'ABJ SkinCare', 'kh_label_tagline': tagline})
        label = self._labels(self._deliveries(self._create_order()))[0]
        self.assertEqual(label['tagline'], tagline)

    def test_khmer_estimates_cover_measured_widths(self):
        """Widths (mm) measured in PDFs of wkhtmltopdf 0.12.6 with the label font stacks (PyMuPDF):
        the estimate is never below the real width (no overflow) and at most 12 % above it (no
        needless cut; it was up to 55 % above, and 13 % below for subscripts that take room)."""
        for text, size, bold, measured in (
            ('ស្បែកភ្លឺថ្លា ទំនុកចិត្តកាន់តែខ្លាំង', 6, False, 25.929),
            ('ខេត្តបន្ទាយមានជ័យ', 15, True, 40.481),
            ('ផ្ទះលេខ ៨៨ក ផ្លូវលេខ ៣៧១ ភូមិត្រពាំងឈូក', 8, False, 53.181),
            ('អនុញ្ញាតឱ្យពិនិត្យទំនិញ', 6, True, 19.05),
            ('ស្រីល័ក្ខ', 9, False, 9.79),  # subscripts RO and KHO
            ('ហ៊ុន សុភ័ក្រ្ត', 11, True, 20.637),  # subscript RO typed first: a dotted circle is drawn
        ):
            estimate = text_width_mm(text, size, bold)
            self.assertGreaterEqual(estimate, measured, text)
            self.assertLessEqual(estimate, measured * 1.12, text)
