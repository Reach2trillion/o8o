# -*- coding: utf-8 -*-
import logging
import math
from collections import Counter
from datetime import date, datetime, time, timedelta

import pytz
from dateutil.relativedelta import relativedelta
from markupsafe import Markup, escape

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

GOOGLE_MAPS_URL = "https://www.google.com/maps?q=%s,%s"
EARTH_RADIUS_M = 6371000.0

CHECKIN_STATES = [
    ('draft', 'Planned'),
    ('checked_in', 'Checked In'),
    ('done', 'Checked Out'),
    ('cancel', 'Cancelled'),
]

LOCATION_STATUSES = [
    ('verified', 'Verified On Site'),
    ('far', 'Far From Customer'),
    ('unknown', 'Customer Not Geolocated'),
    ('no_gps', 'No GPS Data'),
]

OUTCOMES = [
    ('interested', 'Interested'),
    ('order', 'Order Taken'),
    ('follow_up', 'Follow-up Needed'),
    ('not_interested', 'Not Interested'),
    ('not_available', 'Customer Not Available'),
]

REPORT_PERIODS = [
    ('today', 'Today'),
    ('yesterday', 'Yesterday'),
    ('this_week', 'This Week'),
    ('last_week', 'Last Week'),
    ('this_month', 'This Month'),
    ('last_month', 'Last Month'),
    ('this_year', 'This Year'),
    ('last_year', 'Last Year'),
]


