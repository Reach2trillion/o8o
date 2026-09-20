# -*- coding: utf-8 -*-
from datetime import timedelta

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import tagged
from odoo.tools import mute_logger

from .common import (
    CheckinmeCommon, mock_telegram,
    CHECKIN_LAT, CHECKIN_LNG, MANAGER_CHAT_ID, TEST_BOT_TOKEN, TEST_CHAT_ID, TINY_PNG_B64,
)

TELEGRAM_LOGGER = 'odoo.addons.checkinme_sales_activity.models.checkinme_telegram'


@tagged('post_install', '-at_install')
class TestTelegram(CheckinmeCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Telegram = cls.env['checkinme.telegram']
        cls.Log = cls.env['checkinme.telegram.log']

    def setUp(self):
        super().setUp()
        # Safety net: no HTTP request can ever leave, even in tests that do not inspect calls.
        self.telegram = self._patch_telegram()

    def _assert_bot_url(self, url, method):
        self.assertEqual(url, 'https://api.telegram.org/bot%s/%s' % (TEST_BOT_TOKEN, method))

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def test_bot_token_fallback(self):
        self.assertFalse(self.Telegram._get_bot_token())
        self._set_param('send_by_telegram.bot_token', 'FALLBACK-TOKEN')
        self.assertEqual(self.Telegram._get_bot_token(), 'FALLBACK-TOKEN')
        self._set_param('checkinme.telegram_bot_token', '  OWN-TOKEN  ')
        self.assertEqual(self.Telegram._get_bot_token(), 'OWN-TOKEN')
        self._set_param('checkinme.telegram_bot_token', False)
        self.assertEqual(self.Telegram._get_bot_token(), 'FALLBACK-TOKEN')

    def test_default_chat_id(self):
        self.assertEqual(self.Telegram._get_default_chat_id(), '')
        self._set_param('checkinme.telegram_chat_id', ' %s ' % TEST_CHAT_ID)
        self.assertEqual(self.Telegram._get_default_chat_id(), TEST_CHAT_ID)

    def test_manager_chat_ids(self):
        self.assertEqual(self.manager_employee.checkinme_telegram_chat_id, MANAGER_CHAT_ID)
        # No default chat: only the manager's personal chat
        self.assertEqual(self.Telegram._get_manager_chat_ids(self.rep_employee), [MANAGER_CHAT_ID])
        self.assertEqual(self.Telegram._get_manager_chat_ids(self.other_rep_employee), [])
        # Default chat first, then the manager
        self._set_param('checkinme.telegram_chat_id', TEST_CHAT_ID)
        self.assertEqual(self.Telegram._get_manager_chat_ids(self.rep_employee), [TEST_CHAT_ID, MANAGER_CHAT_ID])
        self.assertEqual(self.Telegram._get_manager_chat_ids(self.other_rep_employee), [TEST_CHAT_ID])
        # Deduplicated when both are the same chat
        self._set_param('checkinme.telegram_chat_id', MANAGER_CHAT_ID)
        self.assertEqual(self.Telegram._get_manager_chat_ids(self.rep_employee), [MANAGER_CHAT_ID])
        # A manager who is also the salesperson is not notified twice
        self.assertEqual(self.Telegram._get_manager_chat_ids(self.manager_employee), [MANAGER_CHAT_ID])

    def test_escape_and_split(self):
        self.assertEqual(self.Telegram._escape('<b>x</b> & "y"'), '&lt;b&gt;x&lt;/b&gt; &amp; "y"')
        self.assertEqual(self.Telegram._escape(False), '')

        self.assertEqual(self.Telegram._split_message('short'), ['short'])
        line = 'x' * 50
        text = '\n'.join([line] * 300)  # ~15 kB
        chunks = self.Telegram._split_message(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4096)
            # split on line boundaries only: every chunk is made of complete lines
            self.assertTrue(all(part == line for part in chunk.split('\n')), chunk[:60])
        self.assertEqual('\n'.join(chunks), text)
        # A single over-long line is hard-split
        long_line = 'y' * 9000
        chunks = self.Telegram._split_message(long_line)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))
        self.assertEqual(''.join(chunks), long_line)

    # ------------------------------------------------------------------
    # Low level sending
    # ------------------------------------------------------------------
    def test_send_message_without_chat_id(self):
        self._configure_telegram(chat_id=False)
        with mock_telegram() as tg:
            ok, response = self.Telegram._send_message('', 'Hello nobody', message_type='test')
        self.assertFalse(ok)
        self.assertIn('description', response)
        self.assertFalse(tg.calls)
        log = self.Log.search([('message', '=', 'Hello nobody')])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.state, 'skipped')
        self.assertEqual(log.message_type, 'test')

    def test_send_message_without_token(self):
        with mock_telegram() as tg:
            ok, response = self.Telegram._send_message(TEST_CHAT_ID, 'Hello no token', message_type='test')
        self.assertFalse(ok)
        self.assertFalse(tg.calls)
        log = self.Log.search([('message', '=', 'Hello no token')])
        self.assertEqual(log.state, 'failed')
        self.assertIn('token', (log.response or '').lower())

    def test_send_message(self):
        self._configure_telegram()
        with mock_telegram() as tg:
            ok, response = self.Telegram._send_message(
                TEST_CHAT_ID, 'Hello <b>chat</b>', message_type='test', employee=self.rep_employee)
        self.assertTrue(ok)
        self.assertTrue(response['ok'])
        self.assertEqual(len(tg.calls), 1)
        url, kwargs = tg.calls[0]
        self._assert_bot_url(url, 'sendMessage')
        payload = kwargs['json']
        self.assertEqual(payload['chat_id'], TEST_CHAT_ID)
        self.assertEqual(payload['text'], 'Hello <b>chat</b>')
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertTrue(payload['disable_web_page_preview'])
        self.assertIn('timeout', kwargs)

        log = self.Log.search([('message', '=', 'Hello <b>chat</b>')])
        self.assertEqual(len(log), 1)
        self.assertEqual(log.state, 'sent')
        self.assertEqual(log.chat_id, TEST_CHAT_ID)
        self.assertEqual(log.method, 'sendMessage')
        self.assertEqual(log.message_type, 'test')
        self.assertEqual(log.employee_id, self.rep_employee)
        self.assertEqual(log.company_id, self.company)

    def test_send_long_message_in_chunks(self):
        self._configure_telegram()
        text = '\n'.join(['line %04d %s' % (i, 'z' * 40) for i in range(200)])
        self.assertGreater(len(text), 4096)
        with mock_telegram() as tg:
            ok, _response = self.Telegram._send_message(TEST_CHAT_ID, text, message_type='test')
        self.assertTrue(ok)
        self.assertGreater(len(tg.calls), 1)
        self.assertEqual('\n'.join(p['text'] for p in tg.payloads), text)

    def test_api_error_response(self):
        self._configure_telegram()
        with mock_telegram(response={'ok': False, 'description': 'Bad Request: chat not found'}) as tg:
            ok, response = self.Telegram._send_message(TEST_CHAT_ID, 'Hello error', message_type='test')
        self.assertFalse(ok)
        self.assertEqual(len(tg.calls), 1)
        log = self.Log.search([('message', '=', 'Hello error')])
        self.assertEqual(log.state, 'failed')
        self.assertIn('chat not found', log.response)

    def test_send_location(self):
        self._configure_telegram()
        with mock_telegram() as tg:
            ok, _response = self.Telegram._send_location(TEST_CHAT_ID, CHECKIN_LAT, CHECKIN_LNG, message_type='test')
        self.assertTrue(ok)
        url, kwargs = tg.calls[0]
        self._assert_bot_url(url, 'sendLocation')
        self.assertEqual(kwargs['json'], {'chat_id': TEST_CHAT_ID, 'latitude': CHECKIN_LAT, 'longitude': CHECKIN_LNG})
        log = self.Log.search([('method', '=', 'sendLocation'), ('chat_id', '=', TEST_CHAT_ID)], limit=1)
        self.assertEqual(log.state, 'sent')

    def test_send_photo(self):
        self._configure_telegram()
        with mock_telegram() as tg:
            ok, _response = self.Telegram._send_photo(
                TEST_CHAT_ID, TINY_PNG_B64, caption='Visit <photo>', message_type='test')
        self.assertTrue(ok)
        url, kwargs = tg.calls[0]
        self._assert_bot_url(url, 'sendPhoto')
        self.assertIn('files', kwargs)
        self.assertIn('photo', kwargs['files'])
        filename, content = kwargs['files']['photo'][:2]
        self.assertTrue(filename)
        self.assertTrue(content.startswith(b'\x89PNG'))
        self.assertEqual(kwargs['data']['chat_id'], TEST_CHAT_ID)
        self.assertEqual(kwargs['data']['caption'], 'Visit <photo>')
        self.assertNotIn('json', kwargs)
        log = self.Log.search([('method', '=', 'sendPhoto'), ('chat_id', '=', TEST_CHAT_ID)], limit=1)
        self.assertEqual(log.state, 'sent')
        # Invalid image data never reaches the API
        with mock_telegram() as tg:
            ok, response = self.Telegram._send_photo(TEST_CHAT_ID, '%%%not-base64%%%')
        self.assertFalse(ok)
        self.assertFalse(tg.calls)

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------
    def test_format_checkin_message(self):
        with mock_telegram():
            checkin = self._create_checkin(
                self.rep, notes='Discussed <b>pricing</b> & delivery', purpose='Present price list')
        text = self.Telegram._format_checkin_message(checkin)
        self.assertIn(checkin.name, text)
        self.assertIn(self.rep_employee.name, text)
        self.assertIn(self.partner_geo.name, text)
        self.assertIn('Google Maps', text)
        self.assertIn('href="%s"' % checkin.google_maps_url, text)
        self.assertIn('Present price list', text)
        self.assertIn('verified on site', text)
        self.assertIn('&lt;b&gt;pricing&lt;/b&gt; &amp; delivery', text)
        self.assertNotIn('<b>pricing</b>', text)
        self.assertIn('Check-in', text)
        self.assertIn('New customer', text)

        with mock_telegram():
            checkin.write({'outcome': 'order', 'sale_amount': 120.0})
            checkin.action_check_out()
        text = self.Telegram._format_checkin_message(checkin, event='checkout')
        self.assertIn('Check-out', text)
        self.assertIn('Order Taken', text)
        self.assertIn(self.rep_employee.name, text)

    def test_format_period_report(self):
        with mock_telegram():
            self._create_checkin(self.rep)
        today = self._today()
        summary = self.env['checkinme.checkin']._get_performance_summary(today, today)
        text = self.Telegram._format_period_report(summary, 'My <Report>')
        self.assertIn('My &lt;Report&gt;', text)
        self.assertIn(self.rep_employee.name, text)
        self.assertIn(self.company.name, text)
        # An empty period says so
        empty = self.env['checkinme.checkin']._get_performance_summary(
            today - timedelta(days=400), today - timedelta(days=400), employees=self.rep_employee)
        text = self.Telegram._format_period_report(empty, 'Empty')
        self.assertIn(self.rep_employee.name, text)
        self.assertIn('0', text)

    # ------------------------------------------------------------------
    # Check-in notifications
    # ------------------------------------------------------------------
    def test_checkin_notification(self):
        self._configure_telegram()
        with mock_telegram() as tg:
            checkin = self._create_checkin(self.rep)
        # Message + location pin for the managers' group and for the manager's personal chat
        self.assertEqual(tg.methods, ['sendMessage', 'sendLocation', 'sendMessage', 'sendLocation'])
        for url in tg.urls:
            self.assertTrue(url.startswith('https://api.telegram.org/bot%s/' % TEST_BOT_TOKEN), url)
        self.assertEqual([p['chat_id'] for p in tg.payloads],
                         [TEST_CHAT_ID, TEST_CHAT_ID, MANAGER_CHAT_ID, MANAGER_CHAT_ID])
        self.assertIn(self.rep_employee.name, tg.payloads[0]['text'])
        self.assertIn(self.partner_geo.name, tg.payloads[0]['text'])
        self.assertEqual(tg.payloads[0]['parse_mode'], 'HTML')
        self.assertAlmostEqual(tg.payloads[1]['latitude'], CHECKIN_LAT, places=5)
        self.assertAlmostEqual(tg.payloads[1]['longitude'], CHECKIN_LNG, places=5)

        self.assertTrue(checkin.telegram_notified)
        logs = self.Log.search([('checkin_id', '=', checkin.id)])
        self.assertEqual(len(logs), 4)
        self.assertEqual(set(logs.mapped('state')), {'sent'})
        self.assertEqual(set(logs.mapped('method')), {'sendMessage', 'sendLocation'})
        self.assertEqual(set(logs.mapped('message_type')), {'checkin'})
        self.assertEqual(logs.employee_id, self.rep_employee)
        self.assertEqual(logs.company_id, self.company)

    def test_checkin_notification_with_photo_single_chat(self):
        self._configure_telegram()
        self.manager_employee.checkinme_telegram_chat_id = False
        with mock_telegram() as tg:
            checkin = self._create_checkin(self.rep, photo=TINY_PNG_B64)
        self.assertEqual(tg.methods, ['sendMessage', 'sendLocation', 'sendPhoto'])
        self.assertIn('files', tg.calls[2][1])
        self.assertTrue(checkin.telegram_notified)
        # Location pin and photo can be switched off
        self._set_param('checkinme.send_location_pin', 'False')
        self._set_param('checkinme.send_photo', 'False')
        with mock_telegram() as tg:
            self._create_checkin(self.rep, photo=TINY_PNG_B64)
        self.assertEqual(tg.methods, ['sendMessage'])

    def test_checkout_notification(self):
        self._configure_telegram()
        self.manager_employee.checkinme_telegram_chat_id = False
        with mock_telegram():
            checkin = self._create_checkin(self.rep)
        with mock_telegram() as tg:
            checkin.write({'checkout_latitude': CHECKIN_LAT, 'checkout_longitude': CHECKIN_LNG})
            checkin.action_check_out()
        self.assertEqual(tg.methods, ['sendMessage', 'sendLocation'])
        self.assertIn('Check-out', tg.payloads[0]['text'])
        logs = self.Log.search([('checkin_id', '=', checkin.id), ('message_type', '=', 'checkout')])
        self.assertEqual(len(logs), 2)
        # Check-out notifications can be disabled independently
        self._set_param('checkinme.notify_checkout', 'False')
        with mock_telegram():
            other = self._create_checkin(self.rep)
        with mock_telegram() as tg:
            other.action_check_out()
        self.assertFalse(tg.calls)

    def test_checkin_notification_connection_error(self):
        self._configure_telegram()
        with mock_telegram(side_effect=requests.ConnectionError('boom')) as tg, mute_logger(TELEGRAM_LOGGER):
            checkin = self._create_checkin(self.rep)
        self.assertTrue(tg.calls)
        self.assertEqual(checkin.state, 'checked_in')
        self.assertFalse(checkin.telegram_notified)
        failed = self.Log.search([('checkin_id', '=', checkin.id), ('state', '=', 'failed')])
        self.assertTrue(failed)
        self.assertIn('boom', failed[0].response)
        self.assertFalse(self.Log.search([('checkin_id', '=', checkin.id), ('state', '=', 'sent')]))

    def test_checkin_notification_unexpected_error(self):
        self._configure_telegram()
        with mock_telegram(side_effect=RuntimeError('unexpected')), \
                mute_logger('odoo.addons.checkinme_sales_activity.models.checkinme_checkin'):
            checkin = self._create_checkin(self.rep)
        self.assertEqual(checkin.state, 'checked_in')
        self.assertFalse(checkin.telegram_notified)

    def test_checkin_notification_skipped_without_config(self):
        with mock_telegram() as tg:
            checkin = self._create_checkin(self.rep)
        self.assertFalse(tg.calls)
        self.assertFalse(checkin.telegram_notified)
        skipped = self.Log.search([('checkin_id', '=', checkin.id)])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped.state, 'skipped')
        self.assertEqual(skipped.message_type, 'checkin')
        # Token but no chat at all: skipped as well
        self._configure_telegram(chat_id=False)
        self.manager_employee.checkinme_telegram_chat_id = False
        with mock_telegram() as tg:
            checkin = self._create_checkin(self.rep)
        self.assertFalse(tg.calls)
        self.assertEqual(self.Log.search([('checkin_id', '=', checkin.id)]).mapped('state'), ['skipped'])

    def test_checkin_notification_disabled_and_forced(self):
        self._configure_telegram()
        self.manager_employee.checkinme_telegram_chat_id = False
        self._set_param('checkinme.notify_checkin', 'False')
        with mock_telegram() as tg:
            checkin = self._create_checkin(self.rep)
        self.assertFalse(tg.calls)
        self.assertFalse(checkin.telegram_notified)
        self.assertFalse(self.Log.search([('checkin_id', '=', checkin.id)]))

        with mock_telegram() as tg:
            result = checkin.with_user(self.manager).action_send_telegram()
        self.assertEqual(tg.methods, ['sendMessage', 'sendLocation'])
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')
        self.assertTrue(checkin.telegram_notified)

        # Forcing on a checked-out visit sends the check-out message
        self._set_param('checkinme.notify_checkout', 'False')
        with mock_telegram() as tg:
            checkin.action_check_out()
        self.assertFalse(tg.calls)
        with mock_telegram() as tg:
            checkin.with_user(self.manager).action_send_telegram()
        self.assertEqual(tg.methods, ['sendMessage'])
        self.assertIn('Check-out', tg.payloads[0]['text'])

    # ------------------------------------------------------------------
    # Period reports & crons
    # ------------------------------------------------------------------
    def test_send_period_report(self):
        self._configure_telegram()
        with mock_telegram():
            self._create_checkin(self.rep)
        with mock_telegram() as tg:
            ok, text = self.Telegram._send_period_report('today')
        self.assertTrue(ok)
        self.assertIn('Sales Activity Report', text)
        self.assertIn(self.rep_employee.name, text)
        self.assertEqual(set(tg.methods), {'sendMessage'})
        self.assertEqual({p['chat_id'] for p in tg.payloads}, {TEST_CHAT_ID})
        self.assertEqual('\n'.join(p['text'] for p in tg.payloads), text)
        log = self.Log.search([('message_type', '=', 'report'), ('chat_id', '=', TEST_CHAT_ID)], limit=1)
        self.assertEqual(log.state, 'sent')

        # Explicit chat, explicit period tuple, explicit employees and title
        today = self._today()
        with mock_telegram() as tg:
            ok, text = self.Telegram._send_period_report(
                (today, today), chat_id='42', employees=self.other_rep_employee, message_type='daily')
        self.assertTrue(ok)
        self.assertIn('Daily Sales Activity Report', text)
        self.assertIn(self.other_rep_employee.name, text)
        self.assertNotIn(self.rep_employee.name, text)
        self.assertEqual(tg.payloads[0]['chat_id'], '42')
        with mock_telegram() as tg:
            ok, text = self.Telegram._send_period_report('this_month', message_type='monthly', title='Custom Title')
        self.assertIn('Custom Title', text)
        self.assertNotIn('Monthly Sales Activity Report', text)

    def test_cron_reports(self):
        self._configure_telegram()
        with mock_telegram():
            self._create_checkin(self.rep)

        self._set_param('checkinme.daily_report', 'False')
        with mock_telegram() as tg:
            self.assertFalse(self.Telegram._cron_daily_report())
        self.assertFalse(tg.calls)

        self._set_param('checkinme.daily_report', 'True')
        with mock_telegram() as tg:
            self.assertTrue(self.Telegram._cron_daily_report())
        self.assertTrue(tg.calls)
        self.assertEqual(set(tg.methods), {'sendMessage'})
        self.assertIn('Daily Sales Activity Report', tg.payloads[0]['text'])

        with mock_telegram() as tg:
            self.assertTrue(self.Telegram._cron_weekly_report())
        self.assertIn('Weekly Sales Activity Report', tg.payloads[0]['text'])
        with mock_telegram() as tg:
            self.assertTrue(self.Telegram._cron_monthly_report())
        self.assertIn('Monthly Sales Activity Report', tg.payloads[0]['text'])

        # Without a chat id the scheduled reports are skipped silently
        self._set_param('checkinme.telegram_chat_id', False)
        with mock_telegram() as tg:
            self.assertFalse(self.Telegram._cron_daily_report())
        self.assertFalse(tg.calls)

    def test_action_test_connection(self):
        with mock_telegram() as tg, self.assertRaises(UserError):
            self.Telegram.action_test_connection()
        self.assertFalse(tg.calls)

        self._configure_telegram()
        with mock_telegram() as tg:
            bot_name = self.Telegram.action_test_connection()
        self.assertEqual(bot_name, 'test_bot')
        self.assertEqual(tg.methods, ['getMe', 'sendMessage'])
        self._assert_bot_url(tg.urls[0], 'getMe')
        self.assertEqual(tg.payloads[1]['chat_id'], TEST_CHAT_ID)
        self.assertIn('test_bot', tg.payloads[1]['text'])
        log = self.Log.search([('message_type', '=', 'test'), ('chat_id', '=', TEST_CHAT_ID)], limit=1)
        self.assertEqual(log.state, 'sent')

        # Explicit chat id
        with mock_telegram() as tg:
            self.Telegram.action_test_connection(chat_id='777')
        self.assertEqual(tg.payloads[1]['chat_id'], '777')

        # Valid token but no chat id configured
        self._set_param('checkinme.telegram_chat_id', False)
        with mock_telegram() as tg, self.assertRaises(UserError):
            self.Telegram.action_test_connection()
        self.assertEqual(tg.methods, ['getMe'])

        # Rejected token
        with mock_telegram(response={'ok': False, 'description': 'Unauthorized'}) as tg, \
                self.assertRaises(UserError):
            self.Telegram.action_test_connection(chat_id=TEST_CHAT_ID)
        self.assertEqual(tg.methods, ['getMe'])

    def test_settings_actions(self):
        self._configure_telegram()
        Settings = self.env['res.config.settings']
        settings = Settings.create({})
        self.assertEqual(settings.checkinme_telegram_bot_token, TEST_BOT_TOKEN)
        self.assertEqual(settings.checkinme_telegram_chat_id, TEST_CHAT_ID)
        self.assertTrue(settings.checkinme_notify_checkin)
        self.assertTrue(settings.checkinme_require_gps)
        self.assertEqual(settings.checkinme_max_distance, 500)
        self.assertEqual(settings.checkinme_sales_source, 'sale_order')

        with mock_telegram() as tg:
            result = settings.action_checkinme_test_telegram()
        self.assertEqual(tg.methods, ['getMe', 'sendMessage'])
        self.assertEqual(result['tag'], 'display_notification')
        with mock_telegram() as tg:
            result = settings.action_checkinme_send_daily_report()
        self.assertEqual(tg.methods, ['sendMessage'])
        self.assertEqual(result['params']['type'], 'success')

        # Unchecking a boolean is stored explicitly (and disables the matching cron)
        settings.write({'checkinme_daily_report': False, 'checkinme_require_gps': False})
        settings.set_values()
        self.assertEqual(self._get_param('checkinme.daily_report'), 'False')
        self.assertEqual(self._get_param('checkinme.require_gps'), 'False')
        self.assertFalse(self.env.ref('checkinme_sales_activity.ir_cron_checkinme_daily_report').active)
        self.assertFalse(Settings.create({}).checkinme_daily_report)
        settings.write({'checkinme_daily_report': True})
        settings.set_values()
        self.assertTrue(self.env.ref('checkinme_sales_activity.ir_cron_checkinme_daily_report').active)

    def test_log_autovacuum(self):
        self._configure_telegram()
        with mock_telegram():
            self.Telegram._send_message(TEST_CHAT_ID, 'Old message', message_type='test')
            self.Telegram._send_message(TEST_CHAT_ID, 'Recent message', message_type='test')
        old = self.Log.search([('message', '=', 'Old message')])
        recent = self.Log.search([('message', '=', 'Recent message')])
        self.assertTrue(old and recent)
        self.env.flush_all()
        self.cr.execute("UPDATE checkinme_telegram_log SET create_date = %s WHERE id = %s",
                        (fields.Datetime.now() - timedelta(days=100), old.id))
        self.Log.invalidate_model(['create_date'])
        self.Log._gc_telegram_logs()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
