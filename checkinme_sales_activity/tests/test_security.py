# -*- coding: utf-8 -*-
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import new_test_user, tagged
from odoo.tools import mute_logger

from .common import CheckinmeCommon, MAIL_CONTEXT, TZ, mock_telegram

ACL_LOGGER = 'odoo.addons.base.models.ir_model'
RULE_LOGGER = 'odoo.addons.base.models.ir_rule'


@tagged('post_install', '-at_install')
class TestSecurity(CheckinmeCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with mock_telegram():
            cls.rep_checkin = cls._create_checkin(cls.rep)
            cls.other_checkin = cls._create_checkin(cls.other_rep)
        cls.rep_target = cls._create_target(cls.rep_employee, target_visits=10)
        cls.other_target = cls._create_target(cls.other_rep_employee, target_visits=10)
        cls.env.flush_all()

    def setUp(self):
        super().setUp()
        self.telegram = self._patch_telegram()

    def _checkins(self, user):
        return self.env['checkinme.checkin'].with_user(user).search(
            [('id', 'in', (self.rep_checkin | self.other_checkin).ids)])

    def _targets(self, user):
        return self.env['checkinme.target'].with_user(user).search(
            [('id', 'in', (self.rep_target | self.other_target).ids)])

    def _report_checkins(self, user):
        rows = self.env['checkinme.performance.report'].with_user(user).search([('record_type', '=', 'checkin')])
        return rows.mapped('checkin_id')

    # ------------------------------------------------------------------
    # Check-ins
    # ------------------------------------------------------------------
    def test_rep_creates_own_checkin(self):
        checkin = self._create_checkin(self.rep)
        self.assertEqual(checkin.user_id, self.rep)
        self.assertEqual(checkin.employee_id, self.rep_employee)
        self.assertEqual(checkin.create_uid, self.rep)
        # ... and can update it
        checkin.write({'purpose': 'Updated purpose', 'notes': 'Some notes'})
        self.assertEqual(checkin.purpose, 'Updated purpose')
        # Search as the salesperson only returns own records
        found = self.env['checkinme.checkin'].with_user(self.rep).search([])
        self.assertIn(checkin, found)
        self.assertTrue(all(c.user_id == self.rep for c in found))

    def test_rep_cannot_access_other_checkin(self):
        self.assertEqual(self._checkins(self.rep), self.rep_checkin)
        Checkin = self.env['checkinme.checkin'].with_user(self.rep)
        self.assertFalse(Checkin.search([('id', '=', self.other_checkin.id)]))
        self.assertEqual(Checkin.search_count([('id', '=', self.other_checkin.id)]), 0)
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self.other_checkin.with_user(self.rep).read(['name'])
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self.other_checkin.with_user(self.rep).check_access('read')
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self.other_checkin.with_user(self.rep).write({'purpose': 'Hacked'})
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self.other_checkin.with_user(self.rep).action_cancel()
        # The other salesperson is in the same situation the other way round
        self.assertEqual(self._checkins(self.other_rep), self.other_checkin)

    def test_rep_cannot_unlink_own_checkin(self):
        with self.assertRaises(UserError):
            self.rep_checkin.with_user(self.rep).unlink()
        self.assertTrue(self.rep_checkin.exists())
        # Planned visits of their own may be deleted, other people's may not
        planned = self._create_checkin(self.rep, state='draft')
        self.assertTrue(planned.with_user(self.rep).unlink())
        self.assertFalse(planned.exists())
        other_planned = self._create_checkin(self.other_rep, state='draft')
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            other_planned.with_user(self.rep).unlink()
        self.assertTrue(other_planned.exists())

    def test_manager_sees_all(self):
        self.assertEqual(self._checkins(self.manager), self.rep_checkin | self.other_checkin)
        self.assertEqual(self.other_checkin.with_user(self.manager).read(['name'])[0]['name'], self.other_checkin.name)
        self.other_checkin.with_user(self.manager).write({'purpose': 'Reviewed by manager'})
        self.assertEqual(self.other_checkin.purpose, 'Reviewed by manager')
        self.assertEqual(self._targets(self.manager), self.rep_target | self.other_target)
        self.assertEqual(self._report_checkins(self.manager) & (self.rep_checkin | self.other_checkin),
                         self.rep_checkin | self.other_checkin)
        # Managers can delete check-ins
        checkin = self._create_checkin(self.other_rep)
        self.assertTrue(checkin.with_user(self.manager).unlink())
        self.assertFalse(checkin.exists())
        # ... and create check-ins for other salespeople
        checkin = self._create_checkin(self.manager, employee_id=self.rep_employee.id)
        self.assertEqual(checkin.user_id, self.rep)

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------
    def test_target_access(self):
        self.assertEqual(self._targets(self.rep), self.rep_target)
        self.assertEqual(self.rep_target.with_user(self.rep).read(['target_visits'])[0]['target_visits'], 10)
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self.other_target.with_user(self.rep).read(['target_visits'])
        # Read-only for salespeople
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            self.env['checkinme.target'].with_user(self.rep).create({
                'employee_id': self.rep_employee.id, 'year': 2030, 'month': '1', 'target_visits': 5,
            })
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            self.rep_target.with_user(self.rep).write({'target_visits': 1})
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            self.rep_target.with_user(self.rep).unlink()
        # Managers manage targets
        target = self.env['checkinme.target'].with_user(self.manager).create({
            'employee_id': self.other_rep_employee.id, 'year': 2030, 'month': '1', 'target_visits': 5,
        })
        target.write({'target_visits': 6})
        self.assertEqual(target.target_visits, 6)
        self.assertTrue(target.unlink())

    # ------------------------------------------------------------------
    # Telegram logs
    # ------------------------------------------------------------------
    def test_telegram_log_access(self):
        Log = self.env['checkinme.telegram.log']
        log = Log.create({'chat_id': '1', 'message': 'security test', 'message_type': 'test'})
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            Log.with_user(self.rep).search([])
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            log.with_user(self.rep).read(['message'])
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            Log.with_user(self.rep).create({'chat_id': '1', 'message': 'x'})
        self.assertIn(log, Log.with_user(self.manager).search([]))
        self.assertEqual(log.with_user(self.manager).read(['message'])[0]['message'], 'security test')
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            log.with_user(self.manager).write({'message': 'edited'})
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            Log.with_user(self.manager).create({'chat_id': '1', 'message': 'x'})
        self.assertTrue(log.with_user(self.manager).unlink())

    # ------------------------------------------------------------------
    # Direct reports
    # ------------------------------------------------------------------
    def test_team_lead_sees_direct_reports(self):
        team_lead = new_test_user(
            self.env, login='checkinme_team_lead', name='Team Lead',
            groups='base.group_user,checkinme_sales_activity.group_checkinme_user',
            context=MAIL_CONTEXT, tz=TZ)
        lead_employee = self.env['hr.employee'].with_context(**MAIL_CONTEXT).create({
            'name': 'Team Lead', 'user_id': team_lead.id, 'company_id': self.company.id, 'tz': TZ,
        })
        # Nothing visible before the reporting line exists
        self.assertFalse(self._checkins(team_lead))
        self.assertFalse(self._targets(team_lead))

        self.rep_employee.parent_id = lead_employee
        self.env.flush_all()
        self.assertEqual(self._checkins(team_lead), self.rep_checkin)
        self.assertEqual(self.rep_checkin.with_user(team_lead).read(['name'])[0]['name'], self.rep_checkin.name)
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self.other_checkin.with_user(team_lead).read(['name'])
        # Targets and the performance analysis follow the same rule
        self.assertEqual(self._targets(team_lead), self.rep_target)
        with self.assertRaises(AccessError), mute_logger(RULE_LOGGER):
            self.other_target.with_user(team_lead).read(['name'])
        report_checkins = self._report_checkins(team_lead)
        self.assertIn(self.rep_checkin, report_checkins)
        self.assertNotIn(self.other_checkin, report_checkins)
        # The summary used by the reports is restricted the same way
        today = self._today()
        summary = self.env['checkinme.checkin'].with_user(team_lead)._get_performance_summary(today, today)
        self.assertEqual([row['employee'] for row in summary['employees']], [self.rep_employee])
        # The team lead still cannot delete the report's check-in
        with self.assertRaises(UserError):
            self.rep_checkin.with_user(team_lead).unlink()

    def test_performance_report_rules(self):
        self.env.flush_all()
        rep_rows = self._report_checkins(self.rep)
        self.assertIn(self.rep_checkin, rep_rows)
        self.assertNotIn(self.other_checkin, rep_rows)
        other_rows = self._report_checkins(self.other_rep)
        self.assertIn(self.other_checkin, other_rows)
        self.assertNotIn(self.rep_checkin, other_rows)
        Report = self.env['checkinme.performance.report']
        self.assertEqual(Report.with_user(self.rep).search_count(
            [('checkin_id', '=', self.other_checkin.id)]), 0)
        # Read-only model for everybody
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            Report.with_user(self.manager).create({})
        self.assertIn(self.other_checkin, self._report_checkins(self.manager))

    # ------------------------------------------------------------------
    # Activity types
    # ------------------------------------------------------------------
    def test_activity_type_access(self):
        Type = self.env['checkinme.activity.type']
        types = Type.with_user(self.rep).search([])
        self.assertIn(self.type_visit, types)
        self.assertEqual(self.type_visit.with_user(self.rep).read(['name'])[0]['name'], self.type_visit.name)
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            Type.with_user(self.rep).create({'name': 'Rep Type', 'code': 'REPX'})
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            self.type_visit.with_user(self.rep).write({'name': 'Renamed'})
        with self.assertRaises(AccessError), mute_logger(ACL_LOGGER):
            self.type_visit.with_user(self.rep).unlink()
        new_type = Type.with_user(self.manager).create({'name': 'Manager Type', 'code': 'MGRX'})
        self.assertTrue(new_type.exists())
        new_type.write({'counts_as_visit': False})
        self.assertFalse(new_type.counts_as_visit)

    # ------------------------------------------------------------------
    # Wizard
    # ------------------------------------------------------------------
    def test_wizard_access(self):
        wizard = self.env['checkinme.report.wizard'].with_user(self.rep).create({'period': 'this_month'})
        self.assertTrue(wizard.date_from)
        action = wizard.action_print_pdf()
        self.assertEqual(action['type'], 'ir.actions.report')
        with self.assertRaises(UserError):
            wizard.action_send_telegram()
