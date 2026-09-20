# -*- coding: utf-8 -*-
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import tagged

from .common import CheckinmeCommon, mock_telegram, CHECKIN_LAT, TEST_CHAT_ID


@tagged('post_install', '-at_install')
class TestReport(CheckinmeCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Checkin = cls.env['checkinme.checkin']
        cls.Wizard = cls.env['checkinme.report.wizard']
        cls.today = cls._today()
        cls.month_start = cls.today.replace(day=1)
        cls.month_end = cls.month_start + relativedelta(months=1, days=-1)
        cls.employees = cls.rep_employee | cls.other_rep_employee

    def setUp(self):
        super().setUp()
        self.telegram = self._patch_telegram()

    def _create_activity(self):
        """rep: 2 verified visits + 1 far visit + 1 phone call; other rep: 1 verified visit."""
        c1 = self._create_checkin(self.rep)
        c2 = self._create_checkin(self.rep)
        c3 = self._create_checkin(self.rep, latitude=CHECKIN_LAT + 0.03)
        c4 = self._create_checkin(self.rep, checkin_type_id=self.type_call.id, partner_id=self.partner_nogeo.id)
        other = self._create_checkin(self.other_rep)
        return c1, c2, c3, c4, other

    def _row(self, summary, employee):
        rows = [row for row in summary['employees'] if row['employee'] == employee]
        self.assertEqual(len(rows), 1, 'expected exactly one row for %s' % employee.name)
        return rows[0]

    # ------------------------------------------------------------------
    # Performance summary
    # ------------------------------------------------------------------
    def test_performance_summary(self):
        c1, c2, c3, c4, other = self._create_activity()
        order = self._create_confirmed_order(self.rep, self.partner_geo, 100.0)

        summary = self.Checkin._get_performance_summary(self.month_start, self.month_end, employees=self.employees)
        self.assertEqual(summary['date_from'], self.month_start)
        self.assertEqual(summary['date_to'], self.month_end)
        self.assertEqual(summary['company'], self.company)
        self.assertEqual(summary['currency'], self.company.currency_id)
        self.assertEqual(summary['sales_source'], 'sale_order')
        self.assertEqual(summary['checkins'], c1 | c2 | c3 | c4 | other)
        self.assertEqual([row['employee'].name for row in summary['employees']],
                         sorted(self.employees.mapped('name')))

        rep_row = self._row(summary, self.rep_employee)
        self.assertEqual(rep_row['checkins'], 4)
        self.assertEqual(rep_row['visits'], 3)
        self.assertEqual(rep_row['verified'], 2)
        self.assertEqual(rep_row['customers'], 2)
        self.assertEqual(rep_row['new_customers'], 2)
        self.assertEqual(rep_row['order_count'], 1)
        self.assertEqual(rep_row['orders'], 1)
        self.assertAlmostEqual(rep_row['order_amount'], 100.0)
        self.assertAlmostEqual(rep_row['sales_amount'], 100.0)
        self.assertAlmostEqual(rep_row['checkin_sale_amount'], 0.0)
        self.assertEqual(rep_row['by_type'], [(self.type_visit.name, 3), (self.type_call.name, 1)])
        self.assertEqual(rep_row['by_outcome'], {})
        self.assertEqual(rep_row['checkin_records'], c1 | c2 | c3 | c4)
        self.assertFalse(rep_row['target'])
        self.assertNotIn('target_visits', rep_row)

        other_row = self._row(summary, self.other_rep_employee)
        self.assertEqual(other_row['checkins'], 1)
        self.assertEqual(other_row['visits'], 1)
        self.assertEqual(other_row['verified'], 1)
        self.assertEqual(other_row['customers'], 1)
        self.assertEqual(other_row['new_customers'], 0)
        self.assertEqual(other_row['order_count'], 0)
        self.assertEqual(other_row['sales_amount'], 0.0)

        totals = summary['totals']
        self.assertEqual(totals['employees'], 2)
        self.assertEqual(totals['checkins'], 5)
        self.assertEqual(totals['visits'], 4)
        self.assertEqual(totals['verified'], 3)
        self.assertEqual(totals['customers'], 2)
        self.assertEqual(totals['new_customers'], 2)
        self.assertEqual(totals['orders'], 1)
        self.assertAlmostEqual(totals['sales_amount'], 100.0)
        self.assertEqual(totals['target_sales_amount'], 0.0)
        self.assertEqual(totals['target_visits'], 0)

        # Sales measured from the check-ins instead of the confirmed orders
        c1.write({'outcome': 'order', 'sale_amount': 250.0})
        c4.write({'outcome': 'follow_up'})
        self._set_param('checkinme.sales_source', 'checkin')
        summary = self.Checkin._get_performance_summary(self.month_start, self.month_end, employees=self.employees)
        self.assertEqual(summary['sales_source'], 'checkin')
        rep_row = self._row(summary, self.rep_employee)
        self.assertEqual(rep_row['order_count'], 1)
        self.assertAlmostEqual(rep_row['sales_amount'], 250.0)
        self.assertAlmostEqual(rep_row['checkin_sale_amount'], 250.0)
        self.assertEqual(rep_row['by_outcome'], {'order': 1, 'follow_up': 1})
        self.assertEqual(self._row(summary, self.other_rep_employee)['order_count'], 0)
        self.assertAlmostEqual(summary['totals']['sales_amount'], 250.0)
        self.assertEqual(order.state, 'sale')  # still confirmed, simply not used as the source

    def test_performance_summary_period_filter(self):
        self._create_checkin(self.rep)
        old = self._create_checkin(self.rep, checkin_time=fields.Datetime.to_datetime('2020-01-15 03:00:00'))
        self.assertEqual(old.checkin_date, date(2020, 1, 15))
        summary = self.Checkin._get_performance_summary(self.month_start, self.month_end, employees=self.rep_employee)
        self.assertEqual(self._row(summary, self.rep_employee)['checkins'], 1)
        summary = self.Checkin._get_performance_summary(date(2020, 1, 1), date(2020, 1, 31), employees=self.rep_employee)
        self.assertEqual(self._row(summary, self.rep_employee)['checkins'], 1)
        self.assertEqual(summary['checkins'], old)
        # An employee without activity still gets a (zero) row when explicitly requested
        summary = self.Checkin._get_performance_summary(date(2020, 2, 1), date(2020, 2, 29), employees=self.rep_employee)
        self.assertEqual(self._row(summary, self.rep_employee)['checkins'], 0)
        # ... but not when the employees are derived from the activity
        summary = self.Checkin._get_performance_summary(date(2020, 2, 1), date(2020, 2, 29))
        self.assertNotIn(self.rep_employee, [row['employee'] for row in summary['employees']])

    def test_performance_summary_targets(self):
        self._create_activity()
        target = self._create_target(self.rep_employee, target_visits=10, target_sales_amount=1000.0)

        summary = self.Checkin._get_performance_summary(self.month_start, self.month_end, employees=self.employees)
        rep_row = self._row(summary, self.rep_employee)
        self.assertEqual(rep_row['target'], target)
        self.assertEqual(rep_row['target_visits'], 10)
        self.assertEqual(rep_row['target_sales_amount'], 1000.0)
        self.assertEqual(rep_row['achievement_visits'], 30.0)
        self.assertEqual(rep_row['target_status'], target.status)
        self.assertIn('achievement_rate', rep_row)
        other_row = self._row(summary, self.other_rep_employee)
        self.assertFalse(other_row['target'])
        self.assertNotIn('target_visits', other_row)
        self.assertEqual(summary['totals']['target_visits'], 10)
        self.assertEqual(summary['totals']['target_sales_amount'], 1000.0)

        # An employee with a target but no activity still appears in the derived employee list
        idle_target = self._create_target(self.manager_employee, target_visits=5)
        summary = self.Checkin._get_performance_summary(self.month_start, self.month_end)
        manager_row = self._row(summary, self.manager_employee)
        self.assertEqual(manager_row['checkins'], 0)
        self.assertEqual(manager_row['target'], idle_target)

        # Multi-month range: monthly targets do not apply
        summary = self.Checkin._get_performance_summary(
            self.month_start - relativedelta(months=1), self.month_end, employees=self.employees)
        rep_row = self._row(summary, self.rep_employee)
        self.assertFalse(rep_row['target'])
        self.assertNotIn('target_visits', rep_row)
        self.assertEqual(summary['totals']['target_visits'], 0)

        # Explicitly without targets
        summary = self.Checkin._get_performance_summary(
            self.month_start, self.month_end, employees=self.rep_employee, with_targets=False)
        self.assertNotIn('target_visits', self._row(summary, self.rep_employee))

    def test_performance_summary_non_manager_restriction(self):
        self._create_activity()
        Checkin = self.Checkin.with_user(self.rep)
        summary = Checkin._get_performance_summary(self.month_start, self.month_end)
        self.assertEqual([row['employee'] for row in summary['employees']], [self.rep_employee])
        self.assertEqual(summary['totals']['employees'], 1)
        self.assertEqual(summary['totals']['checkins'], 4)
        self.assertEqual(len(summary['checkins']), 4)
        self.assertTrue(all(c.employee_id == self.rep_employee for c in summary['checkins']))

        # Even when explicitly asking for someone else
        summary = Checkin._get_performance_summary(self.month_start, self.month_end, employees=self.employees)
        self.assertEqual([row['employee'] for row in summary['employees']], [self.rep_employee])
        summary = Checkin._get_performance_summary(
            self.month_start, self.month_end, employees=self.other_rep_employee)
        self.assertEqual(summary['employees'], [])
        self.assertEqual(summary['totals']['checkins'], 0)

        # A manager sees everyone
        summary = self.Checkin.with_user(self.manager)._get_performance_summary(
            self.month_start, self.month_end, employees=self.employees)
        self.assertEqual(len(summary['employees']), 2)
        self.assertEqual(summary['totals']['checkins'], 5)

    # ------------------------------------------------------------------
    # Wizard
    # ------------------------------------------------------------------
    def test_wizard_dates(self):
        wizard = self.Wizard.create({'period': 'last_month'})
        self.assertEqual((wizard.date_from, wizard.date_to),
                         self.Checkin._get_period_dates('last_month', today=self.today))
        wizard.period = 'this_week'
        self.assertEqual((wizard.date_from, wizard.date_to),
                         self.Checkin._get_period_dates('this_week', today=self.today))
        wizard.period = 'this_month'
        self.assertEqual((wizard.date_from, wizard.date_to), (self.month_start, self.month_end))
        self.assertEqual(wizard.company_id, self.company)
        self.assertTrue(wizard.include_details)

        custom = self.Wizard.create({'period': 'custom', 'date_from': date(2026, 1, 5), 'date_to': date(2026, 1, 20)})
        self.assertEqual(custom.date_from, date(2026, 1, 5))
        self.assertEqual(custom.date_to, date(2026, 1, 20))
        custom.write({'date_from': date(2026, 1, 10)})
        self.assertEqual((custom.date_from, custom.date_to), (date(2026, 1, 10), date(2026, 1, 20)))
        # A custom period without dates defaults to today
        custom_empty = self.Wizard.create({'period': 'custom'})
        self.assertEqual((custom_empty.date_from, custom_empty.date_to), (self.today, self.today))

        with self.assertRaises(ValidationError):
            self.Wizard.create({'period': 'custom', 'date_from': date(2026, 2, 10), 'date_to': date(2026, 2, 1)})
        with self.assertRaises(ValidationError):
            custom.write({'date_to': date(2026, 1, 1)})

    def test_wizard_report_data_and_domain(self):
        wizard = self.Wizard.create({
            'period': 'custom', 'date_from': date(2026, 3, 1), 'date_to': date(2026, 3, 31),
            'employee_ids': [Command.set(self.rep_employee.ids)], 'include_details': False,
        })
        data = wizard._get_report_data()
        self.assertEqual(data, {
            'date_from': '2026-03-01',
            'date_to': '2026-03-31',
            'employee_ids': self.rep_employee.ids,
            'company_id': self.company.id,
            'include_details': False,
        })
        domain = wizard._get_checkin_domain()
        self.assertIn(('employee_id', 'in', self.rep_employee.ids), domain)
        self.assertIn(('checkin_date', '>=', date(2026, 3, 1)), domain)
        self.assertIn(('checkin_date', '<=', date(2026, 3, 31)), domain)

        action = wizard.action_view_checkins()
        self.assertEqual(action['res_model'], 'checkinme.checkin')
        self.assertIn(('state', 'in', ('checked_in', 'done')), action['domain'])
        analysis = wizard.action_view_analysis()
        self.assertEqual(analysis['res_model'], 'checkinme.performance.report')
        self.assertIn(('employee_id', 'in', self.rep_employee.ids), analysis['domain'])

    def test_wizard_print_pdf(self):
        # As a non-admin user: for administrators of a company without a document layout,
        # report_action() first returns the layout configuration wizard instead.
        wizard = self.Wizard.with_user(self.manager).create({'period': 'this_month'})
        action = wizard.action_print_pdf()
        self.assertEqual(action['type'], 'ir.actions.report')
        self.assertEqual(action['report_name'], 'checkinme_sales_activity.report_checkinme_activity')
        self.assertEqual(action['report_type'], 'qweb-pdf')
        self.assertEqual(action['data']['date_from'], fields.Date.to_string(self.month_start))
        self.assertEqual(action['data']['date_to'], fields.Date.to_string(self.month_end))
        self.assertEqual(action['context']['active_ids'], wizard.ids)

    def test_wizard_send_telegram(self):
        self._create_activity()
        rep_wizard = self.Wizard.with_user(self.rep).create({'period': 'this_month', 'telegram_chat_id': TEST_CHAT_ID})
        with self.assertRaises(UserError):
            rep_wizard.action_send_telegram()

        self._configure_telegram()
        manager_wizard = self.Wizard.with_user(self.manager).create({'period': 'this_month'})
        self.assertEqual(manager_wizard.telegram_chat_id, TEST_CHAT_ID)
        with mock_telegram() as tg:
            result = manager_wizard.action_send_telegram()
        self.assertEqual(tg.methods, ['sendMessage'])
        self.assertEqual(tg.payloads[0]['chat_id'], TEST_CHAT_ID)
        self.assertIn('Sales Activity Report', tg.payloads[0]['text'])
        self.assertIn(self.rep_employee.name, tg.payloads[0]['text'])
        self.assertIn(self.other_rep_employee.name, tg.payloads[0]['text'])
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')

        # Restricted to some salespeople and sent to an explicit chat
        manager_wizard.write({'telegram_chat_id': '4242', 'employee_ids': [Command.set(self.other_rep_employee.ids)]})
        with mock_telegram() as tg:
            manager_wizard.action_send_telegram()
        self.assertEqual(tg.payloads[0]['chat_id'], '4242')
        self.assertIn(self.other_rep_employee.name, tg.payloads[0]['text'])
        self.assertNotIn(self.rep_employee.name, tg.payloads[0]['text'])

        # No chat id -> error, API failure -> error
        manager_wizard.telegram_chat_id = False
        with self.assertRaises(UserError):
            manager_wizard.action_send_telegram()
        manager_wizard.telegram_chat_id = TEST_CHAT_ID
        with mock_telegram(response={'ok': False, 'description': 'Forbidden'}), self.assertRaises(UserError):
            manager_wizard.action_send_telegram()

    # ------------------------------------------------------------------
    # QWeb rendering
    # ------------------------------------------------------------------
    def test_render_activity_report(self):
        self._create_activity()
        target = self._create_target(self.rep_employee, target_visits=10)
        wizard = self.Wizard.create({
            'period': 'this_month',
            'employee_ids': [Command.set(self.rep_employee.ids)],
        })
        Report = self.env['ir.actions.report']
        html, report_type = Report._render_qweb_html(
            'checkinme_sales_activity.action_report_checkinme_activity', wizard.ids, data=wizard._get_report_data())
        self.assertEqual(report_type, 'html')
        html = html.decode() if isinstance(html, bytes) else str(html)
        self.assertIn('Sales Activity Report', html)
        self.assertIn(self.rep_employee.name, html)
        self.assertNotIn(self.other_rep_employee.name, html)

        # The report model builds the summary from the wizard data
        values = self.env['report.checkinme_sales_activity.report_checkinme_activity']._get_report_values(
            wizard.ids, data=wizard._get_report_data())
        self.assertEqual(values['docs'], wizard)
        self.assertEqual(values['company'], self.company)
        self.assertEqual(values['summary']['date_from'], self.month_start)
        self.assertEqual(values['summary']['totals']['checkins'], 4)
        self.assertEqual(self._row(values['summary'], self.rep_employee)['target'], target)
        self.assertTrue(values['period_label'])

        # Without wizard data it defaults to the current month for everyone
        values = self.env['report.checkinme_sales_activity.report_checkinme_activity']._get_report_values([])
        self.assertEqual(values['summary']['date_from'], self.month_start)
        self.assertEqual(values['summary']['date_to'], self.today)
        self.assertIn(self.other_rep_employee, [row['employee'] for row in values['summary']['employees']])

    def test_render_checkin_report(self):
        checkin = self._create_checkin(self.rep, purpose='Quarterly review', notes='Talked about new price list')
        html, report_type = self.env['ir.actions.report']._render_qweb_html(
            'checkinme_sales_activity.action_report_checkinme_checkin', checkin.ids)
        self.assertEqual(report_type, 'html')
        html = html.decode() if isinstance(html, bytes) else str(html)
        self.assertIn(checkin.name, html)
        self.assertIn(self.rep_employee.name, html)
        # The report is bound to the check-in print menu
        report = self.env.ref('checkinme_sales_activity.action_report_checkinme_checkin')
        self.assertEqual(report.model, 'checkinme.checkin')
        self.assertEqual(report.binding_model_id.model, 'checkinme.checkin')
        self.assertEqual(report.binding_type, 'report')
        activity_report = self.env.ref('checkinme_sales_activity.action_report_checkinme_activity')
        self.assertEqual(activity_report.model, 'checkinme.report.wizard')
        self.assertFalse(activity_report.binding_model_id)

    # ------------------------------------------------------------------
    # Performance report (SQL view)
    # ------------------------------------------------------------------
    def test_performance_report_view(self):
        c1, c2, c3, c4, other = self._create_activity()
        cancelled = self._create_checkin(self.rep)
        cancelled.action_cancel()
        planned = self._create_checkin(self.rep, state='draft')
        c1.write({'outcome': 'order', 'sale_amount': 250.0})
        order = self._create_confirmed_order(self.rep, self.partner_geo, 100.0)
        quotation = self.env['sale.order'].create({
            'partner_id': self.partner_geo.id,
            'user_id': self.rep.id,
            'company_id': self.company.id,
            'order_line': [Command.create({'product_id': self.product.id, 'product_uom_qty': 1.0, 'price_unit': 50.0})],
        })
        self.env.flush_all()

        Report = self.env['checkinme.performance.report']
        checkin_rows = Report.search([('record_type', '=', 'checkin'), ('employee_id', 'in', self.employees.ids)])
        self.assertEqual(len(checkin_rows), 5)
        self.assertEqual(checkin_rows.mapped('checkin_id'), c1 | c2 | c3 | c4 | other)
        self.assertNotIn(cancelled, checkin_rows.mapped('checkin_id'))
        self.assertNotIn(planned, checkin_rows.mapped('checkin_id'))
        self.assertEqual(set(checkin_rows.mapped('date')), {self.today})
        self.assertEqual(set(checkin_rows.mapped('company_id').ids), {self.company.id})
        self.assertEqual(set(checkin_rows.mapped('currency_id').ids), {self.company.currency_id.id})
        row_c1 = checkin_rows.filtered(lambda r: r.checkin_id == c1)
        self.assertEqual(row_c1.employee_id, self.rep_employee)
        self.assertEqual(row_c1.user_id, self.rep)
        self.assertEqual(row_c1.partner_id, self.partner_geo)
        self.assertEqual(row_c1.checkin_type_id, self.type_visit)
        self.assertEqual(row_c1.state, 'checked_in')
        self.assertEqual(row_c1.location_status, 'verified')
        self.assertEqual(row_c1.outcome, 'order')
        self.assertEqual((row_c1.checkin_count, row_c1.visit_count, row_c1.verified_count, row_c1.new_customer_count),
                         (1, 1, 1, 1))
        self.assertAlmostEqual(row_c1.checkin_sale_amount, 250.0)
        self.assertEqual((row_c1.order_count, row_c1.order_amount), (0, 0.0))
        row_c4 = checkin_rows.filtered(lambda r: r.checkin_id == c4)
        self.assertEqual((row_c4.checkin_count, row_c4.visit_count, row_c4.verified_count, row_c4.new_customer_count),
                         (1, 0, 0, 1))

        sale_rows = Report.search([('record_type', '=', 'sale'), ('employee_id', 'in', self.employees.ids)])
        self.assertEqual(len(sale_rows), 1)
        self.assertEqual(sale_rows.sale_order_id, order)
        self.assertNotIn(quotation, sale_rows.mapped('sale_order_id'))
        self.assertEqual(sale_rows.employee_id, self.rep_employee)
        self.assertEqual(sale_rows.user_id, self.rep)
        self.assertEqual(sale_rows.partner_id, self.partner_geo)
        self.assertEqual(sale_rows.date, self.today)
        self.assertEqual((sale_rows.checkin_count, sale_rows.visit_count, sale_rows.order_count), (0, 0, 1))
        self.assertAlmostEqual(sale_rows.order_amount, 100.0)
        self.assertFalse(sale_rows.checkin_id)
        self.assertFalse(sale_rows.state)

        # Aggregates
        groups = Report._read_group(
            [('employee_id', '=', self.rep_employee.id)], [],
            ['checkin_count:sum', 'visit_count:sum', 'verified_count:sum', 'new_customer_count:sum',
             'order_count:sum', 'order_amount:sum', 'checkin_sale_amount:sum'])
        self.assertEqual(len(groups), 1)
        self.assertEqual(tuple(groups[0][:5]), (4, 3, 2, 2, 1))
        self.assertAlmostEqual(groups[0][5], 100.0)
        self.assertAlmostEqual(groups[0][6], 250.0)
        by_type = dict(Report._read_group([('employee_id', 'in', self.employees.ids)], ['record_type'], ['__count']))
        self.assertEqual(by_type, {'checkin': 5, 'sale': 1})
        by_employee = {
            employee: visits for employee, visits in Report._read_group(
                [('employee_id', 'in', self.employees.ids)], ['employee_id'], ['visit_count:sum'])
        }
        self.assertEqual(by_employee[self.rep_employee], 3)
        self.assertEqual(by_employee[self.other_rep_employee], 1)
        # Grouping per day / month works on the date column (used by the pivot views)
        by_month = Report._read_group(
            [('employee_id', 'in', self.employees.ids)], ['date:month'], ['checkin_count:sum'])
        self.assertEqual(len(by_month), 1)
        self.assertEqual(by_month[0][1], 5)

        # Linking the order to a check-in is reflected on the sale row
        order.checkinme_checkin_id = c1
        self.env.flush_all()
        self.env.invalidate_all()  # SQL report rows are not invalidated by writes on sale.order
        sale_row = Report.search([('record_type', '=', 'sale'), ('sale_order_id', '=', order.id)])
        self.assertEqual(sale_row.checkin_id, c1)
