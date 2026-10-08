# -*- coding: utf-8 -*-
"""Shared fixtures for the Cambodia COD delivery label tests.

Every test class inherits :class:`CodLabelCommon`, based on ``AccountTestInvoicingCommon``: an
independent USD company with a chart of accounts and a warehouse, located in Cambodia, the KHR
currency active at 4061.32 riel per dollar, two goods products without taxes, a "Delivery"
carrier ($2 fixed price), a Cambodian customer in the province "ខេត្តកណ្តាល" and a published
payment provider in test mode (the payment QR needs one).

The standard order of the tests is $15.00 of goods + $2.00 of delivery = $17.00.
"""
from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon

KHR_RATE = 4061.32
FIXED_KHR_RATE = 4100.0

# Context used for every record creation to skip mail notifications / tracking.
MAIL_CONTEXT = {
    'tracking_disable': True,
    'mail_create_nolog': True,
    'mail_notrack': True,
    'no_reset_password': True,
}


class CodLabelCommon(AccountTestInvoicingCommon):

    @classmethod
    def get_default_groups(cls):
        return (super().get_default_groups()
                | cls.env.ref('sales_team.group_sale_manager')
                | cls.env.ref('stock.group_stock_manager'))

    @classmethod
    def setup_independent_company(cls, **kwargs):
        return super().setup_independent_company(currency_id=cls.env.ref('base.USD').id, **kwargs)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, **MAIL_CONTEXT, tz='Asia/Phnom_Penh'))
        cls.company = cls.env.company
        cls.usd = cls.env.ref('base.USD')
        cls.cambodia = cls.env.ref('base.kh')
        cls.khr = cls.setup_other_currency('KHR', rates=[('2000-01-01', KHR_RATE)])

        # --- company / label configuration --------------------------------
        cls.company.write({
            'country_id': cls.cambodia.id,
            'phone': '0312663333',
            'kh_label_khr_rate_source': 'odoo',
            'kh_label_khr_rate': FIXED_KHR_RATE,
            'kh_label_khr_rounding': 100,
            'kh_label_show_khr': True,
            'kh_label_qr_content': 'map',
            'kh_label_show_items': True,
            'kh_label_max_item_lines': 3,
            'kh_label_tagline': False,
            'kh_label_footer': 'អរគុណ! Thank you!',
            'kh_label_fragile_default': False,
            'kh_label_allow_check_default': True,
        })
        cls.env.user.tz = 'Asia/Phnom_Penh'

        # --- provinces -------------------------------------------------------
        State = cls.env['res.country.state']
        cls.kandal = State.search([('country_id', '=', cls.cambodia.id), ('name', '=', 'ខេត្តកណ្តាល')], limit=1) \
            or State.create({'name': 'ខេត្តកណ្តាល', 'code': 'KH-TEST-KDL', 'country_id': cls.cambodia.id})

        # --- products and carrier -------------------------------------------
        Product = cls.env['product.product']
        product_values = {
            'type': 'consu',
            'taxes_id': [Command.clear()],
            'supplier_taxes_id': [Command.clear()],
        }
        cls.serum = Product.create(dict(product_values, name='Serum 30ml', default_code='SER30', list_price=7.5))
        cls.cream = Product.create(dict(product_values, name='Night Cream', default_code='CRM', list_price=7.5))
        cls.lip = Product.create(dict(product_values, name='Lip Balm', list_price=2.0))
        cls.delivery_product = Product.create({
            'name': 'Delivery Fee (test)', 'type': 'service', 'list_price': 2.0,
            'taxes_id': [Command.clear()], 'supplier_taxes_id': [Command.clear()],
        })
        cls.carrier = cls.env['delivery.carrier'].create({
            'name': 'Delivery',
            'delivery_type': 'fixed',
            'fixed_price': 2.0,
            'product_id': cls.delivery_product.id,
        })

        # --- customers -------------------------------------------------------
        Partner = cls.env['res.partner']
        cls.customer = Partner.create({
            'name': 'Sok Dara',
            'phone': '+855 12 345 678',
            'street': 'ផ្ទះលេខ ១២៣ ផ្លូវជាតិលេខ២',
            'city': 'ក្រុងតាខ្មៅ',
            'state_id': cls.kandal.id,
            'country_id': cls.cambodia.id,
            'partner_latitude': 11.4833412,
            'partner_longitude': 104.9500127,
        })
        cls.messy_customer = Partner.create({'name': 'Andyyvathhh 095634706'})

        # --- an online payment provider the customer can pay with (the shop's "ABA KHQR") ----------
        # The "scan to pay" QR code is printed only when the payment page offers a provider:
        # enabled or in test mode, and published (customers do not see unpublished providers).
        payment_method = cls.env.ref('payment.payment_method_unknown')
        cls.provider = cls.env['payment.provider'].create({
            'name': 'Dummy (test)', 'code': 'none', 'state': 'test', 'is_published': True,
            'company_id': cls.company.id, 'payment_method_ids': [Command.set(payment_method.ids)],
        })
        payment_method.active = True

        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.picking_type_out = cls.warehouse.out_type_id
        cls.picking_type_in = cls.warehouse.in_type_id
        cls.customer_location = cls.env.ref('stock.stock_location_customers')
        cls.supplier_location = cls.env.ref('stock.stock_location_suppliers')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @classmethod
    def _create_order(cls, partner=None, lines=None, carrier=True, pricelist=None, confirm=True):
        """Sales order ($7.50 serum + $7.50 cream + $2.00 delivery = $17.00 by default)."""
        lines = lines if lines is not None else [(cls.serum, 1, 7.5), (cls.cream, 1, 7.5)]
        values = {
            'partner_id': (partner or cls.customer).id,
            'order_line': [Command.create({
                'product_id': product.id,
                'product_uom_qty': qty,
                'price_unit': price,
                'tax_id': [Command.clear()],
            }) for product, qty, price in lines],
        }
        if pricelist:
            values['pricelist_id'] = pricelist.id
        order = cls.env['sale.order'].create(values)
        if carrier:
            order.set_delivery_line(cls.carrier, carrier if isinstance(carrier, float) else cls.carrier.fixed_price)
        if confirm:
            order.action_confirm()
        return order

    @classmethod
    def _deliveries(cls, order):
        return order.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing')

    @classmethod
    def _create_invoice(cls, order, post=True):
        invoice = order._create_invoices()
        if post:
            invoice.action_post()
        return invoice

    @classmethod
    def _register_payment(cls, invoice, amount=None):
        values = {'payment_date': fields.Date.today()}
        if amount is not None:
            values['amount'] = amount
        return cls.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create(values)._create_payments()

    @classmethod
    def _create_picking(cls, picking_type, partner=None, product=None, qty=1.0):
        """Transfer without sales order (outgoing or incoming)."""
        product = product or cls.lip
        if picking_type.code == 'incoming':
            source, destination = cls.supplier_location, picking_type.default_location_dest_id
        else:
            source, destination = picking_type.default_location_src_id, cls.customer_location
        picking = cls.env['stock.picking'].create({
            'partner_id': (partner or cls.customer).id,
            'picking_type_id': picking_type.id,
            'location_id': source.id,
            'location_dest_id': destination.id,
            'move_ids': [Command.create({
                'name': product.name,
                'product_id': product.id,
                'product_uom_qty': qty,
                'product_uom': product.uom_id.id,
                'location_id': source.id,
                'location_dest_id': destination.id,
            })],
        })
        picking.action_confirm()
        return picking

    @classmethod
    def _validate_partially(cls, picking, quantities):
        """Validate ``picking`` with ``{product: qty}`` done quantities and create a backorder."""
        for move in picking.move_ids:
            move.quantity = quantities.get(move.product_id, 0.0)
            move.picked = True
        result = picking.button_validate()
        if isinstance(result, dict) and result.get('res_model') == 'stock.backorder.confirmation':
            cls.env['stock.backorder.confirmation'].with_context(**result['context']).create({}).process()
        return picking.backorder_ids

    @staticmethod
    def _labels(picking):
        return picking._kh_get_label_values()
