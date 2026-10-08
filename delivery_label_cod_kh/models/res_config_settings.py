# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


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
    kh_label_no_fee_note = fields.Selection(related='company_id.kh_label_no_fee_note', readonly=False)
    kh_label_show_items = fields.Boolean(related='company_id.kh_label_show_items', readonly=False)
    kh_label_max_item_lines = fields.Integer(related='company_id.kh_label_max_item_lines', readonly=False)
    kh_label_tagline = fields.Char(related='company_id.kh_label_tagline', readonly=False)
    kh_label_footer = fields.Char(related='company_id.kh_label_footer', readonly=False)
    kh_label_fragile_default = fields.Boolean(related='company_id.kh_label_fragile_default', readonly=False)
    kh_label_allow_check_default = fields.Boolean(
        related='company_id.kh_label_allow_check_default', readonly=False)
    kh_label_pay_provider_warning = fields.Char(
        string='Payment Provider Warning', compute='_compute_kh_label_pay_qr_warnings')
    kh_label_khqr_image_warning = fields.Char(
        string='KHQR Image Warning', compute='_compute_kh_label_pay_qr_warnings')

    @api.depends('company_id', 'kh_label_pay_qr', 'kh_label_khqr_image')
    def _compute_kh_label_pay_qr_warnings(self):
        for settings in self:
            company = settings.company_id
            settings.kh_label_pay_provider_warning = (
                settings.kh_label_pay_qr == 'odoo_link' and not company._kh_has_payment_provider() and _(
                    "No online payment provider is enabled (or in test mode) and published: a payment link "
                    "would open a page where the customer cannot pay, so COD labels print the static ABA KHQR "
                    "image below when it is set, else no payment QR code. Enable one in Invoicing (or Website) "
                    "> Configuration > Payment Providers. A database copy made with 'Neutralize' has its "
                    "providers disabled."))
            found = True
            image = settings.with_context(bin_size=False).kh_label_khqr_image
            if image and settings.kh_label_pay_qr in ('khqr_image', 'odoo_link'):
                found = self.env['stock.picking']._kh_khqr_image(image)[2]
            settings.kh_label_khqr_image_warning = not found and _(
                "No QR code was found in this image: the whole image is printed 18 mm wide and its QR code "
                "may be too small to scan. Upload only the square QR part of the ABA KHQR (crop the card).")
