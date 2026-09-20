# -*- coding: utf-8 -*-
from datetime import datetime

from dateutil.relativedelta import relativedelta
from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import CheckinmeCommon, CHECKIN_LAT


@tagged('post_install', '-at_install')
class TestTarget(CheckinmeCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Target = cls.env['checkinme.target']
        cls.today = cls._today()
        cls.month_start = cls.today.replace(day=1)
        cls.month_end = cls.month_start + relativedelta(months=1, days=-1)

    def setUp(self):
        super().setUp()
        self.telegram = self._patch_telegram()

    def _create_rep_activity(self):
        """rep: 2 verified visits + 1 far visit + 1 phone call; other rep: 1 verified visit."""
        c1 = self._create_checkin(self.rep)
        c2 = self._create_checkin(self.rep)
        c3 = self._create_checkin(self.rep, latitude=CHECKIN_LAT + 0.03)
        c4 = self._create_checkin(self.rep, checkin_type_id=self.type_call.id, partner_id=self.partner_nogeo.id)
        other = self._create_checkin(self.other_rep)
        self.assertEqual((c1 | c2).mapped('location_status'), ['verified', 'verified'])
        self.assertEqual(c3.location_status, 'far')
        self.assertFalse(c4.counts_as_visit)
        return c1, c2, c3, c4, other

    # ------------------------------------------------------------------
    # Basics
    # ------------------------------------------------------------------
    def test_dates_name_and_current(self):
        target = self._create_target(self.rep_employee)
        self.assertEqual(target.year, self.today.year)
        self.assertEqual(int(target.month), self.today.month)
        self.assertEqual(target.date_from, self.month_start)
        self.assertEqual(target.date_to, self.month_end)
        self.assertIn(self.rep_employee.name, target.name)
        self.assertEqual(target.user_id, self.rep)
        self.assertEqual(target.currency_id, self.company.currency_id)
        self.assertTrue(target.is_current)
        self.assertGreater(target.expected_progress, 0.0)
        self.assertLessEqual(target.expected_progress, 100.0)
        self.assertEqual(target.days_remaining, (self.month_end - self.today).days)

        current = self.Target.search([('is_current', '=', True)])
        self.assertIn(target, current)
        self.assertNotIn(target, self.Target.search([('is_current', '=', False)]))

        last = self.month_start - relativedelta(months=1)
        old_target = self._create_target(self.rep_employee, year=last.year, month=last.month)
        self.assertFalse(old_target.is_current)
        self.assertNotIn(old_target, current)
        self.assertIn(old_target, self.Target.search([('is_current', '!=', True)]))
        self.assertEqual(old_target.date_from, last)
        self.assertEqual(old_target.date_to, last + relativedelta(months=1, days=-1))

    def test_unique_per_employee_and_month(self):
        self._create_target(self.rep_employee)
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), self.cr.savepoint():
            self._create_target(self.rep_employee)
            self.Target.flush_model()
        # Same month for another employee, or another month for the same one, is fine
        self._create_target(self.other_rep_employee)
        last = self.month_start - relativedelta(months=1)
        self._create_target(self.rep_employee, year=last.year, month=last.month)

    def test_constraints(self):
        with self.assertRaises(ValidationError):
            self._create_target(self.rep_employee, year=1999)
        with self.assertRaises(ValidationError):
            self._create_target(self.rep_employee, target_visits=-1)
        with self.assertRaises(ValidationError):
            self._create_target(self.rep_employee, target_sales_amount=-100.0)

    # ------------------------------------------------------------------
    # Actuals
    # ------------------------------------------------------------------
    def test_actuals_and_achievements(self):
        c1, c2, c3, c4, other = self._create_rep_activity()
        target = self._create_target(self.rep_employee, target_visits=6, target_new_customers=1)

        self.assertEqual(target.actual_checkins, 4)
        self.assertEqual(target.actual_visits, 3)
        self.assertEqual(target.actual_verified_visits, 2)
        self.assertEqual(target.actual_customers, 2)
        self.assertEqual(target.actual_new_customers, 2)
        self.assertEqual(target.actual_orders, 0)
        self.assertEqual(target.actual_sales_amount, 0.0)

        # Percentages, and the overall rate averages only the KPIs that have a target
        self.assertEqual(target.achievement_visits, 50.0)
        self.assertEqual(target.achievement_new_customers, 200.0)
        self.assertEqual(target.achievement_orders, 0.0)
        self.assertEqual(target.achievement_sales, 0.0)
        self.assertEqual(target.achievement_rate, 125.0)
        self.assertEqual(target.status, 'achieved')

        # The other salesperson's target only counts his own activity
        other_target = self._create_target(self.other_rep_employee, target_visits=2)
        self.assertEqual(other_target.actual_checkins, 1)
        self.assertEqual(other_target.actual_visits, 1)
        self.assertEqual(other_target.actual_verified_visits, 1)
        self.assertEqual(other_target.actual_customers, 1)
        self.assertEqual(other_target.actual_new_customers, 0)
        self.assertEqual(other_target.achievement_visits, 50.0)
        self.assertEqual(other_target.achievement_rate, 50.0)

        # Cancelled check-ins are not counted
        c2.action_cancel()
        target.invalidate_recordset()
        self.assertEqual(target.actual_checkins, 3)
        self.assertEqual(target.actual_visits, 2)
        self.assertEqual(target.actual_verified_visits, 1)

    def test_actual_sales_from_checkins(self):
        c1, c2, c3, c4, other = self._create_rep_activity()
        c1.write({'outcome': 'order', 'sale_amount': 300.0})
        c2.write({'outcome': 'order', 'sale_amount': 100.0})
        c3.write({'outcome': 'follow_up', 'sale_amount': 400.0})
        other.write({'outcome': 'order', 'sale_amount': 500.0})
        target = self._create_target(self.rep_employee, target_orders=4, target_sales_amount=800.0)
        # Default source: confirmed sales orders -> nothing yet
        self.assertEqual(target.actual_orders, 0)
        self.assertEqual(target.actual_sales_amount, 0.0)

        self._set_param('checkinme.sales_source', 'checkin')
        target.invalidate_recordset()
        # Orders = check-ins with the 'order' outcome, amount = every sales amount entered
        self.assertEqual(target.actual_orders, 2)
        self.assertAlmostEqual(target.actual_sales_amount, 800.0)
        self.assertEqual(target.achievement_orders, 50.0)
        self.assertEqual(target.achievement_sales, 100.0)
        self.assertEqual(target.achievement_rate, 75.0)
        # The other salesperson's order does not leak into this target
        other_target = self._create_target(self.other_rep_employee, target_orders=1)
        self.assertEqual(other_target.actual_orders, 1)
        self.assertAlmostEqual(other_target.actual_sales_amount, 500.0)
        self.assertEqual(other_target.status, 'achieved')

    def test_actual_sales_from_sale_orders(self):
        self._create_rep_activity()
        target = self._create_target(self.rep_employee, target_orders=1, target_sales_amount=200.0)
        self.assertEqual(target.actual_orders, 0)

        order = self._create_confirmed_order(self.rep, self.partner_geo, 100.0)
        self.assertEqual(order.state, 'sale')
        self.assertAlmostEqual(order.amount_untaxed, 100.0)
        # A quotation (not confirmed) and an order of another salesperson do not count
        self.env['sale.order'].create({
            'partner_id': self.partner_geo.id,
            'user_id': self.rep.id,
            'company_id': self.company.id,
            'order_line': [(0, 0, {'product_id': self.product.id, 'product_uom_qty': 1.0, 'price_unit': 50.0})],
        })
        self._create_confirmed_order(self.other_rep, self.partner_geo, 300.0)

        target.invalidate_recordset()
        self.assertEqual(target.actual_orders, 1)
        self.assertAlmostEqual(target.actual_sales_amount, 100.0)
        self.assertEqual(target.achievement_orders, 100.0)
        self.assertEqual(target.achievement_sales, 50.0)
        self.assertEqual(target.achievement_rate, 75.0)

        action = target.action_view_sale_orders()
        self.assertEqual(action['res_model'], 'sale.order')
        orders = self.env['sale.order'].search(action['domain'])
        self.assertEqual(orders, order)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def test_status_behind_for_finished_month(self):
        last = self.month_start - relativedelta(months=1)
        target = self._create_target(
            self.rep_employee, year=last.year, month=last.month, target_sales_amount=1000.0)
        self.assertEqual(target.expected_progress, 100.0)
        self.assertEqual(target.days_remaining, 0)
        self.assertEqual(target.achievement_rate, 0.0)
        self.assertEqual(target.status, 'behind')

    def test_status_achieved(self):
        self._create_checkin(self.rep)
        self._create_checkin(self.rep)
        target = self._create_target(self.rep_employee, target_visits=2)
        self.assertEqual(target.actual_visits, 2)
        self.assertEqual(target.achievement_visits, 100.0)
        self.assertEqual(target.status, 'achieved')
        # Overshooting is still achieved
        target.target_visits = 1
        self.assertEqual(target.achievement_visits, 200.0)
        self.assertEqual(target.status, 'achieved')

    def test_status_none_without_targets(self):
        self._create_checkin(self.rep)
        target = self._create_target(self.rep_employee)
        self.assertEqual(target.actual_visits, 1)
        self.assertEqual(target.achievement_rate, 0.0)
        self.assertEqual(target.status, 'none')

    def test_status_progress_bands(self):
        # A finished month has 100 % expected progress, so the bands only depend on the
        # achievement rate: < 60 % behind, < 90 % at risk, < 100 % on track, else achieved.
        last = self.month_start - relativedelta(months=1)
        when = datetime(last.year, last.month, 15, 3, 0)  # 10:00 in Phnom Penh, last month
        target = self._create_target(self.rep_employee, year=last.year, month=last.month, target_visits=10)
        self.assertEqual(target.expected_progress, 100.0)
        created = 0
        for visits, expected in ((0, 'behind'), (5, 'behind'), (6, 'at_risk'), (8, 'at_risk'),
                                 (9, 'on_track'), (10, 'achieved'), (12, 'achieved')):
            while created < visits:
                self._create_checkin(self.rep, checkin_time=when)
                created += 1
            target.invalidate_recordset()
            self.assertEqual(target.actual_visits, visits)
            self.assertEqual(target.achievement_rate, visits * 10.0)
            self.assertEqual(target.status, expected, '%d visits should give %s' % (visits, expected))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def test_action_view_checkins(self):
        c1, c2, c3, c4, other = self._create_rep_activity()
        c2.action_cancel()
        target = self._create_target(self.rep_employee)
        action = target.action_view_checkins()
        self.assertEqual(action['res_model'], 'checkinme.checkin')
        found = self.env['checkinme.checkin'].search(action['domain'])
        self.assertEqual(found, c1 | c3 | c4)
        self.assertEqual(action['context'].get('default_employee_id'), self.rep_employee.id)
