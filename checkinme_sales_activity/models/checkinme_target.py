# -*- coding: utf-8 -*-
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools.misc import format_date

MONTHS = [
    ('1', 'January'), ('2', 'February'), ('3', 'March'), ('4', 'April'),
    ('5', 'May'), ('6', 'June'), ('7', 'July'), ('8', 'August'),
    ('9', 'September'), ('10', 'October'), ('11', 'November'), ('12', 'December'),
]

TARGET_STATUSES = [
    ('none', 'No Target'),
    ('behind', 'Behind'),
    ('at_risk', 'At Risk'),
    ('on_track', 'On Track'),
    ('achieved', 'Achieved'),
]


class CheckinmeTarget(models.Model):
    _name = 'checkinme.target'
    _description = 'Monthly Sales Target (KPI)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'year desc, month desc, employee_id'
    _check_company_auto = True

    @api.model
    def _default_year(self):
        return self.env['checkinme.checkin']._get_today().year

    @api.model
    def _default_month(self):
        return str(self.env['checkinme.checkin']._get_today().month)

    name = fields.Char(compute='_compute_name', store=True)
    employee_id = fields.Many2one(
        'hr.employee', string='Salesperson', required=True, index=True, tracking=True,
        check_company=True, ondelete='cascade')
    user_id = fields.Many2one(
        'res.users', string='Salesperson User', related='employee_id.user_id', store=True, index=True)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, index=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    year = fields.Integer(string='Year', required=True, default=_default_year, tracking=True)
    month = fields.Selection(MONTHS, string='Month', required=True, default=_default_month, tracking=True)
    date_from = fields.Date(compute='_compute_dates', store=True, index=True)
    date_to = fields.Date(compute='_compute_dates', store=True, index=True)
    active = fields.Boolean(default=True)
    note = fields.Text()

    # Targets
    target_visits = fields.Integer(string='Target Visits', tracking=True)
    target_new_customers = fields.Integer(string='Target New Customers', tracking=True)
    target_orders = fields.Integer(string='Target Orders', tracking=True)
    target_sales_amount = fields.Monetary(
        string='Target Sales Amount', currency_field='currency_id', tracking=True)

    # Actuals (real time)
    actual_checkins = fields.Integer(string='Check-ins', compute='_compute_actuals')
    actual_visits = fields.Integer(string='Visits', compute='_compute_actuals')
    actual_verified_visits = fields.Integer(string='Verified Visits', compute='_compute_actuals')
    actual_customers = fields.Integer(string='Customers Visited', compute='_compute_actuals')
    actual_new_customers = fields.Integer(string='New Customers', compute='_compute_actuals')
    actual_orders = fields.Integer(string='Orders', compute='_compute_actuals')
    actual_sales_amount = fields.Monetary(
        string='Sales Amount', compute='_compute_actuals', currency_field='currency_id')

    # Achievement (%)
    achievement_visits = fields.Float(string='Visits %', compute='_compute_achievements', aggregator='avg')
    achievement_new_customers = fields.Float(string='New Customers %', compute='_compute_achievements', aggregator='avg')
    achievement_orders = fields.Float(string='Orders %', compute='_compute_achievements', aggregator='avg')
    achievement_sales = fields.Float(string='Sales %', compute='_compute_achievements', aggregator='avg')
    achievement_rate = fields.Float(
        string='Overall %', compute='_compute_achievements', aggregator='avg',
        help="Average achievement over the KPIs that have a target defined.")
    expected_progress = fields.Float(
        string='Expected Progress %', compute='_compute_progress',
        help="Share of the month already elapsed.")
    days_remaining = fields.Integer(compute='_compute_progress')
    is_current = fields.Boolean(compute='_compute_progress', search='_search_is_current')
    status = fields.Selection(TARGET_STATUSES, compute='_compute_status', search='_search_status', string='Status')

    _sql_constraints = [
        ('employee_period_uniq', 'unique(employee_id, year, month, company_id)',
         'A target already exists for this salesperson and month.'),
    ]

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    @api.depends('year', 'month')
    def _compute_dates(self):
        for target in self:
            if target.year and target.month:
                start = date(target.year, int(target.month), 1)
                target.date_from = start
                target.date_to = start + relativedelta(months=1, days=-1)
            else:
                target.date_from = target.date_to = False

    @api.depends('employee_id.name', 'date_from')
    def _compute_name(self):
        for target in self:
            period = format_date(self.env, target.date_from, date_format='MMM yyyy') if target.date_from else ''
            target.name = "%s - %s" % (target.employee_id.name or _('Target'), period)

    @api.depends('employee_id', 'date_from', 'date_to', 'company_id')
    def _compute_actuals(self):
        Checkin = self.env['checkinme.checkin']
        for target in self:
            row = {}
            if target.employee_id and target.date_from and target.date_to:
                summary = Checkin._get_performance_summary(
                    target.date_from, target.date_to, employees=target.employee_id,
                    company=target.company_id, with_targets=False)
                row = summary['employees'][0] if summary['employees'] else {}
            target.actual_checkins = row.get('checkins', 0)
            target.actual_visits = row.get('visits', 0)
            target.actual_verified_visits = row.get('verified', 0)
            target.actual_customers = row.get('customers', 0)
            target.actual_new_customers = row.get('new_customers', 0)
            target.actual_orders = row.get('orders', 0)
            target.actual_sales_amount = row.get('sales_amount', 0.0)

    @staticmethod
    def _percentage(actual, target):
        if not target:
            return 0.0
        return round(100.0 * actual / target, 1)

    @api.depends('target_visits', 'actual_visits', 'target_new_customers', 'actual_new_customers',
                 'target_orders', 'actual_orders', 'target_sales_amount', 'actual_sales_amount')
    def _compute_achievements(self):
        for target in self:
            target.achievement_visits = self._percentage(target.actual_visits, target.target_visits)
            target.achievement_new_customers = self._percentage(
                target.actual_new_customers, target.target_new_customers)
            target.achievement_orders = self._percentage(target.actual_orders, target.target_orders)
            target.achievement_sales = self._percentage(target.actual_sales_amount, target.target_sales_amount)
            rates = [
                pct for pct, tgt in (
                    (target.achievement_visits, target.target_visits),
                    (target.achievement_new_customers, target.target_new_customers),
                    (target.achievement_orders, target.target_orders),
                    (target.achievement_sales, target.target_sales_amount),
                ) if tgt
            ]
            target.achievement_rate = round(sum(rates) / len(rates), 1) if rates else 0.0

    @api.depends('date_from', 'date_to')
    def _compute_progress(self):
        today = self.env['checkinme.checkin']._get_today()
        for target in self:
            if not (target.date_from and target.date_to):
                target.expected_progress = 0.0
                target.days_remaining = 0
                target.is_current = False
                continue
            total_days = (target.date_to - target.date_from).days + 1
            elapsed = min(max((today - target.date_from).days + 1, 0), total_days)
            target.expected_progress = round(100.0 * elapsed / total_days, 1)
            target.days_remaining = max((target.date_to - today).days, 0) if today <= target.date_to else 0
            target.is_current = target.date_from <= today <= target.date_to

    def _search_is_current(self, operator, value):
        today = self.env['checkinme.checkin']._get_today()
        if (operator == '=' and value) or (operator == '!=' and not value):
            return [('date_from', '<=', today), ('date_to', '>=', today)]
        return ['|', ('date_from', '>', today), ('date_to', '<', today)]

    def _search_status(self, operator, value):
        """Status is computed live (actuals + today); evaluate it on the candidate targets."""
        if operator not in ('=', '!=', 'in', 'not in'):
            raise ValueError(_("Unsupported operator %s for the status field.", operator))
        values = set(value if isinstance(value, (list, tuple, set)) else [value])
        targets = self.search([('active', 'in', (True, False))]) if self.env.context.get('active_test', True) is False \
            else self.search([])
        matching = targets.filtered(lambda t: t.status in values)
        if operator in ('=', 'in'):
            return [('id', 'in', matching.ids)]
        return [('id', 'not in', matching.ids)]

    @api.depends('achievement_rate', 'expected_progress', 'target_visits', 'target_new_customers',
                 'target_orders', 'target_sales_amount')
    def _compute_status(self):
        for target in self:
            has_target = any((target.target_visits, target.target_new_customers,
                              target.target_orders, target.target_sales_amount))
            if not has_target:
                target.status = 'none'
            elif target.achievement_rate >= 100.0:
                target.status = 'achieved'
            elif target.achievement_rate >= target.expected_progress * 0.9:
                target.status = 'on_track'
            elif target.achievement_rate >= target.expected_progress * 0.6:
                target.status = 'at_risk'
            else:
                target.status = 'behind'

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('year')
    def _check_year(self):
        for target in self:
            if not 2000 <= target.year <= 2100:
                raise ValidationError(_("Please enter a valid year (2000-2100)."))

    @api.constrains('target_visits', 'target_new_customers', 'target_orders', 'target_sales_amount')
    def _check_positive(self):
        for target in self:
            if (target.target_visits < 0 or target.target_new_customers < 0
                    or target.target_orders < 0 or target.target_sales_amount < 0):
                raise ValidationError(_("Targets cannot be negative."))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_view_checkins(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'checkinme_sales_activity.action_checkinme_checkin')
        action['domain'] = [
            ('employee_id', '=', self.employee_id.id),
            ('checkin_date', '>=', self.date_from),
            ('checkin_date', '<=', self.date_to),
            ('state', 'in', ('checked_in', 'done')),
        ]
        action['context'] = {'default_employee_id': self.employee_id.id, 'search_default_group_date': 1}
        return action

    def action_view_sale_orders(self):
        self.ensure_one()
        Checkin = self.env['checkinme.checkin']
        start, end = Checkin._get_utc_bounds(
            self.date_from, self.date_to, self.employee_id.tz or self.company_id.partner_id.tz)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Confirmed Sales Orders'),
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [
                ('company_id', '=', self.company_id.id),
                ('state', '=', 'sale'),
                ('user_id', '=', self.user_id.id),
                ('date_order', '>=', start),
                ('date_order', '<', end),
            ],
        }
