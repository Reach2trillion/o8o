# -*- coding: utf-8 -*-
from odoo import fields, models

from .checkinme_checkin import CHECKIN_STATES, LOCATION_STATUSES, OUTCOMES


class CheckinmePerformanceReport(models.Model):
    """SQL view combining check-ins (visits) and confirmed sales orders per salesperson and day,
    so pivot / graph views can report daily, weekly, monthly and yearly results per employee."""
    _name = 'checkinme.performance.report'
    _description = 'Sales Performance Analysis'
    _auto = False
    _order = 'date desc, id desc'

    record_type = fields.Selection([('checkin', 'Check-in'), ('sale', 'Sales Order')], readonly=True)
    date = fields.Date(string='Date', readonly=True)
    employee_id = fields.Many2one('hr.employee', string='Salesperson', readonly=True)
    user_id = fields.Many2one('res.users', string='Salesperson User', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)
    currency_id = fields.Many2one('res.currency', string='Currency', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    checkin_type_id = fields.Many2one('checkinme.activity.type', string='Activity Type', readonly=True)
    checkin_id = fields.Many2one('checkinme.checkin', string='Check-in', readonly=True)
    sale_order_id = fields.Many2one('sale.order', string='Sales Order', readonly=True)
    state = fields.Selection(CHECKIN_STATES, string='Check-in Status', readonly=True)
    location_status = fields.Selection(LOCATION_STATUSES, string='Location Check', readonly=True)
    outcome = fields.Selection(OUTCOMES, string='Outcome', readonly=True)

    checkin_count = fields.Integer(string='# Check-ins', readonly=True, aggregator='sum')
    visit_count = fields.Integer(string='# Visits', readonly=True, aggregator='sum')
    verified_count = fields.Integer(string='# Verified Visits', readonly=True, aggregator='sum')
    new_customer_count = fields.Integer(string='# New Customers', readonly=True, aggregator='sum')
    duration = fields.Float(string='Visit Duration (Hours)', readonly=True, aggregator='sum')
    checkin_sale_amount = fields.Monetary(
        string='Check-in Sales Amount', currency_field='currency_id', readonly=True, aggregator='sum')
    order_count = fields.Integer(string='# Orders', readonly=True, aggregator='sum')
    order_amount = fields.Monetary(
        string='Orders Amount (Untaxed)', currency_field='currency_id', readonly=True, aggregator='sum')

    @property
    def _table_query(self):
        """Inlined at search time (no database view), so no dependency on table creation order."""
        return """
            SELECT
                c.id * 2 AS id,
                'checkin'::varchar AS record_type,
                c.checkin_date AS date,
                c.employee_id AS employee_id,
                c.user_id AS user_id,
                c.company_id AS company_id,
                comp.currency_id AS currency_id,
                c.partner_id AS partner_id,
                c.checkin_type_id AS checkin_type_id,
                c.id AS checkin_id,
                NULL::integer AS sale_order_id,
                c.state AS state,
                c.location_status AS location_status,
                c.outcome AS outcome,
                1 AS checkin_count,
                CASE WHEN COALESCE(t.counts_as_visit, FALSE) THEN 1 ELSE 0 END AS visit_count,
                CASE WHEN c.location_status = 'verified' AND COALESCE(t.counts_as_visit, FALSE) THEN 1 ELSE 0 END AS verified_count,
                CASE WHEN COALESCE(c.is_new_customer, FALSE) THEN 1 ELSE 0 END AS new_customer_count,
                COALESCE(c.duration, 0.0) AS duration,
                COALESCE(c.sale_amount, 0.0) AS checkin_sale_amount,
                0 AS order_count,
                0.0 AS order_amount
            FROM checkinme_checkin c
            JOIN res_company comp ON comp.id = c.company_id
            LEFT JOIN checkinme_activity_type t ON t.id = c.checkin_type_id
            WHERE c.state IN ('checked_in', 'done')

            UNION ALL

            SELECT
                so.id * 2 + 1 AS id,
                'sale'::varchar AS record_type,
                (so.date_order AT TIME ZONE 'UTC' AT TIME ZONE COALESCE(rr.tz, 'UTC'))::date AS date,
                e.id AS employee_id,
                so.user_id AS user_id,
                so.company_id AS company_id,
                comp.currency_id AS currency_id,
                so.partner_id AS partner_id,
                NULL::integer AS checkin_type_id,
                so.checkinme_checkin_id AS checkin_id,
                so.id AS sale_order_id,
                NULL::varchar AS state,
                NULL::varchar AS location_status,
                NULL::varchar AS outcome,
                0 AS checkin_count,
                0 AS visit_count,
                0 AS verified_count,
                0 AS new_customer_count,
                0.0 AS duration,
                0.0 AS checkin_sale_amount,
                1 AS order_count,
                CASE WHEN COALESCE(so.currency_rate, 0) = 0 THEN so.amount_untaxed
                     ELSE so.amount_untaxed / so.currency_rate END AS order_amount
            FROM sale_order so
            JOIN res_company comp ON comp.id = so.company_id
            JOIN hr_employee e ON e.user_id = so.user_id AND e.company_id = so.company_id AND e.active = TRUE
            LEFT JOIN resource_resource rr ON rr.id = e.resource_id
            WHERE so.state = 'sale' AND so.user_id IS NOT NULL
        """
