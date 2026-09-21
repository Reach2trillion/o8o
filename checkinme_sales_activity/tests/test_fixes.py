# -*- coding: utf-8 -*-
"""Regression tests for the issues found during the module review."""
from datetime import datetime

import pytz
import requests
from dateutil.relativedelta import relativedelta

from odoo.exceptions import AccessError
from odoo.tests.common import new_test_user, tagged
from odoo.tools import mute_logger

from .common import CheckinmeCommon, MAIL_CONTEXT, TEST_BOT_TOKEN, TEST_CHAT_ID, TZ, mock_telegram

RULE_LOGGER = 'odoo.addons.base.models.ir_rule'


@tagged('post_install', '-at_install')
class TestReviewFixes(CheckinmeCommon):

    def _async_context(self):
        ctx = dict(MAIL_CONTEXT)
        ctx.pop('checkinme_telegram_sync', None)
        return ctx

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------
    def test_telegram_queue(self):
        """Automatic notifications are queued and sent by the cron, never inside the user's request."""
        self._configure_telegram()
        Checkin = self.env['checkinme.checkin'].with_user(self.rep).with_context(**self._async_context())
        with mock_telegram() as recorder:
            checkin = Checkin.create({
                'employee_id': self.rep_employee.id, 'partner_id': self.partner_geo.id,
                'checkin_type_id': self.type_visit.id, 'latitude': 11.5565, 'longitude': 104.9283,
            })
            self.assertEqual(checkin.telegram_pending_event, 'checkin')
            self.assertFalse(checkin.telegram_notified)
            self.assertFalse(recorder.calls)
            self.env['checkinme.checkin']._cron_send_pending_telegram()
            self.assertFalse(checkin.telegram_pending_event)
            self.assertTrue(checkin.telegram_notified)
            self.assertIn('sendMessage', recorder.methods)
            sent = len(recorder.calls)
            # the check-out is queued too; the flag is reset until it is delivered
            checkin.action_check_out()
            self.assertEqual(checkin.telegram_pending_event, 'checkout')
            self.assertFalse(checkin.telegram_notified)
            self.assertEqual(len(recorder.calls), sent)
            self.env['checkinme.checkin']._cron_send_pending_telegram()
            self.assertTrue(checkin.telegram_notified)
            self.assertGreater(len(recorder.calls), sent)
            # forced manual sending is immediate
            checkin.with_user(self.manager).action_send_telegram()

    def test_token_never_leaks(self):
        self._configure_telegram()
        error = requests.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: "
            "/bot%s/sendMessage" % TEST_BOT_TOKEN)
        with mock_telegram(side_effect=error):
            ok, response = self.env['checkinme.telegram']._send_message(TEST_CHAT_ID, 'hello', message_type='test')
        self.assertFalse(ok)
        self.assertTrue(response.get('transport_error'))
        self.assertNotIn(TEST_BOT_TOKEN, response['description'])
        self.assertIn('/bot***', response['description'])
        log = self.env['checkinme.telegram.log'].search([('message_type', '=', 'test')], order='id desc', limit=1)
        self.assertEqual(log.state, 'failed')
        self.assertNotIn(TEST_BOT_TOKEN, log.response or '')

    def test_transport_failure_stops_other_chats(self):
        """When Telegram is unreachable the other chats are not attempted (no 10 s per chat)."""
        self._configure_telegram()
        self.assertEqual(len(self.env['checkinme.telegram']._get_manager_chat_ids(self.rep_employee)), 2)
        with mock_telegram(side_effect=requests.ConnectionError('down')) as recorder:
            checkin = self._create_checkin(self.rep)
        self.assertEqual(len(recorder.calls), 1)
        self.assertFalse(checkin.telegram_notified)

    def test_telegram_actions_are_guarded(self):
        self._configure_telegram()
        with mock_telegram():
            with self.assertRaises(AccessError):
                self.env['checkinme.telegram'].with_user(self.rep).action_test_connection()
            checkin = self._create_checkin(self.rep)
            with self.assertRaises(AccessError):
                checkin.with_user(self.rep).action_send_telegram()
            self.assertTrue(checkin.with_user(self.manager).action_send_telegram())

    def test_department_manager_chat(self):
        self._configure_telegram()
        dept = self.env['hr.department'].create({'name': 'Field Sales', 'manager_id': self.manager_employee.id})
        lead = self.env['hr.employee'].create({
            'name': 'Team Lead', 'company_id': self.company.id, 'checkinme_telegram_chat_id': ' 777 '})
        self.rep_employee.write({'department_id': dept.id, 'parent_id': lead.id})
        chats = self.env['checkinme.telegram']._get_manager_chat_ids(self.rep_employee)
        self.assertEqual(chats, [TEST_CHAT_ID, '777', self.manager_employee.checkinme_telegram_chat_id])
        # an employee who manages their own department is not notified about themselves
        self.manager_employee.department_id = dept
        self.assertEqual(self.env['checkinme.telegram']._get_manager_chat_ids(self.manager_employee), [TEST_CHAT_ID])

    def test_align_report_crons(self):
        self.env['checkinme.telegram']._align_report_crons('Asia/Phnom_Penh')
        daily = self.env.ref('checkinme_sales_activity.ir_cron_checkinme_daily_report')
        weekly = self.env.ref('checkinme_sales_activity.ir_cron_checkinme_weekly_report')
        monthly = self.env.ref('checkinme_sales_activity.ir_cron_checkinme_monthly_report')
        self.assertEqual((daily.nextcall.hour, daily.nextcall.minute), (11, 0))  # 18:00 Phnom Penh
        self.assertEqual((weekly.nextcall.weekday(), weekly.nextcall.hour), (0, 2))  # Monday 09:00
        self.assertEqual((monthly.nextcall.day, monthly.nextcall.hour), (1, 2))  # 1st 09:00
        self.assertGreater(daily.nextcall, datetime.utcnow())

    # ------------------------------------------------------------------
    # Security
    # ------------------------------------------------------------------
    def test_rep_cannot_reassign_checkin(self):
        checkin = self._create_checkin(self.rep)
        with self.assertRaises(AccessError):
            checkin.with_user(self.rep).write({'employee_id': self.other_rep_employee.id})
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self._create_checkin(self.rep, employee_id=self.other_rep_employee.id)
        checkin.with_user(self.manager).write({'employee_id': self.other_rep_employee.id})
        self.assertEqual(checkin.employee_id, self.other_rep_employee)

    def test_direct_reports_are_read_only(self):
        team_lead = new_test_user(
            self.env, login='checkinme_test_lead', name='Team Lead',
            groups='base.group_user,checkinme_sales_activity.group_checkinme_user', context=MAIL_CONTEXT, tz=TZ)
        lead_employee = self.env['hr.employee'].with_context(**MAIL_CONTEXT).create({
            'name': 'Team Lead', 'user_id': team_lead.id, 'company_id': self.company.id, 'tz': TZ})
        self.rep_employee.parent_id = lead_employee
        checkin = self._create_checkin(self.rep)
        self.assertEqual(checkin.with_user(team_lead).read(['name'])[0]['name'], checkin.name)
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            checkin.with_user(team_lead).write({'purpose': 'edited by the team lead'})
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self._create_checkin(self.rep, state='draft').with_user(team_lead).unlink()

    # ------------------------------------------------------------------
    # Check-ins
    # ------------------------------------------------------------------
    def test_new_customer_after_cancel_planned_and_delete(self):
        partner = self.env['res.partner'].create({'name': 'Brand New Shop'})
        planned = self._create_checkin(self.rep, partner_id=partner.id, state='draft')
        first = self._create_checkin(self.rep, partner_id=partner.id)
        second = self._create_checkin(self.rep, partner_id=partner.id)
        self.assertFalse(planned.is_new_customer)
        self.assertTrue(first.is_new_customer)
        self.assertFalse(second.is_new_customer)
        first.with_user(self.rep).action_cancel()
        self.assertFalse(first.is_new_customer)
        self.assertTrue(second.is_new_customer)
        third = self._create_checkin(self.rep, partner_id=partner.id)
        self.assertFalse(third.is_new_customer)
        second.with_user(self.manager).unlink()
        self.assertTrue(third.is_new_customer)
        # a back-dated check-in takes the flag from the later one
        earlier = self._create_checkin(self.rep, partner_id=partner.id, checkin_time=datetime(2020, 1, 1, 8, 0))
        self.assertTrue(earlier.is_new_customer)
        self.assertFalse(third.is_new_customer)

    def test_copy_is_a_planned_visit(self):
        checkin = self._create_checkin(self.rep, notes='original')
        duplicate = checkin.copy()
        self.assertEqual(duplicate.state, 'draft')
        self.assertFalse(duplicate.latitude)
        self.assertFalse(duplicate.telegram_notified)
        self.assertEqual(duplicate.notes, 'original')

    def test_calendar_stop(self):
        checkin = self._create_checkin(self.rep, checkin_time=datetime(2026, 9, 1, 8, 0))
        self.assertEqual(checkin.calendar_stop, datetime(2026, 9, 1, 9, 0))
        checkin.checkout_time = datetime(2026, 9, 1, 10, 30)
        self.assertEqual(checkin.calendar_stop, datetime(2026, 9, 1, 10, 30))

    def test_get_today_uses_company_timezone(self):
        Checkin = self.env['checkinme.checkin']
        self.assertEqual(Checkin._get_today(), datetime.now(pytz.timezone(TZ)).date())
        self.company.partner_id.tz = 'Pacific/Kiritimati'  # UTC+14
        self.assertEqual(Checkin._get_today(), datetime.now(pytz.timezone('Pacific/Kiritimati')).date())
        self.company.partner_id.tz = 'Pacific/Pago_Pago'  # UTC-11
        self.assertEqual(Checkin._get_today(), datetime.now(pytz.timezone('Pacific/Pago_Pago')).date())

    # ------------------------------------------------------------------
    # Targets and reports
    # ------------------------------------------------------------------
    def test_target_upcoming_status_and_unrounded_achievement(self):
        today = self._today()
        nxt = today.replace(day=1) + relativedelta(months=1)
        upcoming = self._create_target(self.rep_employee, year=nxt.year, month=nxt.month, target_visits=10)
        self.assertEqual(upcoming.status, 'upcoming')
        self.assertIn(upcoming, self.env['checkinme.target'].search([('status', '=', 'upcoming')]))
        self._set_param('checkinme.sales_source', 'checkin')
        current = self._create_target(self.rep_employee, target_sales_amount=2500.0)
        self._create_checkin(self.rep, sale_amount=2499.0)
        current.invalidate_recordset()
        self.assertEqual(current.achievement_sales, 100.0)  # 99.96 rounded for display
        self.assertNotEqual(current.status, 'achieved')
        self._create_checkin(self.rep, sale_amount=1.0)
        current.invalidate_recordset()
        self.assertEqual(current.status, 'achieved')

    def test_search_status_and_period_order(self):
        Target = self.env['checkinme.target']
        last = self._today().replace(day=1) - relativedelta(months=1)
        behind = self._create_target(self.rep_employee, year=last.year, month=last.month, target_visits=10)
        untargeted = self._create_target(self.other_rep_employee)
        self.assertIn(behind, Target.search([('status', '=', 'behind')]))
        self.assertNotIn(untargeted, Target.search([('status', '=', 'behind')]))
        self.assertIn(untargeted, Target.search([('status', 'in', ['none'])]))
        self.assertNotIn(untargeted, Target.search([('status', '!=', 'none')]))
        # ordered by period, not by the month selection key as text ('9' > '10')
        sept = self._create_target(self.rep_employee, year=2030, month=9)
        octo = self._create_target(self.rep_employee, year=2030, month=10)
        self.assertEqual(Target.search([('year', '=', 2030)]), octo | sept)

    def test_summary_order_only_employees_and_month_to_date_targets(self):
        Checkin = self.env['checkinme.checkin']
        today = self._today()
        self._create_confirmed_order(self.other_rep, self.partner_geo, 300.0)
        summary = Checkin._get_performance_summary(today, today)
        row = [r for r in summary['employees'] if r['employee'] == self.other_rep_employee]
        self.assertEqual(len(row), 1)
        self.assertEqual((row[0]['checkins'], row[0]['orders']), (0, 1))
        self.assertAlmostEqual(row[0]['sales_amount'], 300.0)
        self.assertAlmostEqual(summary['totals']['sales_amount'], 300.0)
        # a range crossing a month boundary carries the month-to-date target of its last month
        target = self._create_target(self.rep_employee, target_sales_amount=1000.0)
        month_start = today.replace(day=1)
        date_from, date_to = month_start - relativedelta(days=3), month_start + relativedelta(days=3)
        summary = Checkin._get_performance_summary(date_from, date_to, employees=self.rep_employee)
        self.assertEqual(summary['employees'][0]['target'], target)
        self.assertEqual(summary['totals']['target_month'], date_to)
        text = self.env['checkinme.telegram']._format_period_report(summary, 'Weekly Sales Activity Report')
        self.assertIn('month to date', text)

    def test_sql_report_checkin_orders_and_archived_employee(self):
        checkin = self._create_checkin(self.rep, outcome='order', sale_amount=50.0)
        order = self._create_confirmed_order(self.rep, self.partner_geo, 120.0)
        self.env.flush_all()
        Report = self.env['checkinme.performance.report']
        row = Report.search([('checkin_id', '=', checkin.id)])
        self.assertEqual((row.checkin_order_count, row.order_count), (1, 0))
        self.assertAlmostEqual(row.checkin_sale_amount, 50.0)
        self.rep_employee.active = False
        self.env.flush_all()
        self.env.invalidate_all()
        sale_row = Report.search([('sale_order_id', '=', order.id)])
        self.assertEqual(sale_row.employee_id, self.rep_employee)
        self.assertAlmostEqual(sale_row.order_amount, 120.0)

    def test_foreign_currency_orders(self):
        eur = self.env.ref('base.EUR')
        eur.active = True
        # one deterministic rate: 1 USD = 2 EUR (drop demo rates, which may be dated in the future)
        self.env['res.currency.rate'].search([('currency_id', '=', eur.id)]).unlink()
        self.env['res.currency.rate'].create({
            'currency_id': eur.id, 'rate': 2.0, 'name': '2000-01-01', 'company_id': self.company.id})
        pricelist = self.env['product.pricelist'].create({'name': 'EUR list', 'currency_id': eur.id})
        order = self._create_confirmed_order(self.rep, self.partner_geo, 100.0, pricelist_id=pricelist.id)
        self.assertEqual(order.currency_id, eur)
        self.assertAlmostEqual(order.currency_rate, 2.0)
        today = self._today()
        row = self.env['checkinme.checkin']._get_performance_summary(today, today, employees=self.rep_employee)['employees'][0]
        self.assertAlmostEqual(row['sales_amount'], 50.0)  # 100 EUR at 2 EUR per USD
        self.env.flush_all()
        sale_row = self.env['checkinme.performance.report'].search([('sale_order_id', '=', order.id)])
        self.assertAlmostEqual(sale_row.order_amount, 50.0)
        target = self._create_target(self.rep_employee, target_sales_amount=100.0)
        self.assertAlmostEqual(target.actual_sales_amount, 50.0)
