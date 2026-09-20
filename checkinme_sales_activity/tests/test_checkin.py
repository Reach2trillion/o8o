# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import tagged

from .common import CheckinmeCommon, CHECKIN_LAT, CHECKIN_LNG


@tagged('post_install', '-at_install')
class TestCheckin(CheckinmeCommon):

    def setUp(self):
        super().setUp()
        # No token is configured, so nothing can be sent; patch anyway so no HTTP can ever leave.
        self.telegram = self._patch_telegram()

    # ------------------------------------------------------------------
    # Creation & defaults
    # ------------------------------------------------------------------
    def test_sequence_and_defaults(self):
        checkin = self._create_checkin(self.rep)
        self.assertTrue(checkin.name.startswith('CHK/'), checkin.name)
        self.assertEqual(checkin.state, 'checked_in')
        self.assertTrue(checkin.checkin_time)
        self.assertFalse(checkin.checkout_time)
        self.assertEqual(checkin.employee_id, self.rep_employee)
        self.assertEqual(checkin.user_id, self.rep)
        self.assertEqual(checkin.company_id, self.company)
        self.assertEqual(checkin.currency_id, self.company.currency_id)
        self.assertTrue(checkin.has_location)
        self.assertTrue(checkin.counts_as_visit)
        self.assertEqual(checkin.display_name, '%s - %s' % (checkin.name, self.partner_geo.name))
        # Two check-ins never share a reference
        other = self._create_checkin(self.rep)
        self.assertNotEqual(other.name, checkin.name)

    def test_checkin_date_in_employee_timezone(self):
        # 18:30 UTC on the 19th is already the 20th in Asia/Phnom_Penh (UTC+7)
        checkin = self._create_checkin(self.rep, checkin_time='2026-09-19 18:30:00')
        self.assertEqual(checkin.checkin_time, datetime(2026, 9, 19, 18, 30))
        self.assertEqual(checkin.checkin_date, date(2026, 9, 20))
        self.assertEqual(checkin.get_local_datetime_str(), '20 Sep 2026 01:30')
        self.assertEqual(checkin._get_tz_name(), 'Asia/Phnom_Penh')

    def test_google_maps_url(self):
        checkin = self._create_checkin(self.rep)
        self.assertEqual(checkin.google_maps_url, 'https://www.google.com/maps?q=11.5565,104.9283')
        self.assertFalse(checkin.checkout_google_maps_url)
        action = checkin.action_open_google_maps()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertEqual(action['url'], checkin.google_maps_url)
        with self.assertRaises(UserError):
            checkin.action_open_checkout_google_maps()
        checkin.write({'checkout_latitude': 11.55, 'checkout_longitude': 104.93})
        self.assertEqual(checkin.checkout_google_maps_url, 'https://www.google.com/maps?q=11.55,104.93')
        self.assertEqual(checkin.action_open_checkout_google_maps()['url'], checkin.checkout_google_maps_url)

    def test_no_location(self):
        planned = self._create_checkin(self.rep, state='draft', latitude=0.0, longitude=0.0)
        self.assertFalse(planned.has_location)
        self.assertFalse(planned.google_maps_url)
        self.assertEqual(planned.location_status, 'no_gps')
        self.assertEqual(planned.distance_to_partner, 0.0)
        with self.assertRaises(UserError):
            planned.action_open_google_maps()

    # ------------------------------------------------------------------
    # GPS requirement
    # ------------------------------------------------------------------
    def test_gps_required_on_checkin(self):
        with self.assertRaises(UserError):
            self._create_checkin(self.rep, latitude=0.0, longitude=0.0)
        self._set_param('checkinme.require_gps', 'False')
        checkin = self._create_checkin(self.rep, latitude=0.0, longitude=0.0)
        self.assertEqual(checkin.state, 'checked_in')
        self.assertFalse(checkin.has_location)
        self.assertEqual(checkin.location_status, 'no_gps')

    def test_planned_visit_check_in_flow(self):
        planned = self._create_checkin(self.rep, state='draft', latitude=0.0, longitude=0.0)
        self.assertEqual(planned.state, 'draft')
        with self.assertRaises(UserError):
            planned.action_check_in()
        self.assertEqual(planned.state, 'draft')

        planned.write({'latitude': CHECKIN_LAT, 'longitude': CHECKIN_LNG, 'accuracy': 5.0})
        before = fields.Datetime.now()
        self.assertTrue(planned.action_check_in())
        self.assertEqual(planned.state, 'checked_in')
        self.assertGreaterEqual(planned.checkin_time, before)
        self.assertEqual(planned.location_status, 'verified')
        # Only planned visits can be checked in
        with self.assertRaises(UserError):
            planned.action_check_in()

    # ------------------------------------------------------------------
    # Location verification
    # ------------------------------------------------------------------
    def test_location_status_verified(self):
        checkin = self._create_checkin(self.rep)
        self.assertEqual(checkin.location_status, 'verified')
        self.assertAlmostEqual(checkin.distance_to_partner, 15.0, delta=10.0)
        self.assertIn('Verified', checkin._get_location_status_label())

    def test_location_status_far(self):
        checkin = self._create_checkin(self.rep, latitude=CHECKIN_LAT + 0.03)
        self.assertEqual(checkin.location_status, 'far')
        self.assertAlmostEqual(checkin.distance_to_partner, 3300.0, delta=200.0)

    def test_location_status_unknown_and_no_gps(self):
        unknown = self._create_checkin(self.rep, partner_id=self.partner_nogeo.id)
        self.assertEqual(unknown.location_status, 'unknown')
        self.assertEqual(unknown.distance_to_partner, 0.0)

        self._set_param('checkinme.require_gps', 'False')
        no_gps = self._create_checkin(self.rep, latitude=0.0, longitude=0.0)
        self.assertEqual(no_gps.location_status, 'no_gps')

    def test_max_distance_recompute(self):
        checkin = self._create_checkin(self.rep)
        self.assertEqual(checkin.location_status, 'verified')
        self._set_param('checkinme.max_distance_m', '5')
        self.assertTrue(checkin.action_recompute_location_status())
        self.assertEqual(checkin.location_status, 'far')
        self._set_param('checkinme.max_distance_m', '500')
        checkin.action_recompute_location_status()
        self.assertEqual(checkin.location_status, 'verified')

    def test_location_status_follows_customer_geolocation(self):
        checkin = self._create_checkin(self.rep, partner_id=self.partner_nogeo.id)
        self.assertEqual(checkin.location_status, 'unknown')
        self.partner_nogeo.write({'partner_latitude': CHECKIN_LAT, 'partner_longitude': CHECKIN_LNG})
        self.assertEqual(checkin.location_status, 'verified')
        self.assertAlmostEqual(checkin.distance_to_partner, 0.0, delta=1.0)

    # ------------------------------------------------------------------
    # Check-out, cancel, reset
    # ------------------------------------------------------------------
    def test_check_out(self):
        one_hour_ago = fields.Datetime.now() - timedelta(hours=1)
        checkin = self._create_checkin(self.rep, checkin_time=one_hour_ago)
        self.assertEqual(checkin.checkin_time, one_hour_ago)
        self.assertEqual(checkin.duration, 0.0)

        self.assertTrue(checkin.action_check_out())
        self.assertEqual(checkin.state, 'done')
        self.assertTrue(checkin.checkout_time)
        self.assertGreaterEqual(checkin.checkout_time, checkin.checkin_time)
        self.assertGreater(checkin.duration, 0.0)
        self.assertAlmostEqual(checkin.duration, 1.0, delta=0.1)

        with self.assertRaises(UserError):
            checkin.action_check_out()

    def test_check_out_requires_checked_in(self):
        planned = self._create_checkin(self.rep, state='draft')
        with self.assertRaises(UserError):
            planned.action_check_out()

    def test_cancel_and_reset(self):
        checkin = self._create_checkin(self.rep)
        with self.assertRaises(UserError):
            checkin.action_reset_to_draft()
        self.assertTrue(checkin.action_cancel())
        self.assertEqual(checkin.state, 'cancel')
        with self.assertRaises(UserError):
            checkin.action_cancel()
        self.assertTrue(checkin.action_reset_to_draft())
        self.assertEqual(checkin.state, 'draft')
        self.assertFalse(checkin.checkout_time)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    def test_invalid_coordinates(self):
        with self.assertRaises(ValidationError):
            self._create_checkin(self.rep, latitude=95.0)
        with self.assertRaises(ValidationError):
            self._create_checkin(self.rep, longitude=-190.0)
        checkin = self._create_checkin(self.rep)
        with self.assertRaises(ValidationError):
            checkin.write({'checkout_latitude': -91.0, 'checkout_longitude': 104.0})

    def test_checkout_before_checkin(self):
        checkin = self._create_checkin(self.rep)
        with self.assertRaises(ValidationError):
            checkin.write({'checkout_time': checkin.checkin_time - timedelta(hours=1)})
        with self.assertRaises(ValidationError):
            self._create_checkin(
                self.rep, checkin_time='2026-09-19 10:00:00', checkout_time='2026-09-19 09:00:00')

    def test_customer_required_by_activity_type(self):
        self.assertTrue(self.type_visit.requires_customer)
        with self.assertRaises(ValidationError):
            self._create_checkin(self.rep, partner_id=False)
        # An activity type that does not require a customer is fine without one
        self.assertFalse(self.type_other.requires_customer)
        checkin = self._create_checkin(self.rep, partner_id=False, checkin_type_id=self.type_other.id)
        self.assertEqual(checkin.state, 'checked_in')
        self.assertEqual(checkin.location_status, 'unknown')
        self.assertFalse(checkin.is_new_customer)
        self.assertEqual(checkin.display_name, checkin.name)

    # ------------------------------------------------------------------
    # Customer & sales helpers
    # ------------------------------------------------------------------
    def test_is_new_customer(self):
        partner = self.env['res.partner'].create({'name': 'Brand New Shop'})
        first = self._create_checkin(self.rep, partner_id=partner.id)
        self.assertTrue(first.is_new_customer)
        second = self._create_checkin(self.other_rep, partner_id=partner.id)
        self.assertFalse(second.is_new_customer)
        self.assertTrue(first.is_new_customer)

    def test_action_create_quotation(self):
        checkin = self._create_checkin(self.rep)
        action = checkin.action_create_quotation()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'sale.order')
        self.assertEqual(action['context']['default_partner_id'], self.partner_geo.id)
        self.assertEqual(action['context']['default_checkinme_checkin_id'], checkin.id)
        self.assertEqual(action['context']['default_user_id'], self.rep.id)

        order = self._create_confirmed_order(self.rep, self.partner_geo, 100.0, checkinme_checkin_id=checkin.id)
        self.assertEqual(order.checkinme_checkin_id, checkin)
        self.assertEqual(checkin.sale_order_count, 1)
        self.assertAlmostEqual(checkin.order_amount, 100.0)
        self.assertEqual(checkin.action_view_sale_orders()['domain'], [('checkinme_checkin_id', '=', checkin.id)])

        no_customer = self._create_checkin(self.rep, partner_id=False, checkin_type_id=self.type_other.id)
        with self.assertRaises(UserError):
            no_customer.action_create_quotation()

    def test_chatter_message_on_checkin(self):
        checkin = self._create_checkin(self.rep)
        bodies = [msg.body or '' for msg in checkin.sudo().message_ids]
        self.assertTrue(any('Checked in' in body for body in bodies), bodies)
        self.assertTrue(any('Google Maps' in body for body in bodies), bodies)
        checkin.action_check_out()
        bodies = [msg.body or '' for msg in checkin.sudo().message_ids]
        self.assertTrue(any('Checked out' in body for body in bodies), bodies)

    # ------------------------------------------------------------------
    # Period helpers
    # ------------------------------------------------------------------
    def test_get_period_dates(self):
        Checkin = self.env['checkinme.checkin']
        today = date(2026, 9, 20)  # a Sunday
        self.assertEqual(Checkin._get_period_dates('today', today=today), (today, today))
        self.assertEqual(Checkin._get_period_dates('yesterday', today=today), (date(2026, 9, 19), date(2026, 9, 19)))
        self.assertEqual(Checkin._get_period_dates('this_week', today=today), (date(2026, 9, 14), date(2026, 9, 20)))
        self.assertEqual(Checkin._get_period_dates('last_week', today=today), (date(2026, 9, 7), date(2026, 9, 13)))
        self.assertEqual(Checkin._get_period_dates('this_month', today=today), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(Checkin._get_period_dates('last_month', today=today), (date(2026, 8, 1), date(2026, 8, 31)))
        self.assertEqual(Checkin._get_period_dates('this_year', today=today), (date(2026, 1, 1), date(2026, 12, 31)))
        self.assertEqual(Checkin._get_period_dates('last_year', today=today), (date(2025, 1, 1), date(2025, 12, 31)))
        # Month / year boundaries
        self.assertEqual(Checkin._get_period_dates('last_month', today=date(2026, 1, 15)), (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertEqual(Checkin._get_period_dates('this_week', today=date(2026, 9, 14)), (date(2026, 9, 14), date(2026, 9, 20)))
        with self.assertRaises(ValueError):
            Checkin._get_period_dates('fortnight', today=today)

    def test_get_utc_bounds(self):
        Checkin = self.env['checkinme.checkin']
        start, end = Checkin._get_utc_bounds(date(2026, 9, 20), date(2026, 9, 20), 'Asia/Phnom_Penh')
        self.assertEqual((start, end), (datetime(2026, 9, 19, 17, 0), datetime(2026, 9, 20, 17, 0)))
        start, end = Checkin._get_utc_bounds(date(2026, 9, 1), date(2026, 9, 30), 'Asia/Phnom_Penh')
        self.assertEqual((start, end), (datetime(2026, 8, 31, 17, 0), datetime(2026, 9, 30, 17, 0)))
        start, end = Checkin._get_utc_bounds(date(2026, 9, 20), date(2026, 9, 20), 'UTC')
        self.assertEqual((start, end), (datetime(2026, 9, 20, 0, 0), datetime(2026, 9, 21, 0, 0)))
        # Unknown timezones fall back to UTC instead of crashing
        start, end = Checkin._get_utc_bounds(date(2026, 9, 20), date(2026, 9, 20), 'Mars/Olympus')
        self.assertEqual((start, end), (datetime(2026, 9, 20, 0, 0), datetime(2026, 9, 21, 0, 0)))

    def test_get_today_uses_company_timezone(self):
        Checkin = self.env['checkinme.checkin']
        self.assertEqual(Checkin._get_today(), Checkin._get_today(self.company))
        self.assertIsInstance(Checkin._get_today(), date)

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------
    def test_unlink_rights(self):
        checkin = self._create_checkin(self.rep)
        # Salespeople cannot delete a check-in that already happened
        with self.assertRaises(UserError):
            checkin.with_user(self.rep).unlink()
        self.assertTrue(checkin.exists())
        # ...but they may delete their own planned visits
        planned = self._create_checkin(self.rep, state='draft')
        self.assertTrue(planned.with_user(self.rep).unlink())
        self.assertFalse(planned.exists())
        # Managers can delete checked-in records
        self.assertTrue(checkin.with_user(self.manager).unlink())
        self.assertFalse(checkin.exists())
