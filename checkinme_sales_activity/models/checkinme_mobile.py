# -*- coding: utf-8 -*-
"""RPC helpers behind the Sale Check phone screen (client action ``checkinme_sales_activity.screen``).

Every tap on the screen writes real records through the regular ORM methods, so access rights,
record rules, GPS verification and Telegram notifications behave exactly as in the backend views.
"""
import math
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang, format_date

from .checkinme_checkin import haversine_distance, OUTCOMES

NEARBY_RADIUS_M = 3000
RECENT_DAYS = 90
APP_NAME = 'ABJ'
QR_SIZE = 180


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
            'employee_id': self.employee_id.id,
            'employee_name': self.employee_id.name or '',
            # the phone only offers check-in / check-out on the user's own visits
            'is_mine': self._is_mine(),
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
            'checkin_date_local': self._format_local(self.checkin_time, 'dd MMM y'),
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
            'sale_orders': self._mobile_sale_orders_vals(),
        }

    def _is_mine(self):
        """True when the visit belongs to the current user (same rule as the 'own records' rule)."""
        self.ensure_one()
        return bool(self.employee_id) and self.employee_id.user_id == self.env.user

    def _mobile_sale_orders_vals(self):
        """Sales orders taken during the visit that the user may read (for the "Print" buttons)."""
        self.ensure_one()
        if not self.env['sale.order'].has_access('read'):
            return []
        state_labels = dict(self.env['sale.order']._fields['state']._description_selection(self.env))
        return [{
            'id': order.id,
            'name': order.name,
            'state_label': state_labels.get(order.state, ''),
            'amount_total_display': formatLang(self.env, order.amount_total, currency_obj=order.currency_id),
        } for order in self.sale_order_ids.filtered(lambda o: o.state != 'cancel')._filtered_access('read')]

    @api.model
    def _mobile_app_vals(self):
        """ABJ app information: push readiness and the company letterhead (receipts, about)."""
        company = self.env.company.sudo()
        return {
            'name': APP_NAME,
            'push': {
                # mail (a dependency) creates the VAPID keys when the first browser subscribes
                'web_push_ready': True,
                'native_ready': self.env['checkinme.push']._is_native_ready(),
            },
            'company': {
                'name': company.name or '',
                'phone': company.phone or '',
                'email': company.email or '',
                'website': company.website or '',
                'vat': company.vat or '',
                'street': company.street or '',
                'city': company.city or '',
            },
            'server_version': '18.0',
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
            'today_label': format_date(self.env, today, date_format='EEEE dd MMMM y'),
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
            'followups_today': [],
            'today_visits': [],
            'today_stats': {'checkins': 0, 'visits': 0, 'verified': 0, 'customers': 0},
            'target': False,
            'app': self._mobile_app_vals(),
        }
        if not employee:
            return data
        base_domain = [('employee_id', '=', employee.id)]
        open_visit = self.search(base_domain + [('state', '=', 'checked_in')], order='checkin_time desc, id desc', limit=1)
        data['open_visit'] = open_visit._mobile_vals() if open_visit else False
        planned = self.search(base_domain + [('state', '=', 'draft'), ('checkin_date', '<=', today)],
                              order='checkin_time asc', limit=20)
        data['planned_today'] = [p._mobile_vals() for p in planned]
        # customers to call back today (same rule as the morning follow-up reminder)
        followups = self.search(base_domain + [('state', '=', 'done'), ('next_action_date', '=', today)],
                                order='id', limit=20)
        data['followups_today'] = [f._mobile_vals() for f in followups]
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
            'checkin_type_id': vals.get('type_id') or self._default_checkin_type_id().id,
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
            if planned and not planned._is_mine():
                raise UserError(_("You can only check in or out your own visits."))
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
        if checkin and not checkin._is_mine():
            raise UserError(_("You can only check in or out your own visits."))
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

    @api.model
    def mobile_send_test_push(self, endpoint=None, token=None):
        """"Send test notification" of the phone screen settings: push to the current user's
        devices now, or only to this phone given its browser push ``endpoint`` or its Android app
        FCM ``token``. Returns ``{'web', 'native', 'state', 'message', 'this_device'}``."""
        return self.env['checkinme.push']._send_test_notification(self.env.user, endpoint=endpoint, token=token)

    # ------------------------------------------------------------------
    # Receipts (printed by the phone on a Bluetooth / network receipt printer)
    # ------------------------------------------------------------------
    @api.model
    def mobile_get_receipt(self, checkin_id, kind='visit', order_id=None):
        """Receipt document of a visit (``kind='visit'``) or of a sales order taken during the
        visit (``kind='order'``). The phone renders it to a bitmap (ESC/POS text mode cannot
        shape Khmer), so every string is final here: translated and formatted.

        :return: ``{'title', 'name', 'blocks': [...]}``; block types: text, separator, pair,
            item, total, qr, feed
        """
        checkin = self.browse(int(checkin_id)).exists()
        if not checkin:
            raise UserError(_("This check-in no longer exists."))
        checkin.check_access('read')
        if kind == 'order':
            order = self.env['sale.order'].browse(int(order_id or 0)).exists()
            if not order or order.sudo().checkinme_checkin_id != checkin:
                raise UserError(_("This sales order is not linked to the visit %s.", checkin.name))
            order.check_access('read')
            if order.state == 'cancel':
                raise UserError(_("The sales order %s is cancelled.", order.name))
            return checkin._receipt_order(order)
        if kind != 'visit':
            raise UserError(_("Unknown receipt type: %s", kind))
        return checkin._receipt_visit()

    def _receipt_header(self):
        """Company letterhead blocks."""
        company = self.company_id.sudo()
        blocks = [{'type': 'text', 'text': company.name or '', 'align': 'center', 'size': 'l', 'bold': True}]
        address = ', '.join(filter(None, [company.street, company.street2, company.city]))
        contact = ' · '.join(filter(None, [company.phone, company.email]))
        for line in (address, contact, company.website):
            if line:
                blocks.append({'type': 'text', 'text': line, 'align': 'center', 'size': 's'})
        if company.vat:
            blocks.append({'type': 'text', 'text': _("Tax ID: %s", company.vat), 'align': 'center', 'size': 's'})
        blocks.append({'type': 'separator'})
        return blocks

    def _receipt_footer(self):
        now = self._format_local(fields.Datetime.now())
        return [
            {'type': 'separator'},
            {'type': 'text', 'text': _("Printed %s", now), 'align': 'center', 'size': 's'},
            {'type': 'feed', 'lines': 3},
        ]

    @staticmethod
    def _receipt_pair(left, right, bold=False):
        return {'type': 'pair', 'left': left, 'right': right or '', 'bold': bold}

    def _receipt_visit(self):
        """Visit slip: where, when, who and what was agreed."""
        self.ensure_one()
        checkin_local = self._to_local(self.checkin_time)
        checkout_local = self._to_local(self.checkout_time)
        partner = self.partner_id
        pair = self._receipt_pair
        blocks = self._receipt_header()
        blocks += [
            {'type': 'text', 'text': _("VISIT SLIP"), 'align': 'center', 'size': 'm', 'bold': True},
            {'type': 'text', 'text': self.name, 'align': 'center', 'size': 'm'},
            {'type': 'separator'},
        ]
        if checkin_local:
            blocks.append(pair(_("Date"), self._format_local(self.checkin_time, 'dd MMM y')))
            blocks.append(pair(_("Check-in"), checkin_local.strftime('%H:%M')))
        if checkout_local:
            blocks.append(pair(_("Check-out"), checkout_local.strftime('%H:%M')))
        if self.duration:
            hours, minutes = divmod(int(round(self.duration * 60)), 60)
            blocks.append(pair(_("Duration"), "%dh %02dm" % (hours, minutes)))
        blocks.append(pair(_("Salesperson"), self.employee_id.name))
        blocks.append({'type': 'separator'})
        if partner:
            blocks.append(pair(_("Customer"), partner.display_name, bold=True))
            phone = partner.phone or partner.mobile
            if phone:
                blocks.append(pair(_("Phone"), phone))
            address = ' '.join((partner._display_address(without_company=True) or '').split())
            if address:
                blocks.append({'type': 'text', 'text': address, 'size': 's'})
        blocks.append(pair(_("Activity"), self.checkin_type_id.name))
        status = self._get_location_status_label()
        if self.location_status in ('verified', 'far'):
            status = "%s (%s)" % (status, self.env['checkinme.telegram']._format_distance(self.distance_to_partner))
        if status:
            blocks.append(pair(_("Location"), status))
        if self.outcome:
            outcome_labels = dict(self._fields['outcome']._description_selection(self.env))
            blocks.append(pair(_("Outcome"), outcome_labels.get(self.outcome, '')))
        if self.sale_amount:
            blocks.append(pair(_("Sales"), formatLang(self.env, self.sale_amount, currency_obj=self.currency_id),
                               bold=True))
        if self.next_action_date:
            blocks.append(pair(_("Follow-up"), format_date(self.env, self.next_action_date)))
        for label, text in ((_("Purpose"), self.purpose), (_("Notes"), self.notes)):
            if text and text.strip():
                blocks.append({'type': 'separator'})
                blocks.append({'type': 'text', 'text': label, 'bold': True})
                blocks.append({'type': 'text', 'text': text.strip()})
        if self.google_maps_url:
            blocks.append({'type': 'separator'})
            blocks.append({'type': 'qr', 'value': self.google_maps_url, 'size': QR_SIZE})
            blocks.append({'type': 'text', 'text': _("Scan to open the location"), 'align': 'center', 'size': 's'})
        blocks += self._receipt_footer()
        return {'title': _("VISIT SLIP"), 'name': self.name, 'blocks': blocks}

    def _receipt_order(self, order):
        """Quotation / sales order receipt: one item per product line, then the totals."""
        self.ensure_one()
        currency = order.currency_id
        pair = self._receipt_pair

        precision = self.env['decimal.precision'].precision_get

        def amount(value, dp=None):
            return formatLang(self.env, value, dp=dp, currency_obj=currency)

        def number(value, dp):
            return formatLang(self.env, value, digits=0 if float(value).is_integer() else precision(dp))

        title = _("QUOTATION") if order.state in ('draft', 'sent') else _("SALES ORDER")
        blocks = self._receipt_header()
        blocks += [
            {'type': 'text', 'text': title, 'align': 'center', 'size': 'm', 'bold': True},
            {'type': 'text', 'text': order.name, 'align': 'center', 'size': 'm'},
            {'type': 'separator'},
        ]
        if order.date_order:
            blocks.append(pair(_("Date"), self._format_local(order.date_order)))
        blocks.append(pair(_("Customer"), order.partner_id.display_name, bold=True))
        if order.user_id:
            blocks.append(pair(_("Salesperson"), order.user_id.name))
        blocks.append(pair(_("Visit"), self.name))
        blocks.append({'type': 'separator'})
        for line in order.order_line:
            if line.display_type == 'line_section':
                blocks.append({'type': 'text', 'text': line.name or '', 'bold': True})
            elif line.display_type:
                blocks.append({'type': 'text', 'text': line.name or '', 'size': 's'})
            else:
                quantity = ' '.join(filter(None, [number(line.product_uom_qty, 'Product Unit of Measure'),
                                                  line.product_uom.name]))
                detail = "%s × %s" % (quantity, amount(line.price_unit, dp='Product Price'))
                if line.discount:
                    detail += " (-%s%%)" % number(line.discount, 'Discount')
                blocks.append({
                    'type': 'item',
                    'name': line.name or line.product_id.display_name or '',
                    'detail': detail,
                    'amount': amount(line.price_subtotal),
                })
        blocks.append({'type': 'separator'})
        blocks.append(pair(_("Untaxed Amount"), amount(order.amount_untaxed)))
        blocks.append(pair(_("Taxes"), amount(order.amount_tax)))
        blocks.append({'type': 'total', 'left': _("Total"), 'right': amount(order.amount_total)})
        if order.payment_term_id:
            blocks.append({'type': 'text', 'text': _("Payment terms: %s", order.payment_term_id.name), 'size': 's'})
        blocks += self._receipt_footer()
        return {'title': title, 'name': order.name, 'blocks': blocks}
