# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ReportCheckinmeActivity(models.AbstractModel):
    _name = 'report.checkinme_sales_activity.report_checkinme_activity'
    _description = 'Sales Activity Report (PDF)'

    @api.model
    def _get_report_values(self, docids, data=None):
        data = dict(data or {})
        Checkin = self.env['checkinme.checkin']
        company = self.env['res.company'].browse(data.get('company_id')).exists() or self.env.company
        today = Checkin._get_today(company)
        date_from = fields.Date.to_date(data.get('date_from')) or today.replace(day=1)
        date_to = fields.Date.to_date(data.get('date_to')) or today
        employees = self.env['hr.employee'].browse(data.get('employee_ids') or [])
        summary = Checkin._get_performance_summary(
            date_from, date_to, employees=employees or None, company=company)
        wizards = self.env['checkinme.report.wizard'].browse(docids)
        return {
            'doc_ids': docids,
            'doc_model': 'checkinme.report.wizard',
            'docs': wizards,
            'data': data,
            'summary': summary,
            'company': company,
            'include_details': data.get('include_details', True),
            'period_label': self.env['checkinme.telegram']._format_period_label(date_from, date_to),
        }
