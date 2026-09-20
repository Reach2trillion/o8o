# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from ..models.checkinme_checkin import REPORT_PERIODS


class CheckinmeReportWizard(models.TransientModel):
    _name = 'checkinme.report.wizard'
    _description = 'Sales Activity Report'

    period = fields.Selection(
        REPORT_PERIODS + [('custom', 'Custom Range')], string='Period', default='this_month', required=True)
    date_from = fields.Date(compute='_compute_dates', store=True, readonly=False, required=True, precompute=True)
    date_to = fields.Date(compute='_compute_dates', store=True, readonly=False, required=True, precompute=True)
    employee_ids = fields.Many2many(
        'hr.employee', string='Salespeople', help="Leave empty to include every salesperson.")
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, default=lambda self: self.env.company)
    include_details = fields.Boolean(string='Include Check-in Details', default=True)
    telegram_chat_id = fields.Char(
        string='Telegram Chat ID',
        default=lambda self: self.env['checkinme.telegram']._get_default_chat_id())

    @api.depends('period', 'company_id')
    def _compute_dates(self):
        Checkin = self.env['checkinme.checkin']
        for wizard in self:
            if wizard.period and wizard.period != 'custom':
                wizard.date_from, wizard.date_to = Checkin._get_period_dates(
                    wizard.period, today=Checkin._get_today(wizard.company_id))
            elif not wizard.date_from or not wizard.date_to:
                today = Checkin._get_today(wizard.company_id)
                wizard.date_from = wizard.date_from or today
                wizard.date_to = wizard.date_to or today

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from and wizard.date_to and wizard.date_from > wizard.date_to:
                raise ValidationError(_("The start date must be before the end date."))

    def _get_report_data(self):
        self.ensure_one()
        return {
            'date_from': fields.Date.to_string(self.date_from),
            'date_to': fields.Date.to_string(self.date_to),
            'employee_ids': self.employee_ids.ids,
            'company_id': self.company_id.id,
            'include_details': self.include_details,
        }

    def _get_checkin_domain(self):
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('checkin_date', '>=', self.date_from),
            ('checkin_date', '<=', self.date_to),
        ]
        if self.employee_ids:
            domain.append(('employee_id', 'in', self.employee_ids.ids))
        return domain

    def action_print_pdf(self):
        self.ensure_one()
        return self.env.ref('checkinme_sales_activity.action_report_checkinme_activity').report_action(
            self, data=self._get_report_data(), config=False)

    def action_send_telegram(self):
        self.ensure_one()
        if not self.env.user.has_group('checkinme_sales_activity.group_checkinme_manager'):
            raise UserError(_("Only CheckinMe managers can send reports to Telegram."))
        if not self.telegram_chat_id:
            raise UserError(_("Please enter the Telegram chat id to send the report to."))
        ok, _text = self.env['checkinme.telegram']._send_period_report(
            (self.date_from, self.date_to), chat_id=self.telegram_chat_id,
            employees=self.employee_ids or None, company=self.company_id, message_type='report')
        if not ok:
            raise UserError(_("The report could not be sent. Please check the Telegram settings and logs."))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Telegram'),
                'message': _('The activity report was sent to chat %s.', self.telegram_chat_id),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def action_view_checkins(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'checkinme_sales_activity.action_checkinme_checkin')
        action['domain'] = self._get_checkin_domain() + [('state', 'in', ('checked_in', 'done'))]
        action['context'] = {'search_default_group_employee': 1}
        return action

    def action_view_analysis(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'checkinme_sales_activity.action_checkinme_performance_report')
        domain = [
            ('company_id', '=', self.company_id.id),
            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
        ]
        if self.employee_ids:
            domain.append(('employee_id', 'in', self.employee_ids.ids))
        action['domain'] = domain
        return action
