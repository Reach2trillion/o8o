# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    checkinme_checkin_ids = fields.One2many('checkinme.checkin', 'partner_id', string='Sales Check-ins')
    checkinme_checkin_count = fields.Integer(compute='_compute_checkinme_stats', string='# Visits')
    checkinme_last_checkin_time = fields.Datetime(compute='_compute_checkinme_stats', string='Last Visit')

    def _compute_checkinme_stats(self):
        groups = self.env['checkinme.checkin']._read_group(
            [('partner_id', 'in', self.ids), ('state', 'in', ('checked_in', 'done'))],
            ['partner_id'], ['__count', 'checkin_time:max'])
        stats = {partner.id: (count, last) for partner, count, last in groups}
        for partner in self:
            count, last = stats.get(partner.id, (0, False))
            partner.checkinme_checkin_count = count
            partner.checkinme_last_checkin_time = last

    def action_view_checkinme_checkins(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'checkinme_sales_activity.action_checkinme_checkin')
        action['domain'] = [('partner_id', '=', self.id)]
        action['context'] = {'default_partner_id': self.id, 'search_default_filter_checked': 1}
        return action
