# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    """Inventory > Settings > COD Delivery Label.

    Every setting is a company field exposed through a related field, so boolean settings that
    default to True are stored as real booleans on the company (unticking them persists, unlike
    ``config_parameter`` booleans whose "False" is stored as "absent").
    """
    _inherit = 'res.config.settings'

    kh_label_khr_rate_source = fields.Selection(related='company_id.kh_label_khr_rate_source', readonly=False)
    kh_label_khr_rate = fields.Float(related='company_id.kh_label_khr_rate', readonly=False)
    kh_label_khr_rounding = fields.Integer(related='company_id.kh_label_khr_rounding', readonly=False)
    kh_label_show_khr = fields.Boolean(related='company_id.kh_label_show_khr', readonly=False)
    kh_label_qr_content = fields.Selection(related='company_id.kh_label_qr_content', readonly=False)
    kh_label_khqr_image = fields.Image(related='company_id.kh_label_khqr_image', readonly=False)
    kh_label_pay_qr = fields.Selection(related='company_id.kh_label_pay_qr', readonly=False)
    kh_label_pay_url_template = fields.Char(related='company_id.kh_label_pay_url_template', readonly=False)
    kh_label_show_items = fields.Boolean(related='company_id.kh_label_show_items', readonly=False)
    kh_label_max_item_lines = fields.Integer(related='company_id.kh_label_max_item_lines', readonly=False)
    kh_label_tagline = fields.Char(related='company_id.kh_label_tagline', readonly=False)
    kh_label_footer = fields.Char(related='company_id.kh_label_footer', readonly=False)
    kh_label_fragile_default = fields.Boolean(related='company_id.kh_label_fragile_default', readonly=False)
    kh_label_allow_check_default = fields.Boolean(
        related='company_id.kh_label_allow_check_default', readonly=False)
