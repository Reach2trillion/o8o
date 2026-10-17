# -*- coding: utf-8 -*-
"""RPC helpers behind the CheckinMe phone screen (client action ``checkinme_sales_activity.screen``).

Every tap on the screen writes real records through the regular ORM methods, so access rights,
record rules, GPS verification and Telegram notifications behave exactly as in the backend views.
"""
import math
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang

from .checkinme_checkin import haversine_distance, OUTCOMES

NEARBY_RADIUS_M = 3000
RECENT_DAYS = 90


class CheckinmeCheckinMobile(models.Model):
    _inherit = 'checkinme.checkin'

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------
    def _mobile_vals(self):
        """Compact representation of a check-in for the phone screen."""
        self.ensure_one()
        status_labels = dict(self._fields['location_status']._description_selection(self.env))
        state_labels = dict(self._fields['state']._description_selection(self.env))
        outcome_labels = dict(self._fields['outcome']._description_selection(self.env))
        checkin_local = self._to_local(self.checkin_time)
        checkout_local = self._to_local(self.checkout_time)
        return {
            'id': self.id,
            'name': self.name,
            'state': self.state,
            'state_label': state_labels.get(self.state, ''),
            'partner_id': self.partner_id.id,
            'partner_name': self.partner_id.display_name or '',
            'partner_address': ' '.join((self.partner_id._display_address(without_company=True) or '').split()) if self.partner_id else '',
            'partner_phone': self.partner_id.phone or self.partner_id.mobile or '',
            'type_id': self.checkin_type_id.id,
            'type_name': self.checkin_type_id.name,
            'type_icon': self.checkin_type_id.icon or 'fa-map-marker',
            'purpose': self.purpose or '',
            'notes': self.notes or '',
            'checkin_time': fields.Datetime.to_string(self.checkin_time) if self.checkin_time else False,
            'checkin_time_local': checkin_local.strftime('%H:%M') if checkin_local else '',
            'checkin_date_local': checkin_local.strftime('%d %b %Y') if checkin_local else '',
            'checkout_time_local': checkout_local.strftime('%H:%M') if checkout_local else '',
            'duration': self.duration,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'accuracy': self.accuracy,
            'has_location': self.has_location,
            'google_maps_url': self.google_maps_url or '',
            'location_status': self.location_status,
            'location_status_label': status_labels.get(self.location_status, ''),
            'distance_to_partner': round(self.distance_to_partner or 0.0),
            'outcome': self.outcome or '',
            'outcome_label': outcome_labels.get(self.outcome, ''),
            'sale_amount': self.sale_amount,
            'next_action_date': fields.Date.to_string(self.next_action_date) if self.next_action_date else '',
            'is_new_customer': self.is_new_customer,
            'has_photo': bool(self.photo),
            'telegram_notified': self.telegram_notified,
            'telegram_pending': bool(self.telegram_pending_event),
        }

    @api.model
    def _mobile_partner_vals(self, partner, latitude=None, longitude=None, last_visits=None):
        distance = None
        if latitude and longitude and (partner.partner_latitude or partner.partner_longitude):
            distance = round(haversine_distance(latitude, longitude, partner.partner_latitude, partner.partner_longitude))
        last_visit = (last_visits or {}).get(partner.id)
        return {
            'id': partner.id,
            'name': partner.display_name,
            'address': ' '.join((partner._display_address(without_company=True) or '').split()),
            'phone': partner.phone or partner.mobile or '',
            'geolocated': bool(partner.partner_latitude or partner.partner_longitude),
            'distance_m': distance,
            'last_visit': fields.Date.to_string(last_visit) if last_visit else '',
        }

    @api.model
    def _mobile_target_vals(self, employee, today):
        target = self.env['checkinme.target'].search([
            ('employee_id', '=', employee.id),
            ('year', '=', today.year),
            ('month', '=', str(today.month)),
        ], limit=1)
        if not target:
            return False
        currency = target.currency_id
        return {
            'id': target.id,
            'name': target.name,
            'status': target.status,
            'status_label': dict(target._fields['status']._description_selection(self.env)).get(target.status, ''),
            'days_remaining': target.days_remaining,
            'expected_progress': target.expected_progress,
            'achievement_rate': target.achievement_rate,
            'visits': {'actual': target.actual_visits, 'target': target.target_visits, 'pct': target.achievement_visits},
            'new_customers': {'actual': target.actual_new_customers, 'target': target.target_new_customers,
                              'pct': target.achievement_new_customers},
            'orders': {'actual': target.actual_orders, 'target': target.target_orders, 'pct': target.achievement_orders},
            'sales': {
                'actual': target.actual_sales_amount, 'target': target.target_sales_amount,
                'pct': target.achievement_sales,
                'actual_display': formatLang(self.env, target.actual_sales_amount, currency_obj=currency),
                'target_display': formatLang(self.env, target.target_sales_amount, currency_obj=currency),
            },
        }

    # ------------------------------------------------------------------
    # RPC entry points
    # ------------------------------------------------------------------
    @api.model
    def mobile_get_dashboard(self):
        """Everything the phone home screen needs in one call."""
        employee = self.env.user.employee_id
        company = self.env.company
        today = self._get_today(company)
        config = self.env['checkinme.config']
        data = {
            'employee': {
                'id': employee.id,
                'name': employee.name or self.env.user.name,
                'has_employee': bool(employee),
            },
            'user_name': self.env.user.name,
            'today': fields.Date.to_string(today),
            'today_label': today.strftime('%A %d %B %Y'),
            'currency_symbol': company.currency_id.symbol or '',
            'settings': {
                'require_gps': config._get_bool('checkinme.require_gps', True),
                'max_distance_m': config._get_int('checkinme.max_distance_m', 500),
                'telegram_enabled': bool(self.env['checkinme.telegram']._get_bot_token()),
            },
            'types': [{
                'id': t.id, 'name': t.name, 'icon': t.icon or 'fa-map-marker', 'color': t.color,
                'requires_customer': t.requires_customer, 'counts_as_visit': t.counts_as_visit,
            } for t in self.env['checkinme.activity.type'].search([])],
            'outcomes': [{'value': value, 'label': label} for value, label in
                         self._fields['outcome']._description_selection(self.env)],
            'open_visit': False,
            'planned_today': [],
            'today_visits': [],
            'today_stats': {'checkins': 0, 'visits': 0, 'verified': 0, 'customers': 0},
            'target': False,
        }
        if not employee:
            return data
        base_domain = [('employee_id', '=', employee.id)]
        open_visit = self.search(base_domain + [('state', '=', 'checked_in')], order='checkin_time desc, id desc', limit=1)
        data['open_visit'] = open_visit._mobile_vals() if open_visit else False
        planned = self.search(base_domain + [('state', '=', 'draft'), ('checkin_date', '<=', today)],
                              order='checkin_time asc', limit=20)
        data['planned_today'] = [p._mobile_vals() for p in planned]
        done_today = self.search(base_domain + [('state', 'in', ('checked_in', 'done')), ('checkin_date', '=', today)],
                                 order='checkin_time desc, id desc')
        data['today_visits'] = [c._mobile_vals() for c in done_today]
        visits = done_today.filtered('counts_as_visit')
        data['today_stats'] = {
            'checkins': len(done_today),
            'visits': len(visits),
            'verified': len(visits.filtered(lambda c: c.location_status == 'verified')),
            'customers': len(done_today.partner_id),
        }
        data['target'] = self._mobile_target_vals(employee, today)
        return data

    @api.model
    def mobile_search_customers(self, query='', latitude=None, longitude=None, limit=20):
        """Customers for the picker: text matches, customers near the current position and the
        ones visited recently by this salesperson."""
        Partner = self.env['res.partner']
        employee = self.env.user.employee_id
        query = (query or '').strip()
        base = [('is_company', 'in', (True, False)), ('type', '=', 'contact'), ('active', '=', True)]
        result, seen = [], set()

        last_visits = {}
        recent_visits = self.search([
            ('employee_id', '=', employee.id), ('state', 'in', ('checked_in', 'done')),
            ('checkin_date', '>=', self._get_today() - timedelta(days=RECENT_DAYS)),
        ], order='checkin_time desc', limit=200) if employee else self.browse()
        for visit in recent_visits:
            last_visits.setdefault(visit.partner_id.id, visit.checkin_date)

        def add(partners, section):
            for partner in partners:
                if partner.id in seen:
                    continue
                seen.add(partner.id)
                vals = self._mobile_partner_vals(partner, latitude, longitude, last_visits)
                vals['section'] = section
                result.append(vals)

        if query:
            add(Partner.search(base + ['|', '|', ('name', 'ilike', query), ('phone', 'ilike', query),
                                       ('mobile', 'ilike', query)], limit=limit, order='name'), 'search')
            return result

        if latitude and longitude:
            # cheap bounding box first, then exact haversine ordering
            dlat = NEARBY_RADIUS_M / 111320.0
            dlng = NEARBY_RADIUS_M / 111320.0 / max(0.2, abs(math.cos(math.radians(latitude))))
            nearby = Partner.search(base + [
                ('partner_latitude', '>=', latitude - dlat), ('partner_latitude', '<=', latitude + dlat),
                ('partner_longitude', '>=', longitude - dlng), ('partner_longitude', '<=', longitude + dlng),
                '|', ('partner_latitude', '!=', 0), ('partner_longitude', '!=', 0),
            ], limit=100)
            nearby = nearby.sorted(key=lambda p: haversine_distance(latitude, longitude, p.partner_latitude, p.partner_longitude))
            nearby = nearby.filtered(lambda p: haversine_distance(latitude, longitude, p.partner_latitude, p.partner_longitude) <= NEARBY_RADIUS_M)
            add(nearby[:limit], 'nearby')
        recent_ids = list(last_visits.keys())[:limit]
        if recent_ids:
            recent = Partner.browse(recent_ids).exists()
            add(sorted(recent, key=lambda p: recent_ids.index(p.id)), 'recent')
        return result

    @api.model
    def mobile_create_customer(self, name, phone='', street='', city='', latitude=None, longitude=None):
        """Create a customer from the phone; the current GPS position becomes its geolocation."""
        name = (name or '').strip()
        if not name:
            raise UserError(_("Please enter the customer name."))
        vals = {
            'name': name,
            'is_company': True,
            'customer_rank': 1,
            'phone': (phone or '').strip() or False,
            'street': (street or '').strip() or False,
            'city': (city or '').strip() or False,
            'company_id': False,
        }
        if latitude and longitude:
            vals.update({
                'partner_latitude': latitude,
                'partner_longitude': longitude,
                'date_localization': fields.Date.context_today(self),
            })
        partner = self.env['res.partner'].create(vals)
        return self._mobile_partner_vals(partner, latitude, longitude)

    @api.model
    def mobile_checkin(self, vals):
        """Create a check-in from the phone screen and return its summary."""
        employee = self.env.user.employee_id
        if not employee:
            raise UserError(_("Your user is not linked to an employee. Ask your administrator to create your employee record."))
        values = {
            'employee_id': employee.id,
            'partner_id': vals.get('partner_id') or False,
            'checkin_type_id': vals.get('type_id') or self._default_activity_type_id().id,
            'purpose': (vals.get('purpose') or '').strip() or False,
            'notes': (vals.get('notes') or '').strip() or False,
            'latitude': float(vals.get('latitude') or 0.0),
            'longitude': float(vals.get('longitude') or 0.0),
            'accuracy': float(vals.get('accuracy') or 0.0),
            'state': 'checked_in',
        }
        if vals.get('photo'):
            values['photo'] = vals['photo']
        if vals.get('planned_id'):
            planned = self.browse(int(vals['planned_id'])).exists()
            if planned and planned.state == 'draft':
                planned.write({k: v for k, v in values.items() if k not in ('state', 'employee_id') and v})
                planned.action_check_in()
                return planned._mobile_vals()
        checkin = self.create(values)
        return checkin._mobile_vals()

    @api.model
    def mobile_checkout(self, checkin_id, vals):
        """Check out an open visit from the phone screen."""
        checkin = self.browse(int(checkin_id)).exists()
        if not checkin or checkin.state != 'checked_in':
            raise UserError(_("This visit is not checked in."))
        values = {}
        if vals.get('outcome') in dict(OUTCOMES):
            values['outcome'] = vals['outcome']
        if vals.get('notes') is not None:
            values['notes'] = (vals.get('notes') or '').strip() or False
        if vals.get('sale_amount') not in (None, ''):
            values['sale_amount'] = float(vals['sale_amount'] or 0.0)
        if vals.get('next_action_date'):
            values['next_action_date'] = vals['next_action_date']
        if vals.get('latitude') and vals.get('longitude'):
            values.update({
                'checkout_latitude': float(vals['latitude']),
                'checkout_longitude': float(vals['longitude']),
                'checkout_accuracy': float(vals.get('accuracy') or 0.0),
            })
        if values:
            checkin.write(values)
        checkin.action_check_out()
        return checkin._mobile_vals()

    @api.model
    def mobile_get_checkin(self, checkin_id):
        checkin = self.browse(int(checkin_id)).exists()
        if not checkin:
            raise UserError(_("This check-in no longer exists."))
        return checkin._mobile_vals()

    @api.model
    def mobile_get_history(self, limit=30):
        employee = self.env.user.employee_id
        if not employee:
            return []
        records = self.search([('employee_id', '=', employee.id), ('state', '!=', 'cancel')],
                              order='checkin_time desc, id desc', limit=limit)
        return [r._mobile_vals() for r in records]
