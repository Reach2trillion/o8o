# -*- coding: utf-8 -*-
from odoo.http import request

from odoo.addons.sale.controllers.portal import PaymentPortal


class CodLabelPaymentPortal(PaymentPortal):
    """Payment page of a sales order opened from a payment link: never more than what is due.

    The "scan to pay" QR code of a COD label is a standard sale payment link
    (``/payment/pay?amount=..&sale_order_id=..&access_token=..``). Odoo only checks the signed
    amount of such a link, so the QR code of a label stays payable after the order is paid:
    scanning an old label again, or the label of another parcel of the same shipment, would charge
    the customer twice. The amount of the payment form is capped at the amount still due on the
    order (same rule as the label: total minus the paid part of the posted invoices and the online
    payments); when nothing is due, the page says there is nothing to pay. Links for less than
    the amount due (e.g. a deposit) are not changed.
    """

    def _get_extra_payment_form_values(self, sale_order_id=None, access_token=None, **kwargs):
        form_values = super()._get_extra_payment_form_values(
            sale_order_id=sale_order_id, access_token=access_token, **kwargs
        )
        order_id = self._cast_as_int(sale_order_id)
        amount = form_values.get('amount', kwargs.get('amount'))
        if order_id and amount:
            order_sudo = request.env['sale.order'].sudo().browse(order_id).exists()
            if order_sudo:
                due = request.env['stock.picking'].sudo()._kh_sale_order_due(order_sudo)
                if order_sudo.currency_id.compare_amounts(amount, due) > 0:
                    form_values['amount'] = due
        return form_values
