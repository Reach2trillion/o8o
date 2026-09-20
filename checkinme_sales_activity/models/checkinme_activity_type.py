# -*- coding: utf-8 -*-
from odoo import api, fields, models


class CheckinmeActivityType(models.Model):
    _name = 'checkinme.activity.type'
    _description = 'Sales Activity Type'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(help="Short technical code, e.g. VISIT, MEET, CALL.")
    sequence = fields.Integer(default=10)
    color = fields.Integer(default=0)
    icon = fields.Char(
        default='fa-map-marker',
        help="Font Awesome 4 icon class shown on mobile cards, e.g. fa-handshake-o, fa-phone.")
    counts_as_visit = fields.Boolean(
        string='Counts as Field Visit', default=True,
        help="Check-ins of this type count as field visits in KPI targets and performance reports.")
    requires_customer = fields.Boolean(
        string='Customer Required', default=True,
        help="A customer must be selected on check-ins of this type.")
    active = fields.Boolean(default=True)
    description = fields.Text(translate=True)
    checkin_count = fields.Integer(compute='_compute_checkin_count', string='# Check-ins')

    _sql_constraints = [
        ('code_uniq', 'unique(code)', 'The activity type code must be unique.'),
    ]

    def _compute_checkin_count(self):
        groups = self.env['checkinme.checkin']._read_group(
            [('checkin_type_id', 'in', self.ids)], ['checkin_type_id'], ['__count'])
        counts = {activity_type.id: count for activity_type, count in groups}
        for rec in self:
            rec.checkin_count = counts.get(rec.id, 0)

    def action_view_checkins(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'checkinme_sales_activity.action_checkinme_checkin')
        action['domain'] = [('checkin_type_id', '=', self.id)]
        action['context'] = {'default_checkin_type_id': self.id}
        return action
