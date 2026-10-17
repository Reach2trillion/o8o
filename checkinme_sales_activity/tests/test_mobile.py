# -*- coding: utf-8 -*-
"""Tests of the RPC helpers behind the phone screen."""

from odoo.exceptions import UserError
from odoo.tests.common import new_test_user, tagged

from .common import CheckinmeCommon, MAIL_CONTEXT, TZ

CHECKIN_LAT, CHECKIN_LNG = 11.5565, 104.9283


@tagged('post_install', '-at_install')
class TestMobileScreen(CheckinmeCommon):

    def _rep_checkin_model(self):
        return self.env['checkinme.checkin'].with_user(self.rep).with_context(**MAIL_CONTEXT)

    def test_dashboard_structure_and_today(self):
        Checkin = self._rep_checkin_model()
        data = Checkin.mobile_get_dashboard()
        self.assertTrue(data['employee']['has_employee'])
        self.assertEqual(data['employee']['id'], self.rep_employee.id)
        self.assertTrue(data['types'])
        self.assertTrue(all({'id', 'name', 'icon', 'requires_customer'} <= set(t) for t in data['types']))
        self.assertEqual([o['value'] for o in data['outcomes']][:2], ['interested', 'order'])
        self.assertIn('require_gps', data['settings'])
        self.assertFalse(data['open_visit'])
        self.assertEqual(data['today_stats'], {'checkins': 0, 'visits': 0, 'verified': 0, 'customers': 0})
        self.assertFalse(data['target'])

        visit = self._create_checkin(self.rep, purpose='Price list')
        planned = self._create_checkin(self.rep, state='draft', partner_id=self.partner_nogeo.id)
        target = self._create_target(self.rep_employee, target_visits=10, target_sales_amount=500.0)
        data = Checkin.mobile_get_dashboard()
        self.assertEqual(data['open_visit']['id'], visit.id)
        self.assertEqual(data['open_visit']['location_status'], 'verified')
        self.assertEqual([p['id'] for p in data['planned_today']], [planned.id])
        self.assertEqual([v['id'] for v in data['today_visits']], [visit.id])
        self.assertEqual(data['today_stats'], {'checkins': 1, 'visits': 1, 'verified': 1, 'customers': 1})
        self.assertEqual(data['target']['id'], target.id)
        self.assertEqual(data['target']['visits'], {'actual': 1, 'target': 10, 'pct': 10.0})
        self.assertIn('actual_display', data['target']['sales'])

    def test_dashboard_without_employee(self):
        user = new_test_user(self.env, login='checkinme_test_noemp',
                             groups='base.group_user,checkinme_sales_activity.group_checkinme_user',
                             context=MAIL_CONTEXT, tz=TZ)
        data = self.env['checkinme.checkin'].with_user(user).mobile_get_dashboard()
        self.assertFalse(data['employee']['has_employee'])
        self.assertEqual(data['today_visits'], [])
        with self.assertRaises(UserError):
            self.env['checkinme.checkin'].with_user(user).mobile_checkin({'partner_id': self.partner_geo.id})

    def test_search_customers_nearby_recent_and_query(self):
        Checkin = self._rep_checkin_model()
        far = self.env['res.partner'].create({
            'name': 'Far Away Shop', 'is_company': True,
            'partner_latitude': CHECKIN_LAT + 0.5, 'partner_longitude': CHECKIN_LNG})
        close = self.env['res.partner'].create({
            'name': 'Zyxcorner Shop', 'is_company': True,
            'partner_latitude': CHECKIN_LAT + 0.004, 'partner_longitude': CHECKIN_LNG})
        # nearby: ordered by distance, far partners excluded, distance reported
        result = Checkin.mobile_search_customers('', CHECKIN_LAT, CHECKIN_LNG)
        nearby = [r for r in result if r['section'] == 'nearby']
        self.assertEqual([r['id'] for r in nearby][:2], [self.partner_geo.id, close.id])
        self.assertNotIn(far.id, [r['id'] for r in result])
        self.assertLess(nearby[0]['distance_m'], 50)
        self.assertGreater(nearby[1]['distance_m'], 400)
        # recent: visited customers without geolocation show up with the last visit date
        self._create_checkin(self.rep, partner_id=self.partner_nogeo.id)
        result = Checkin.mobile_search_customers('', CHECKIN_LAT, CHECKIN_LNG)
        recent = [r for r in result if r['section'] == 'recent']
        self.assertEqual([r['id'] for r in recent], [self.partner_nogeo.id])
        self.assertTrue(recent[0]['last_visit'])
        self.assertIsNone(recent[0]['distance_m'])
        # query: text search only
        result = Checkin.mobile_search_customers('zyxcorner')
        self.assertEqual([(r['id'], r['section']) for r in result], [(close.id, 'search')])

    def test_create_customer_from_position(self):
        Checkin = self._rep_checkin_model()
        vals = Checkin.mobile_create_customer('New Pharmacy', ' 012 345 678 ', 'Street 271', 'Phnom Penh',
                                              CHECKIN_LAT, CHECKIN_LNG)
        partner = self.env['res.partner'].browse(vals['id'])
        self.assertEqual(partner.name, 'New Pharmacy')
        self.assertEqual(partner.phone, '012 345 678')
        self.assertTrue(partner.is_company)
        self.assertAlmostEqual(partner.partner_latitude, CHECKIN_LAT, places=5)
        self.assertTrue(partner.date_localization)
        self.assertTrue(vals['geolocated'])
        self.assertEqual(vals['distance_m'], 0)
        with self.assertRaises(UserError):
            Checkin.mobile_create_customer('   ')

    def test_mobile_checkin_and_checkout_flow(self):
        Checkin = self._rep_checkin_model()
        result = Checkin.mobile_checkin({
            'partner_id': self.partner_geo.id, 'type_id': self.type_visit.id, 'purpose': ' Demo ',
            'notes': 'first contact', 'latitude': CHECKIN_LAT, 'longitude': CHECKIN_LNG, 'accuracy': 9,
        })
        checkin = self.env['checkinme.checkin'].browse(result['id'])
        self.assertEqual(checkin.state, 'checked_in')
        self.assertEqual(checkin.employee_id, self.rep_employee)
        self.assertEqual((checkin.purpose, checkin.notes), ('Demo', 'first contact'))
        self.assertEqual(result['location_status'], 'verified')
        self.assertLess(result['distance_to_partner'], 50)
        self.assertTrue(result['name'].startswith('CHK/'))
        # GPS is required by default
        with self.assertRaises(UserError):
            Checkin.mobile_checkin({'partner_id': self.partner_geo.id, 'type_id': self.type_visit.id})
        # check out with outcome, amount, follow-up and a new position
        result = Checkin.mobile_checkout(checkin.id, {
            'outcome': 'order', 'sale_amount': '150.5', 'notes': 'ordered 10 boxes',
            'next_action_date': '2026-12-01', 'latitude': CHECKIN_LAT + 0.001, 'longitude': CHECKIN_LNG, 'accuracy': 5,
        })
        self.assertEqual(result['state'], 'done')
        self.assertEqual(checkin.outcome, 'order')
        self.assertAlmostEqual(checkin.sale_amount, 150.5)
        self.assertEqual(str(checkin.next_action_date), '2026-12-01')
        self.assertAlmostEqual(checkin.checkout_latitude, CHECKIN_LAT + 0.001, places=5)
        self.assertTrue(checkin.checkout_time)
        with self.assertRaises(UserError):
            Checkin.mobile_checkout(checkin.id, {})

    def test_mobile_checkin_of_planned_visit(self):
        Checkin = self._rep_checkin_model()
        planned = self._create_checkin(self.rep, state='draft', partner_id=self.partner_geo.id,
                                       latitude=0.0, longitude=0.0, purpose='Planned by manager')
        result = Checkin.mobile_checkin({
            'planned_id': planned.id, 'partner_id': self.partner_geo.id, 'type_id': self.type_visit.id,
            'latitude': CHECKIN_LAT, 'longitude': CHECKIN_LNG, 'accuracy': 12, 'notes': 'arrived',
        })
        self.assertEqual(result['id'], planned.id)
        self.assertEqual(planned.state, 'checked_in')
        self.assertEqual(planned.purpose, 'Planned by manager')
        self.assertEqual(planned.notes, 'arrived')
        self.assertEqual(planned.location_status, 'verified')
        self.assertEqual(self.env['checkinme.checkin'].search_count([('employee_id', '=', self.rep_employee.id)]), 1)

    def test_history_and_get(self):
        Checkin = self._rep_checkin_model()
        first = self._create_checkin(self.rep)
        second = self._create_checkin(self.rep, partner_id=self.partner_nogeo.id)
        cancelled = self._create_checkin(self.rep)
        cancelled.action_cancel()
        history = Checkin.mobile_get_history()
        self.assertEqual([h['id'] for h in history], [second.id, first.id])
        self.assertEqual(Checkin.mobile_get_checkin(first.id)['partner_name'], self.partner_geo.display_name)
        with self.assertRaises(UserError):
            Checkin.mobile_get_checkin(999999)
        # other people's visits are invisible to a salesperson
        other = self._create_checkin(self.other_rep)
        self.assertNotIn(other.id, [h['id'] for h in Checkin.mobile_get_history()])