def haversine_distance(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def google_maps_url(lat, lng):
    return GOOGLE_MAPS_URL % (('%.7f' % lat).rstrip('0').rstrip('.'), ('%.7f' % lng).rstrip('0').rstrip('.'))


def safe_timezone(tz_name):
    try:
        return pytz.timezone(tz_name or 'UTC')
    except pytz.UnknownTimeZoneError:
        return pytz.utc


class CheckinmeCheckin(models.Model):
    _name = 'checkinme.checkin'
    _description = 'Sales Check-in'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'checkin_time desc, id desc'
    _check_company_auto = True

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------
    @api.model
    def _default_employee_id(self):
        return self.env.user.employee_id

    @api.model
    def _default_checkin_type_id(self):
        return self.env['checkinme.activity.type'].search([], limit=1)

    # ------------------------------------------------------------------
    # Fields
    # ------------------------------------------------------------------
    name = fields.Char(
        string='Reference', required=True, copy=False, readonly=True, index=True,
        default=lambda self: _('New'))
    employee_id = fields.Many2one(
        'hr.employee', string='Salesperson', required=True, index=True, tracking=True,
        check_company=True, default=_default_employee_id, ondelete='restrict')
    user_id = fields.Many2one(
        'res.users', string='Salesperson User', related='employee_id.user_id', store=True, index=True)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, index=True,
        default=lambda self: self.env.company)
    currency_id = fields.Many2one(related='company_id.currency_id')
    partner_id = fields.Many2one(
        'res.partner', string='Customer', index=True, tracking=True, check_company=True,
        ondelete='restrict')
    partner_phone = fields.Char(related='partner_id.phone', string='Customer Phone')
    partner_mobile = fields.Char(related='partner_id.mobile', string='Customer Mobile')
    partner_address = fields.Char(related='partner_id.contact_address', string='Customer Address')
    partner_latitude = fields.Float(related='partner_id.partner_latitude', string='Customer Latitude')
    partner_longitude = fields.Float(related='partner_id.partner_longitude', string='Customer Longitude')
    checkin_type_id = fields.Many2one(
        'checkinme.activity.type', string='Activity Type', required=True, index=True, tracking=True,
        default=_default_checkin_type_id, ondelete='restrict')
    counts_as_visit = fields.Boolean(related='checkin_type_id.counts_as_visit', store=True)
    color = fields.Integer(related='checkin_type_id.color')
    purpose = fields.Char(string='Purpose', tracking=True)
    state = fields.Selection(
        CHECKIN_STATES, string='Status', default='checked_in', required=True, index=True,
        tracking=True, copy=False)

    checkin_time = fields.Datetime(
        string='Check-in Time', required=True, default=fields.Datetime.now, index=True,
        tracking=True, copy=False)
    checkin_date = fields.Date(
        string='Check-in Date', compute='_compute_checkin_date', store=True, index=True,
        help="Check-in day in the salesperson's timezone (used for daily/weekly/monthly grouping).")
    checkout_time = fields.Datetime(string='Check-out Time', tracking=True, copy=False)
    duration = fields.Float(string='Duration (Hours)', compute='_compute_duration', store=True)

    latitude = fields.Float(string='Latitude', digits=(10, 7), copy=False)
    longitude = fields.Float(string='Longitude', digits=(10, 7), copy=False)
    accuracy = fields.Float(string='GPS Accuracy (m)', copy=False)
    checkout_latitude = fields.Float(string='Check-out Latitude', digits=(10, 7), copy=False)
    checkout_longitude = fields.Float(string='Check-out Longitude', digits=(10, 7), copy=False)
    checkout_accuracy = fields.Float(string='Check-out GPS Accuracy (m)', copy=False)
    has_location = fields.Boolean(string='Has GPS Location', compute='_compute_has_location', store=True)
    google_maps_url = fields.Char(string='Google Maps', compute='_compute_google_maps_url')
    checkout_google_maps_url = fields.Char(string='Check-out Google Maps', compute='_compute_google_maps_url')
    distance_to_partner = fields.Float(
        string='Distance to Customer (m)', compute='_compute_location_status', store=True, digits=(16, 1),
        help="Distance between the check-in position and the customer's geolocation.")
    location_status = fields.Selection(
        LOCATION_STATUSES, string='Location Check', compute='_compute_location_status', store=True,
        help="Verified: within the maximum distance configured in settings from the customer's geolocation.")

    notes = fields.Text(string='Meeting Notes')
    outcome = fields.Selection(OUTCOMES, string='Outcome', tracking=True)
    next_action_date = fields.Date(string='Follow-up Date', tracking=True)
    is_new_customer = fields.Boolean(
        string='New Customer', compute='_compute_is_new_customer', store=True, readonly=False,
        help="Automatically checked when this is the first check-in recorded for the customer.")
    sale_amount = fields.Monetary(
        string='Sales Amount', currency_field='currency_id', tracking=True,
        help="Value of sales closed during this visit (manual entry).")
    sale_order_ids = fields.One2many('sale.order', 'checkinme_checkin_id', string='Sales Orders')
    sale_order_count = fields.Integer(string='# Sales Orders', compute='_compute_sale_order_stats')
    order_amount = fields.Monetary(
        string='Confirmed Orders Amount', compute='_compute_sale_order_stats', currency_field='currency_id',
        help="Untaxed amount of confirmed sales orders linked to this check-in, in company currency.")
    photo = fields.Image(string='Photo', max_width=1920, max_height=1920, copy=False)
    telegram_notified = fields.Boolean(string='Telegram Notified', copy=False, readonly=True)

    # ------------------------------------------------------------------
    # Compute
    # ------------------------------------------------------------------
    def _get_tz_name(self):
        self.ensure_one()
        return self.employee_id.tz or self.company_id.partner_id.tz or self.env.user.tz or 'UTC'

    def _to_local(self, dt):
        """Convert a naive UTC datetime to the salesperson's local timezone (aware)."""
        self.ensure_one()
        if not dt:
            return False
        return pytz.utc.localize(dt).astimezone(safe_timezone(self._get_tz_name()))

    def get_local_datetime_str(self, field_name='checkin_time', fmt='%d %b %Y %H:%M'):
        """Public helper for QWeb reports: local formatted datetime of a datetime field."""
        self.ensure_one()
        local = self._to_local(self[field_name])
        return local.strftime(fmt) if local else ''

    @api.depends('checkin_time', 'employee_id.tz', 'company_id')
    def _compute_checkin_date(self):
        for rec in self:
            local = rec._to_local(rec.checkin_time)
            rec.checkin_date = local.date() if local else False

    @api.depends('checkin_time', 'checkout_time')
    def _compute_duration(self):
        for rec in self:
            if rec.checkin_time and rec.checkout_time and rec.checkout_time > rec.checkin_time:
                rec.duration = (rec.checkout_time - rec.checkin_time).total_seconds() / 3600.0
            else:
                rec.duration = 0.0

    @api.depends('latitude', 'longitude')
    def _compute_has_location(self):
        for rec in self:
            rec.has_location = bool(rec.latitude or rec.longitude)

    @api.depends('latitude', 'longitude', 'checkout_latitude', 'checkout_longitude')
    def _compute_google_maps_url(self):
        for rec in self:
            rec.google_maps_url = google_maps_url(rec.latitude, rec.longitude) \
                if (rec.latitude or rec.longitude) else False
            rec.checkout_google_maps_url = google_maps_url(rec.checkout_latitude, rec.checkout_longitude) \
                if (rec.checkout_latitude or rec.checkout_longitude) else False

    @api.depends('latitude', 'longitude', 'partner_id', 'partner_id.partner_latitude', 'partner_id.partner_longitude')
    def _compute_location_status(self):
        max_distance = self.env['checkinme.config']._get_int('checkinme.max_distance_m', 500)
        for rec in self:
            if not (rec.latitude or rec.longitude):
                rec.distance_to_partner = 0.0
                rec.location_status = 'no_gps'
                continue
            plat, plng = rec.partner_id.partner_latitude, rec.partner_id.partner_longitude
            if not rec.partner_id or not (plat or plng):
                rec.distance_to_partner = 0.0
                rec.location_status = 'unknown'
                continue
            distance = haversine_distance(rec.latitude, rec.longitude, plat, plng)
            rec.distance_to_partner = distance
            rec.location_status = 'verified' if distance <= max_distance else 'far'

    @api.depends('partner_id')
    def _compute_is_new_customer(self):
        for rec in self:
            if not rec.partner_id:
                rec.is_new_customer = False
                continue
            domain = [('partner_id', '=', rec.partner_id.id), ('state', '!=', 'cancel')]
            if rec._origin.id:
                domain.append(('id', '!=', rec._origin.id))
            rec.is_new_customer = not self.sudo().search_count(domain, limit=1)

    @api.depends('sale_order_ids.state', 'sale_order_ids.amount_untaxed', 'sale_order_ids.currency_id')
    def _compute_sale_order_stats(self):
        for rec in self:
            orders = rec.sudo().sale_order_ids
            confirmed = orders.filtered(lambda so: so.state == 'sale')
            rec.sale_order_count = len(orders)
            rec.order_amount = sum(
                so.currency_id._convert(
                    so.amount_untaxed, rec.currency_id, rec.company_id,
                    (so.date_order or fields.Datetime.now()).date())
                for so in confirmed)

    @api.depends('name', 'partner_id.name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s - %s" % (rec.name, rec.partner_id.name) if rec.partner_id else rec.name

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('latitude', 'longitude', 'checkout_latitude', 'checkout_longitude')
    def _check_coordinates(self):
        for rec in self:
            for lat, lng in ((rec.latitude, rec.longitude), (rec.checkout_latitude, rec.checkout_longitude)):
                if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
                    raise ValidationError(_(
                        "Invalid GPS coordinates: latitude must be within [-90, 90] "
                        "and longitude within [-180, 180]."))

    @api.constrains('checkin_time', 'checkout_time')
    def _check_times(self):
        for rec in self:
            if rec.checkout_time and rec.checkin_time and rec.checkout_time < rec.checkin_time:
                raise ValidationError(_("The check-out time cannot be earlier than the check-in time."))

    @api.constrains('partner_id', 'checkin_type_id', 'state')
    def _check_partner_required(self):
        for rec in self:
            if rec.state != 'cancel' and rec.checkin_type_id.requires_customer and not rec.partner_id:
                raise ValidationError(_(
                    "A customer is required for activity type '%s'.", rec.checkin_type_id.name))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('checkinme.checkin') or _('New')
            if vals.get('state', 'checked_in') == 'checked_in' and not vals.get('checkin_time'):
                vals['checkin_time'] = fields.Datetime.now()
        records = super().create(vals_list)
        checked_in = records.filtered(lambda r: r.state == 'checked_in')
        checked_in._check_gps_required()
        checked_in._on_checked_in()
        return records

    def unlink(self):
        if (any(rec.state in ('checked_in', 'done') for rec in self)
                and not self.env.user.has_group('checkinme_sales_activity.group_checkinme_manager')):
            raise UserError(_("Only managers can delete check-ins that have already been checked in."))
        return super().unlink()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_check_in(self):
        if any(rec.state != 'draft' for rec in self):
            raise UserError(_("Only planned visits can be checked in."))
        self._check_gps_required()
        self.write({'state': 'checked_in', 'checkin_time': fields.Datetime.now()})
        self._on_checked_in()
        return True

    def action_check_out(self):
        if any(rec.state != 'checked_in' for rec in self):
            raise UserError(_("Only checked-in visits can be checked out."))
        self.write({'state': 'done', 'checkout_time': fields.Datetime.now()})
        for rec in self:
            rec._post_location_message('checkout')
            rec._notify_telegram('checkout')
        return True

    def action_cancel(self):
        if any(rec.state == 'cancel' for rec in self):
            raise UserError(_("This check-in is already cancelled."))
        self.write({'state': 'cancel'})
        return True

    def action_reset_to_draft(self):
        if any(rec.state != 'cancel' for rec in self):
            raise UserError(_("Only cancelled check-ins can be reset to planned."))
        self.write({'state': 'draft', 'checkout_time': False})
        return True

    def action_open_google_maps(self):
        self.ensure_one()
        url = self.google_maps_url
        if not url:
            raise UserError(_("No GPS position has been captured for this check-in."))
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'new'}

    def action_open_checkout_google_maps(self):
        self.ensure_one()
        url = self.checkout_google_maps_url
        if not url:
            raise UserError(_("No GPS position has been captured for this check-out."))
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'new'}

    def action_create_quotation(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_("Please select a customer before creating a quotation."))
        return {
            'type': 'ir.actions.act_window',
            'name': _('New Quotation'),
            'res_model': 'sale.order',
            'view_mode': 'form',
            'target': 'current',
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_user_id': self.user_id.id or self.env.user.id,
                'default_company_id': self.company_id.id,
                'default_checkinme_checkin_id': self.id,
                'default_origin': self.name,
            },
        }

    def action_view_sale_orders(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sales Orders'),
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('checkinme_checkin_id', '=', self.id)],
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_checkinme_checkin_id': self.id,
            },
        }

    def action_send_telegram(self):
        """Manually (re)send the Telegram notification for these check-ins."""
        sent = 0
        for rec in self:
            event = 'checkout' if rec.state == 'done' else 'checkin'
            if rec._notify_telegram(event, force=True):
                sent += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Telegram'),
                'message': _('%(sent)s of %(total)s notification(s) sent.', sent=sent, total=len(self)),
                'type': 'success' if sent == len(self) else 'warning',
                'sticky': False,
            },
        }

    def action_recompute_location_status(self):
        """Recompute distance / verification after the customer was geolocated or the threshold changed."""
        self.env.add_to_compute(self._fields['distance_to_partner'], self)
        self.env.add_to_compute(self._fields['location_status'], self)
        self.flush_recordset(['distance_to_partner', 'location_status'])
        return True

    # ------------------------------------------------------------------
    # Business helpers
    # ------------------------------------------------------------------
    def _check_gps_required(self):
        if not self.env['checkinme.config']._get_bool('checkinme.require_gps', True):
            return
        if any(not rec.has_location for rec in self):
            raise UserError(_(
                "A GPS position is required to check in. Please allow location access on your "
                "device and capture your position before checking in."))

    def _on_checked_in(self):
        for rec in self:
            rec._post_location_message('checkin')
            rec._notify_telegram('checkin')

    def _get_location_status_label(self):
        self.ensure_one()
        return dict(self._fields['location_status']._description_selection(self.env)).get(
            self.location_status, '')

    def _post_location_message(self, event):
        self.ensure_one()
        if event == 'checkin':
            when, url = self._to_local(self.checkin_time), self.google_maps_url
            title = _("Checked in on %s", when.strftime('%d %b %Y %H:%M') if when else '')
        else:
            when, url = self._to_local(self.checkout_time), self.checkout_google_maps_url
            title = _("Checked out on %s", when.strftime('%d %b %Y %H:%M') if when else '')
        parts = [escape(title)]
        if url:
            parts.append(Markup('<a href="%s" target="_blank">%s</a>') % (url, _("Open in Google Maps")))
        if event == 'checkin':
            status = self._get_location_status_label()
            if self.location_status in ('verified', 'far'):
                status = _("%(status)s (%(distance)s m from customer)",
                           status=status, distance=int(round(self.distance_to_partner)))
            parts.append(escape(status))
        # _message_log: internal note without notifications; works for users without an email address
        self._message_log(body=Markup('<br/>').join(parts))

    def _notify_telegram(self, event, force=False):
        self.ensure_one()
        if not force:
            key = 'checkinme.notify_checkin' if event == 'checkin' else 'checkinme.notify_checkout'
            if not self.env['checkinme.config']._get_bool(key, True):
                return False
        try:
            ok = self.env['checkinme.telegram']._notify_checkin_event(self, event)
        except Exception:  # noqa: BLE001 - never block the salesperson because of Telegram
            _logger.exception("CheckinMe: Telegram notification failed for %s", self.name)
            ok = False
        if ok and not self.telegram_notified:
            self.write({'telegram_notified': True})
        return ok

    # ------------------------------------------------------------------
    # Period helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_today(self, company=None):
        company = company or self.env.company
        tz = safe_timezone(company.partner_id.tz or self.env.user.tz or 'UTC')
        return datetime.now(tz).date()

    @api.model
    def _get_period_dates(self, period, today=None):
        """Return (date_from, date_to) for a named report period (weeks start on Monday)."""
        today = today or self._get_today()
        if period == 'today':
            return today, today
        if period == 'yesterday':
            d = today - timedelta(days=1)
            return d, d
        if period == 'this_week':
            start = today - timedelta(days=today.weekday())
            return start, start + timedelta(days=6)
        if period == 'last_week':
            start = today - timedelta(days=today.weekday() + 7)
            return start, start + timedelta(days=6)
        if period == 'this_month':
            start = today.replace(day=1)
            return start, start + relativedelta(months=1, days=-1)
        if period == 'last_month':
            start = today.replace(day=1) - relativedelta(months=1)
            return start, start + relativedelta(months=1, days=-1)
        if period == 'this_year':
            return today.replace(month=1, day=1), today.replace(month=12, day=31)
        if period == 'last_year':
            return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
        raise ValueError("Unknown period: %s" % period)

    @api.model
    def _get_utc_bounds(self, date_from, date_to, tz_name):
        """Naive UTC datetimes [start, end) covering the local dates date_from..date_to."""
        tz = safe_timezone(tz_name)
        start = tz.localize(datetime.combine(date_from, time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        end = tz.localize(datetime.combine(date_to + timedelta(days=1), time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        return start, end

    @api.model
    def _get_performance_summary(self, date_from, date_to, employees=None, company=None, with_targets=True):
        """Per-employee activity and sales summary for a date range.

        Returns a dict with keys: date_from, date_to, company, currency, sales_source,
        employees (list of row dicts sorted by employee name), totals (dict), checkins (recordset).
        Row keys: employee, checkins, visits, verified, customers, new_customers, duration,
        checkin_sale_amount, order_count, order_amount, sales_amount, orders, by_type (list of
        (type name, count)), by_outcome (dict), target (record or empty), target_* / achievement_*.
        """
        company = company or self.env.company
        Checkin = self.with_company(company)
        domain = [
            ('company_id', '=', company.id),
            ('state', 'in', ('checked_in', 'done')),
            ('checkin_date', '>=', date_from),
            ('checkin_date', '<=', date_to),
        ]
        if employees:
            domain.append(('employee_id', 'in', employees.ids))
        checkins = Checkin.search(domain)

        Target = self.env['checkinme.target'].with_company(company)
        targets = Target.browse()
        single_month = (date_from.year, date_from.month) == (date_to.year, date_to.month)
        if with_targets and single_month:
            tdomain = [
                ('company_id', '=', company.id),
                ('year', '=', date_from.year),
                ('month', '=', str(date_from.month)),
            ]
            if employees:
                tdomain.append(('employee_id', 'in', employees.ids))
            targets = Target.search(tdomain)

        if employees is None:
            employees = checkins.employee_id | targets.employee_id
        # Mirror the record rules: non-managers only ever see themselves and their direct reports.
        if not self.env.user.has_group('checkinme_sales_activity.group_checkinme_manager'):
            user = self.env.user
            employees = employees.filtered(lambda e: e.user_id == user or e.parent_id.user_id == user)
        employees = employees.sorted(lambda e: e.name or '')

        sales_source = self.env['checkinme.config']._get_param('checkinme.sales_source', 'sale_order')
        SaleOrder = self.env['sale.order'].sudo()
        currency = company.currency_id
        rows = []
        for emp in employees:
            emp_checkins = checkins.filtered(lambda c: c.employee_id == emp)
            visits = emp_checkins.filtered('counts_as_visit')
            verified = visits.filtered(lambda c: c.location_status == 'verified')
            if sales_source == 'sale_order':
                orders = SaleOrder.browse()
                if emp.user_id:
                    start, end = self._get_utc_bounds(date_from, date_to, emp.tz or company.partner_id.tz)
                    orders = SaleOrder.search([
                        ('company_id', '=', company.id),
                        ('state', '=', 'sale'),
                        ('user_id', '=', emp.user_id.id),
                        ('date_order', '>=', start),
                        ('date_order', '<', end),
                    ])
                order_count = len(orders)
                order_amount = sum(
                    so.currency_id._convert(
                        so.amount_untaxed, currency, company,
                        (so.date_order or fields.Datetime.now()).date())
                    for so in orders)
            else:
                orders = SaleOrder.browse()
                order_count = len(emp_checkins.filtered(lambda c: c.outcome == 'order'))
                order_amount = sum(emp_checkins.mapped('sale_amount'))
            by_type = Counter(c.checkin_type_id.name for c in emp_checkins)
            by_outcome = Counter(c.outcome for c in emp_checkins if c.outcome)
            row = {
                'employee': emp,
                'checkins': len(emp_checkins),
                'visits': len(visits),
                'verified': len(verified),
                'customers': len(emp_checkins.partner_id),
                'new_customers': len(emp_checkins.filtered('is_new_customer')),
                'duration': sum(emp_checkins.mapped('duration')),
                'checkin_sale_amount': sum(emp_checkins.mapped('sale_amount')),
                'order_count': order_count,
                'order_amount': order_amount,
                'orders': order_count,
                'sales_amount': order_amount,
                'by_type': sorted(by_type.items(), key=lambda item: (-item[1], item[0])),
                'by_outcome': dict(by_outcome),
                'checkin_records': emp_checkins,
                'target': Target.browse(),
            }
            target = targets.filtered(lambda t: t.employee_id == emp)[:1]
            if target:
                row.update({
                    'target': target,
                    'target_visits': target.target_visits,
                    'target_new_customers': target.target_new_customers,
                    'target_orders': target.target_orders,
                    'target_sales_amount': target.target_sales_amount,
                    'achievement_visits': target.achievement_visits,
                    'achievement_new_customers': target.achievement_new_customers,
                    'achievement_orders': target.achievement_orders,
                    'achievement_sales': target.achievement_sales,
                    'achievement_rate': target.achievement_rate,
                    'target_status': target.status,
                })
            rows.append(row)

        totals = {
            'employees': len(rows),
            'checkins': sum(r['checkins'] for r in rows),
            'visits': sum(r['visits'] for r in rows),
            'verified': sum(r['verified'] for r in rows),
            'customers': len(checkins.filtered(lambda c: c.employee_id in employees).partner_id),
            'new_customers': sum(r['new_customers'] for r in rows),
            'duration': sum(r['duration'] for r in rows),
            'orders': sum(r['orders'] for r in rows),
            'sales_amount': sum(r['sales_amount'] for r in rows),
            'target_sales_amount': sum(r.get('target_sales_amount', 0.0) for r in rows),
            'target_visits': sum(r.get('target_visits', 0) for r in rows),
        }
        return {
            'date_from': date_from,
            'date_to': date_to,
            'company': company,
            'currency': currency,
            'sales_source': sales_source,
            'employees': rows,
            'totals': totals,
            'checkins': checkins.filtered(lambda c: c.employee_id in employees),
        }
