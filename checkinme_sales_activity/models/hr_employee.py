# -*- coding: utf-8 -*-
from odoo import fields, models


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    checkinme_telegram_chat_id = fields.Char(
        string='Telegram Chat ID', groups='hr.group_hr_user',
        help="Personal Telegram chat id of this person. When this employee is the manager "
             "(or department manager) of a salesperson, check-in notifications are also sent here. "
             "Tip: message @userinfobot on Telegram to get your chat id.")
    checkinme_checkin_count = fields.Integer(compute='_compute_checkinme_counts', string='# Check-ins')
    checkinme_target_count = fields.Integer(compute='_compute_checkinme_counts', string='# Targets')

    def _compute_checkinme_counts(self):
        checkin_groups = self.env['checkinme.checkin']._read_group(
            [('employee_id', 'in', self.ids), ('state', '!=', 'cancel')], ['employee_id'], ['__count'])
        checkin_counts = {employee.id: count for employee, count in checkin_groups}
        target_groups = self.env['checkinme.target']._read_group(
            [('employee_id', 'in', self.ids)], ['employee_id'], ['__count'])
        target_counts = {employee.id: count for employee, count in target_groups}
        for employee in self:
            employee.checkinme_checkin_count = checkin_counts.get(employee.id, 0)
            employee.checkinme_target_count = target_counts.get(employee.id, 0)

    def action_view_checkinme_checkins(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'checkinme_sales_activity.action_checkinme_checkin')
        action['domain'] = [('employee_id', '=', self.id)]
        action['context'] = {'default_employee_id': self.id, 'search_default_filter_checked': 1}
        return action

    def action_view_checkinme_targets(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'checkinme_sales_activity.action_checkinme_target')
        action['domain'] = [('employee_id', '=', self.id)]
        action['context'] = {'default_employee_id': self.id}
        return action
