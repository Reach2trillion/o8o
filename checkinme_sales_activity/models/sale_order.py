# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    checkinme_checkin_id = fields.Many2one(
        'checkinme.checkin', string='Sales Check-in', index=True, copy=False, ondelete='set null',
        help="Field visit during which this order was taken.")
