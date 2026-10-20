# -*- coding: utf-8 -*-
"""Tests of the ABJ app push notifications: device registration, queue, Web Push sending,
FCM sending, business hooks, mail mirror, settings and the test button."""
import base64
import json
from datetime import timedelta
from unittest.mock import patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from markupsafe import Markup

from odoo import Command, api, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.checkinme_sales_activity.tools import fcm
from odoo.addons.checkinme_sales_activity.models import checkinme_push
from odoo.addons.checkinme_sales_activity.models.checkinme_push import (
    MAX_ATTEMPTS, MAX_BODY_LENGTH, MAX_DEVICES_PER_USER, CheckinmePush,
)

from .common import MAIL_CONTEXT
from .push_common import (
    ACCESS_TOKEN, FCM_PROJECT, FCM_SEND_URL, FakeResponse, PushCommon,
    decrypt_web_push, verify_rs256, verify_vapid_authorization,
)

PUSH_LOGGER = 'odoo.addons.checkinme_sales_activity.models.checkinme_push'
CHECKIN_LOGGER = 'odoo.addons.checkinme_sales_activity.models.checkinme_checkin'
WEB_PUSH_LOGGER = 'odoo.addons.checkinme_sales_activity.tools.web_push'
FCM_LOGGER = 'odoo.addons.checkinme_sales_activity.tools.fcm'


def fcm_error(status, code, message, error_code=None):
    error = {'code': status, 'message': message, 'status': code}
    if error_code:
        error['details'] = [{'@type': 'type.googleapis.com/google.firebase.fcm.v1.FcmError',
                             'errorCode': error_code}]
    return FakeResponse(status, {'error': error})


@tagged('post_install', '-at_install')
class TestPushDevices(PushCommon):

    def test_register_and_unregister(self):
        Device = self.Device.with_user(self.rep)
        result = Device.register_device(' tok-1 ', platform='android', device_name='Redmi 13', app_version='1.0.0')
        device = self.Device.sudo().browse(result['id'])
        self.assertEqual(result, {'id': device.id, 'active': True})
        self.assertEqual((device.token, device.user_id, device.partner_id), ('tok-1', self.rep, self.rep.partner_id))
        self.assertEqual((device.platform, device.device_name, device.app_version), ('android', 'Redmi 13', '1.0.0'))
        self.assertTrue(device.last_seen)
        self.assertEqual(device.display_name, 'Redmi 13')
        # another user cannot unregister it
        self.assertTrue(self.Device.with_user(self.other_rep).unregister_device('tok-1'))
        self.assertTrue(device.active)
        # its owner can (on logout)
        self.assertTrue(Device.unregister_device('tok-1'))
        self.assertFalse(device.active)
        # registering again reactivates it
        result = Device.register_device('tok-1', platform='weird', device_name='')
        self.assertEqual(result, {'id': device.id, 'active': True})
        self.assertTrue(device.active)
        self.assertEqual(device.platform, 'android')
        self.assertEqual(device.display_name, 'tok-1')

    def test_reassign_to_new_user(self):
        result = self.Device.with_user(self.rep).register_device('shared-phone-token', device_name='Shop phone')
        result2 = self.Device.with_user(self.other_rep).register_device('shared-phone-token', device_name='Shop phone')
        self.assertEqual(result2['id'], result['id'])
        device = self.Device.sudo().browse(result['id'])
        self.assertEqual(device.user_id, self.other_rep)
        self.assertEqual(self.Device.sudo().with_context(active_test=False).search_count(
            [('token', '=', 'shared-phone-token')]), 1)

    def test_invalid_tokens_and_access(self):
        Device = self.Device.with_user(self.rep)
        with self.assertRaises(UserError):
            Device.register_device('   ')
        with self.assertRaises(UserError):
            Device.register_device('x' * 4097)
        portal = new_test_user(self.env, login='checkinme_test_portal', groups='base.group_portal',
                               context=MAIL_CONTEXT)
        with self.assertRaises(AccessError):
            self.Device.with_user(portal).register_device('portal-token')
        self.assertFalse(self.Device.sudo().search([('token', '=', 'portal-token')]))
        # salespeople never read the device table directly; managers do, without the token
        device = self._add_native_device(self.rep, token='managed-token')
        with self.assertRaises(AccessError):
            self.Device.with_user(self.rep).search([])
        self.assertEqual(self.Device.with_user(self.manager).search([('user_id', '=', self.rep.id)]), device)
        with self.assertRaises(AccessError):
            device.with_user(self.manager).read(['token'])
        with self.assertRaises(AccessError):
            self.Device.with_user(self.manager).search([('token', '=', 'managed-token')])
        self.assertEqual(device.with_user(self.manager).display_name, 'Galaxy A15')

    def test_managers_cannot_redirect_devices(self):
        """server-security-1: a manager may archive a phone, never give it to another user."""
        result = self.Device.with_user(self.manager).register_device('mgr-phone-token', device_name='Mgr phone')
        device = self.Device.with_user(self.manager).browse(result['id'])
        for vals in ({'user_id': self.other_rep.id}, {'token': 'other-token'}, {'device_name': 'x', 'active': True}):
            with self.assertRaises(AccessError):
                api.call_kw(self.Device.with_user(self.manager), 'write', [[device.id], vals], {})
        self.assertEqual(device.sudo().user_id, self.manager)
        self.assertEqual(device.sudo().token, 'mgr-phone-token')
        # a direct message to other_rep never reaches the manager's phone
        self._configure_fcm()
        self._add_native_device(self.other_rep, token='other-rep-token')
        chat = self.env['discuss.channel'].with_user(self.rep).channel_get([self.other_rep.partner_id.id])
        chat.with_user(self.rep).message_post(body='PRIVATE 9999', message_type='comment',
                                              subtype_xmlid='mail.mt_comment')
        self.Push._cron_send_push_queue()
        tokens = [kwargs['json']['message']['token'] for _url, kwargs in self.session.calls_to(FCM_SEND_URL)]
        self.assertEqual(tokens, ['other-rep-token'])

    def test_managers_only_see_devices_of_their_companies(self):
        company_b = self.env['res.company'].create({'name': 'ABJ Siem Reap'})
        user_b = new_test_user(self.env, login='checkinme_test_user_b', groups='base.group_user',
                               company_id=company_b.id, company_ids=[Command.set(company_b.ids)],
                               context=MAIL_CONTEXT)
        device_b = self._add_native_device(user_b, token='company-b-token')
        device_a = self._add_native_device(self.rep, token='company-a-token')
        found = self.Device.with_user(self.manager).search([('id', 'in', (device_a | device_b).ids)])
        self.assertEqual(found, device_a)

    def test_archived_phone_stays_archived(self):
        """requirements-archived-device-reactivates: a phone archived by a manager (lost phone) is not
        reactivated by the app of the same user."""
        Device = self.Device.with_user(self.rep)
        device = self.Device.sudo().browse(Device.register_device('lost-phone', device_name='Redmi 13')['id'])
        device.with_user(self.manager).action_archive()
        self.assertEqual((device.active, device.blocked), (False, True))
        self.assertEqual(Device.register_device('lost-phone', device_name='Redmi 13'),
                         {'id': device.id, 'active': False})
        self.assertFalse(device.active)
        self.assertFalse(self.Push._notify(self.rep, 'Hello', 'Nobody', native_only=True))
        # unarchived by the manager: it works again
        device.with_user(self.manager).action_unarchive()
        self.assertEqual((device.active, device.blocked), (True, False))
        device.with_user(self.manager).action_archive()
        # another user logs in on the phone: it is theirs now
        self.assertEqual(self.Device.with_user(self.other_rep).register_device('lost-phone'),
                         {'id': device.id, 'active': True})
        self.assertEqual((device.user_id, device.blocked), (self.other_rep, False))
        # an owner's logout is not a block
        self.Device.with_user(self.other_rep).unregister_device('lost-phone')
        self.assertEqual((device.active, device.blocked), (False, False))
        self.assertTrue(self.Device.with_user(self.other_rep).register_device('lost-phone')['active'])

    def test_active_devices_per_user_are_capped(self):
        """server-security-4: one user cannot register an unbounded number of phones."""
        Device = self.Device.with_user(self.rep)
        for index in range(MAX_DEVICES_PER_USER + 2):
            Device.register_device('flood-token-%d' % index)
            # each registration is older than the next one
            self.env.cr.execute("UPDATE checkinme_push_device SET last_seen = last_seen - interval '1 hour' "
                                "WHERE user_id = %s", (self.rep.id,))
            self.env.invalidate_all()
        active = self.Device.sudo().search([('user_id', '=', self.rep.id)])
        self.assertEqual(len(active), MAX_DEVICES_PER_USER)
        tokens = active.mapped('token')
        self.assertNotIn('flood-token-0', tokens)
        self.assertNotIn('flood-token-1', tokens)
        self.assertIn('flood-token-%d' % (MAX_DEVICES_PER_USER + 1), tokens)
        # an old phone that registers again is the newest one
        Device.register_device('flood-token-0')
        self.assertIn('flood-token-0', self.Device.sudo().search([('user_id', '=', self.rep.id)]).mapped('token'))


@tagged('post_install', '-at_install')
class TestPushQueue(PushCommon):

    def test_no_device_short_circuit(self):
        triggers = self._cron_triggers()
        message = self.Push._notify(self.rep, 'Hello', 'Nobody listens')
        self.assertFalse(message)
        self.assertFalse(self._messages())
        self.assertEqual(self._cron_triggers(), triggers)
        # an Android app device only counts for a native-only message
        self._add_web_device(self.rep)
        self.assertFalse(self.Push._notify(self.rep, 'Hello', 'App only', native_only=True))
        self.assertTrue(self.Push._notify(self.rep, 'Hello', 'Browser'))

    def test_notify_queues_and_triggers(self):
        self._add_web_device(self.rep)
        self._add_native_device(self.other_rep)
        visit = self._create_checkin(self.rep)
        triggers = self._cron_triggers()
        message = self.Push._notify(self.rep | self.other_rep | self.manager, ' Title ', 'Body',
                                    event='planned_visit', record=visit)
        self.assertEqual(len(message), 1)
        self.assertEqual(message.user_ids, self.rep | self.other_rep, "the manager has no device")
        self.assertEqual((message.name, message.body, message.event, message.state),
                         ('Title', 'Body', 'planned_visit', 'pending'))
        self.assertEqual((message.res_model, message.res_id), ('checkinme.checkin', visit.id))
        self.assertEqual(message.url, '/odoo/abj/%d' % visit.id)
        self.assertEqual(message.company_id, self.company)
        self.assertEqual(self._cron_triggers(), triggers + 1)

    def test_recipients_and_urls(self):
        self._add_web_device(self.rep)
        self.rep.sudo().active = False
        self.assertFalse(self.Push._notify(self.rep, 'Archived', 'user'))
        self.rep.sudo().active = True
        # default links
        self.assertEqual(self.Push._notify(self.rep, 'x', 'y').url, '/odoo/abj')
        self.assertEqual(self.Push._notify(self.rep, 'x', 'y', record=self.partner_geo).url,
                         '/odoo/res.partner/%d' % self.partner_geo.id)
        # only same-origin relative links
        self.assertEqual(self.Push._notify(self.rep, 'x', 'y', url='/odoo/abj/5').url, '/odoo/abj/5')
        for url in ('https://evil.example/phish', '//evil.example/x', 'javascript:alert(1)', '/\\evil'):
            self.assertEqual(self.Push._notify(self.rep, 'x', 'y', url=url).url, '/odoo/abj', url)
        # unknown event
        self.assertEqual(self.Push._notify(self.rep, 'x', 'y', event='nope').event, 'other')

    @mute_logger(PUSH_LOGGER)
    def test_notify_never_raises(self):
        self._add_web_device(self.rep)
        with patch.object(type(self.env['checkinme.push.message']), 'create', side_effect=ValueError('boom')):
            message = self.Push._notify(self.rep, 'Hello', 'Broken')
        self.assertFalse(message)
        # the transaction is still usable
        self.assertTrue(self.Push._notify(self.rep, 'Hello', 'Works again'))


@tagged('post_install', '-at_install')
class TestWebPushSending(PushCommon):

    def test_cron_sends_web_push(self):
        device_ok, browser_key, auth = self._add_web_device(self.rep, 'https://push.example.com/send/ok')
        device_gone, _key, _auth = self._add_web_device(self.rep, 'https://push.example.com/send/gone')
        device_404, _key, _auth = self._add_web_device(self.manager, 'https://push.example.com/send/missing')
        visit = self._create_checkin(self.rep)

        def handler(url, kwargs):
            if url.endswith('/gone'):
                return FakeResponse(410, text='expired')
            if url.endswith('/missing'):
                return FakeResponse(404, text='not found')
            return None
        self.session.handler = handler
        message = self.Push._notify(self.rep | self.manager, 'New visit planned', 'ផ្សារ ABC · 20 Oct 2026 09:00',
                                    event='planned_visit', record=visit)
        with mute_logger(WEB_PUSH_LOGGER):
            self.Push._cron_send_push_queue()

        self.assertEqual(sorted(self.session.urls()), sorted([
            'https://push.example.com/send/ok', 'https://push.example.com/send/gone',
            'https://push.example.com/send/missing']))
        self.assertTrue(self.session.closed)
        url, kwargs = self.session.calls_to('https://push.example.com/send/ok')[0]
        headers = kwargs['headers']
        self.assertEqual(headers['TTL'], '86400')
        self.assertEqual(headers['Urgency'], 'high')
        self.assertEqual(headers['Content-Encoding'], 'aes128gcm')
        claims = verify_vapid_authorization(headers['Authorization'], self.vapid_public_key)
        self.assertEqual(claims['aud'], 'https://push.example.com')
        self.assertEqual(claims['sub'], 'https://erp.abj.example')
        self.assertGreater(claims['exp'], 0)
        payload = decrypt_web_push(kwargs['data'], browser_key, auth)
        self.assertEqual(payload, {
            'title': 'New visit planned',
            'options': {
                'body': 'ផ្សារ ABC · 20 Oct 2026 09:00',
                'icon': '/checkinme_sales_activity/static/img/abj-192.png',
                'badge': '/checkinme_sales_activity/static/img/abj-badge-96.png',
                'tag': 'abj-planned_visit-%d' % visit.id,
                'data': {'abj': 1, 'abj_url': '/odoo/abj/%d' % visit.id,
                         'model': 'checkinme.checkin', 'res_id': visit.id},
            },
        })
        # expired subscriptions are removed, the result is logged
        self.assertTrue(device_ok.exists())
        self.assertFalse(device_gone.exists())
        self.assertFalse(device_404.exists())
        self.assertEqual(message.state, 'partial')
        self.assertEqual((message.web_push_count, message.native_count), (1, 0))
        self.assertIn('2 expired browser subscription(s) removed', message.error)
        self.assertTrue(message.sent_date)
        # nothing left to send
        self.session.calls.clear()
        self.Push._cron_send_push_queue()
        self.assertFalse(self.session.calls)

    def test_ttl_per_event_and_payload_defaults(self):
        _device, browser_key, auth = self._add_web_device(self.rep)
        message = self.Push._notify(self.rep, 'ABJ test notification', 'Hi', event='test')
        result = self.Push._send_message(message)
        self.assertEqual(result, {'web': 1, 'native': 0, 'devices': 1, 'state': 'sent', 'error': ''})
        _url, kwargs = self.session.calls[0]
        self.assertEqual(kwargs['headers']['TTL'], '3600')
        payload = decrypt_web_push(kwargs['data'], browser_key, auth)
        # without a record: the default model keeps mail's service worker working, the tag is unique
        self.assertEqual(payload['options']['data'], {'abj': 1, 'abj_url': '/odoo/abj',
                                                      'model': 'checkinme.checkin', 'res_id': 0})
        self.assertEqual(payload['options']['tag'], 'abj-test-%d' % message.id)

    def test_long_body_is_truncated(self):
        _device, browser_key, auth = self._add_web_device(self.rep)
        message = self.Push._notify(self.rep, 'Long', 'សួស្តី ' * 2000)
        self.Push._send_message(message)
        payload = decrypt_web_push(self.session.calls[0][1]['data'], browser_key, auth)
        self.assertTrue(payload['options']['body'].startswith('សួស្តី'))
        self.assertLess(len(json.dumps(payload).encode()), 4096)

    @mute_logger(WEB_PUSH_LOGGER)
    def test_push_service_errors(self):
        device, _key, _auth = self._add_web_device(self.rep)
        self.session.handler = lambda url, kwargs: FakeResponse(500, text='Internal error')
        message = self.Push._notify(self.rep, 'Hello', 'Server down')
        self.Push._cron_send_push_queue()
        self.assertEqual(message.state, 'failed')
        self.assertIn('500', message.error)
        self.assertTrue(device.exists(), "a server error must never delete the subscription")
        # transport error
        self.session.handler = lambda url, kwargs: requests.ConnectionError('unreachable')
        message = self.Push._notify(self.rep, 'Hello', 'Offline')
        self.Push._cron_send_push_queue()
        self.assertEqual(message.state, 'failed')
        self.assertTrue(device.exists())

    def test_missing_vapid_keys(self):
        device, _key, _auth = self._add_web_device(self.rep)
        message = self.Push._notify(self.rep, 'Hello', 'No keys')
        self._set_param('mail.web_push_vapid_private_key', False)
        self.Push._cron_send_push_queue()
        self.assertFalse(self.session.calls)
        self.assertEqual(message.state, 'failed')
        self.assertIn('VAPID', message.error)
        self.assertTrue(device.exists())
        # the keys are never generated by the push service
        self.assertFalse(self._get_param('mail.web_push_vapid_private_key'))

    def test_recipient_without_device_anymore(self):
        device, _key, _auth = self._add_web_device(self.rep)
        message = self.Push._notify(self.rep, 'Hello', 'Gone before sending')
        device.unlink()
        self.Push._cron_send_push_queue()
        self.assertEqual(message.state, 'no_device')

    def test_queue_limit_retrigger_and_gc(self):
        self._add_web_device(self.rep)
        messages = self.Message.browse()
        for index in range(3):
            messages |= self.Push._notify(self.rep, 'Message %d' % index, 'Body')
        old = self.Push._notify(self.rep, 'Old', 'Body')
        old.state = 'sent'
        self.env.cr.execute("UPDATE checkinme_push_message SET create_date = %s WHERE id = %s",
                            (fields.Datetime.now() - timedelta(days=31), old.id))
        old.invalidate_recordset()
        triggers = self._cron_triggers()
        self.Push._cron_send_push_queue(limit=2)
        self.assertEqual(messages.sorted('id').mapped('state'), ['sent', 'sent', 'pending'])
        self.assertEqual(self._cron_triggers(), triggers + 1, "more messages remain: the cron is retriggered")
        self.assertFalse(old.exists(), "messages older than 30 days are garbage collected")
        self.Push._cron_send_push_queue(limit=2)
        self.assertEqual(messages.mapped('state'), ['sent'] * 3)

    @mute_logger(PUSH_LOGGER)
    def test_one_failing_message_does_not_block_the_queue(self):
        self._add_web_device(self.rep)
        first = self.Push._notify(self.rep, 'First', 'Body')
        second = self.Push._notify(self.rep, 'Second', 'Body')
        original = CheckinmePush._send_message

        def flaky(model, message, session=None):
            if message == first:
                raise RuntimeError('unexpected')
            return original(model, message, session=session)
        with patch.object(CheckinmePush, '_send_message', flaky):
            self.Push._cron_send_push_queue()
        self.assertEqual(first.state, 'failed')
        self.assertIn('unexpected', first.error)
        self.assertEqual(second.state, 'sent')

    def test_retry_button(self):
        self._add_web_device(self.rep)
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        message.write({'state': 'failed', 'error': 'old error', 'attempt_count': 1})
        with self.assertRaises(AccessError):
            message.with_user(self.rep).action_retry()
        message.with_user(self.manager).action_retry()
        self.assertEqual((message.state, message.error, message.attempt_count), ('pending', False, 0))
        self.Push._cron_send_push_queue()
        self.assertEqual((message.state, message.attempt_count), ('sent', 1))
        # a message already sent is not sent again
        triggers = self._cron_triggers()
        message.with_user(self.manager).action_retry()
        self.assertEqual(message.state, 'sent')
        self.assertEqual(self._cron_triggers(), triggers)

    def test_retry_respects_the_company_rule(self):
        """server-security-5: the ids given to action_retry go through the record rules."""
        company_b = self.env['res.company'].create({'name': 'ABJ Siem Reap'})
        self._add_web_device(self.rep)
        message = self.Push._notify(self.rep, 'B notification', 'b body')
        message.write({'company_id': company_b.id, 'state': 'failed'})
        Message = self.Message.with_user(self.manager)
        self.assertFalse(Message.search([('id', '=', message.id)]))
        with self.assertRaises(AccessError):
            api.call_kw(Message, 'action_retry', [[message.id]], {})
        self.assertEqual(message.state, 'failed')

    def test_push_log_access(self):
        self._add_web_device(self.rep)
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.assertEqual(self.Message.with_user(self.manager).search([('id', '=', message.id)]), message)
        with self.assertRaises(AccessError):
            self.Message.with_user(self.rep).search([])
        # copies of Odoo messages: administrators only (server-security-2)
        odoo_message = self.Push._notify(self.rep, 'Chan Sophea', 'PRIVATE', event='odoo_message')
        self.assertFalse(self.Message.with_user(self.manager).search([('id', '=', odoo_message.id)]))
        with self.assertRaises(AccessError):
            odoo_message.with_user(self.manager).read(['body'])
        with self.assertRaises(AccessError):
            odoo_message.with_user(self.manager).action_retry()
        admin = self.env.ref('base.user_admin')
        self.assertEqual(self.Message.with_user(admin).search([('id', '=', odoo_message.id)]), odoo_message)

    def test_attempts_and_interrupted_messages(self):
        """server-security-4: a message that keeps killing the worker is given up, the others go."""
        self._add_web_device(self.rep)
        stuck = self.Push._notify(self.rep, 'Stuck', 'Body')
        stuck.attempt_count = MAX_ATTEMPTS  # the worker was killed while sending it, 3 times
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual(stuck.state, 'failed')
        self.assertIn('interrupted', stuck.error)
        self.assertEqual((message.state, message.attempt_count), ('sent', 1))
        self.assertEqual(len(self.session.calls), 1, "the stuck message is not sent again")

    def test_time_budget(self):
        self._add_web_device(self.rep)
        messages = self.Push._notify(self.rep, 'First', 'Body') | self.Push._notify(self.rep, 'Second', 'Body')
        triggers = self._cron_triggers()
        with patch.object(checkinme_push, 'QUEUE_TIME_BUDGET', -1):
            self.Push._cron_send_push_queue()
        self.assertEqual(messages.mapped('state'), ['sent', 'pending'], "at least one message per run")
        self.assertEqual(self._cron_triggers(), triggers + 1, "the next run sends the rest")
        self.Push._cron_send_push_queue()
        self.assertEqual(messages.mapped('state'), ['sent', 'sent'])

    @mute_logger(WEB_PUSH_LOGGER)
    def test_invalid_endpoints_are_never_called(self):
        """server-security-3: the server only posts to https push services on public host names."""
        bad = [self._add_web_device(self.rep, endpoint)[0] for endpoint in (
            'http://push.example.com/send/1', 'https://127.0.0.1:8069/web', 'https://[::1]/x',
            'https://10.0.0.5/internal', 'https://localhost/x', 'https://intranet/x', 'https://db.internal/x',
            'https://push.example.com:99999/x')]
        good, _key, _auth = self._add_web_device(self.rep, 'https://fcm.googleapis.com/fcm/send/abc')
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual(self.session.urls(), ['https://fcm.googleapis.com/fcm/send/abc'])
        self.assertTrue(good.exists())
        self.assertFalse(any(device.exists() for device in bad))
        self.assertEqual((message.state, message.web_push_count), ('partial', 1))


@tagged('post_install', '-at_install')
class TestFcmSending(PushCommon):

    def test_fcm_send_and_oauth_assertion(self):
        self._configure_fcm()
        device = self._add_native_device(self.rep, token='fcm-token-rep')
        visit = self._create_checkin(self.rep)
        message = self.Push._notify(self.rep, 'New visit planned', 'ផ្សារ ABC', event='planned_visit', record=visit)
        self.Push._cron_send_push_queue()
        self.assertEqual(self.session.urls(), [fcm.TOKEN_URL, FCM_SEND_URL])
        # OAuth2 JWT bearer grant, RS256 signed with the service account key
        _url, kwargs = self.session.calls[0]
        self.assertEqual(kwargs['data']['grant_type'], 'urn:ietf:params:oauth:grant-type:jwt-bearer')
        self.assertEqual(kwargs['timeout'], 10)
        header, claims = verify_rs256(kwargs['data']['assertion'], self.rsa_key.public_key())
        self.assertEqual(header, {'alg': 'RS256', 'typ': 'JWT', 'kid': 'test-key-id-1'})
        self.assertEqual(claims['iss'], self.service_account['client_email'])
        self.assertEqual(claims['scope'], 'https://www.googleapis.com/auth/firebase.messaging')
        self.assertEqual(claims['aud'], 'https://oauth2.googleapis.com/token')
        self.assertEqual(claims['exp'] - claims['iat'], 3600)
        # the FCM v1 message
        _url, kwargs = self.session.calls[1]
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer %s' % ACCESS_TOKEN)
        self.assertEqual(kwargs['timeout'], 10)
        self.assertEqual(kwargs['json'], {'message': {
            'token': 'fcm-token-rep',
            'notification': {'title': 'New visit planned', 'body': 'ផ្សារ ABC'},
            'data': {'abj_url': '/odoo/abj/%d' % visit.id, 'abj_event': 'planned_visit',
                     'abj_model': 'checkinme.checkin', 'abj_res_id': str(visit.id)},
            'android': {'priority': 'HIGH', 'ttl': '86400s',
                        'notification': {'channel_id': 'abj_default', 'tag': 'abj-planned_visit-%d' % visit.id}},
        }})
        self.assertEqual(message.state, 'sent')
        self.assertEqual((message.web_push_count, message.native_count), (0, 1))
        self.assertTrue(device.active)

    def test_access_token_is_cached(self):
        self._configure_fcm()
        self._add_native_device(self.rep)
        self._add_native_device(self.rep)
        self.Push._notify(self.rep, 'First', 'Body')
        self.Push._notify(self.rep, 'Second', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual(len(self.session.calls_to(fcm.TOKEN_URL)), 1)
        self.assertEqual(len(self.session.calls_to(FCM_SEND_URL)), 4)

    def test_expired_token_cache_is_refreshed(self):
        client = fcm.FcmClient(self.service_account, dbname='db1')
        self.assertEqual(client.get_access_token(self.session), ACCESS_TOKEN)
        self.assertEqual(client.get_access_token(self.session), ACCESS_TOKEN)
        self.assertEqual(len(self.session.calls), 1)
        # less than 5 minutes left: refreshed
        token, expires_at = fcm._token_cache[client.cache_key]
        fcm._token_cache[client.cache_key] = (token, expires_at - 3400)
        client.get_access_token(self.session)
        self.assertEqual(len(self.session.calls), 2)
        # another database never shares the token
        fcm.FcmClient(self.service_account, dbname='db2').get_access_token(self.session)
        self.assertEqual(len(self.session.calls), 3)

    def test_401_retries_once_with_a_new_token(self):
        self._configure_fcm()
        self._add_native_device(self.rep)
        answers = [FakeResponse(401, {'error': {'code': 401, 'status': 'UNAUTHENTICATED', 'message': 'expired'}})]

        def handler(url, kwargs):
            if url == FCM_SEND_URL and answers:
                return answers.pop(0)
            return None
        self.session.handler = handler
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual(self.session.urls(), [fcm.TOKEN_URL, FCM_SEND_URL, fcm.TOKEN_URL, FCM_SEND_URL])
        self.assertEqual(message.state, 'sent')
        # two 401 in a row: no endless loop
        answers.extend([FakeResponse(401, {'error': {'status': 'UNAUTHENTICATED', 'message': 'bad'}})] * 2)
        self.session.calls.clear()
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual(len(self.session.calls_to(FCM_SEND_URL)), 2)
        self.assertEqual(message.state, 'failed')
        self.assertIn('UNAUTHENTICATED', message.error)

    def test_unregistered_tokens_are_deactivated(self):
        self._configure_fcm()
        cases = {
            'tok-unregistered': (fcm_error(404, 'NOT_FOUND', 'Requested entity was not found.', 'UNREGISTERED'), False),
            'tok-mismatch': (fcm_error(403, 'PERMISSION_DENIED', 'SenderId mismatch', 'SENDER_ID_MISMATCH'), False),
            'tok-invalid': (fcm_error(400, 'INVALID_ARGUMENT',
                                      'The registration token is not a valid FCM registration token',
                                      'INVALID_ARGUMENT'), False),
            'tok-bad-payload': (fcm_error(400, 'INVALID_ARGUMENT', "Invalid value at 'message.data[0].value'",
                                          'INVALID_ARGUMENT'), True),
            'tok-quota': (fcm_error(429, 'RESOURCE_EXHAUSTED', 'Quota exceeded', 'QUOTA_EXCEEDED'), True),
            'tok-unavailable': (fcm_error(503, 'UNAVAILABLE', 'Service unavailable', 'UNAVAILABLE'), True),
            'tok-internal': (FakeResponse(500, text='<html>oops</html>'), True),
        }
        devices = {token: self._add_native_device(self.rep, token=token) for token in cases}

        def handler(url, kwargs):
            if url == FCM_SEND_URL:
                return cases[kwargs['json']['message']['token']][0]
            return None
        self.session.handler = handler
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        with mute_logger(FCM_LOGGER):
            self.Push._cron_send_push_queue()
        for token, (_response, still_active) in cases.items():
            self.assertEqual(devices[token].active, still_active, token)
        self.assertEqual(message.state, 'failed')
        self.assertIn('UNREGISTERED', message.error)
        self.assertIn('QUOTA_EXCEEDED', message.error)
        self.assertIn('HTTP_500', message.error)
        # inactive devices are not targeted anymore
        self.session.calls.clear()
        self.Push._notify(self.rep, 'Hello', 'Again')
        self.Push._cron_send_push_queue()
        self.assertEqual(len(self.session.calls_to(FCM_SEND_URL)), 4)

    def test_oauth_and_network_failures(self):
        self._configure_fcm()
        device = self._add_native_device(self.rep)
        self.session.handler = lambda url, kwargs: FakeResponse(
            400, {'error': 'invalid_grant', 'error_description': 'Invalid JWT Signature.'}) \
            if url == fcm.TOKEN_URL else None
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual(message.state, 'failed')
        self.assertIn('INVALID_GRANT', message.error)
        self.assertTrue(device.active)
        self.assertFalse(self.session.calls_to(FCM_SEND_URL))
        self.session.handler = lambda url, kwargs: requests.ConnectionError('no route to host')
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual(message.state, 'failed')
        self.assertIn('NETWORK_ERROR', message.error)
        self.assertTrue(device.active)

    def test_not_configured(self):
        device = self._add_native_device(self.rep)
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertFalse(self.session.calls)
        self.assertEqual(message.state, 'failed')
        self.assertIn('Firebase service account is not configured', message.error)
        self.assertTrue(device.active)

    def test_web_and_native_together(self):
        self._configure_fcm()
        self._add_web_device(self.rep)
        self._add_native_device(self.rep)
        message = self.Push._notify(self.rep, 'Hello', 'Body')
        self.Push._cron_send_push_queue()
        self.assertEqual((message.state, message.web_push_count, message.native_count), ('sent', 1, 1))

    def test_long_message_fits_in_fcm_limit(self):
        """server-correctness-1: FCM refuses messages over 4096 bytes (Khmer: 3 bytes a character)."""
        self._configure_fcm()
        self._add_native_device(self.rep, token='rep-app-token')
        visit = self._create_checkin(self.rep)
        visit.with_user(self.manager).message_post(
            body='សូមទូរស័ព្ទទៅអតិថិជនវិញ ' * 400, partner_ids=self.rep.partner_id.ids,
            message_type='comment', subtype_xmlid='mail.mt_comment')
        message = self._messages(event='odoo_message')
        self.assertLessEqual(len(message.body), MAX_BODY_LENGTH)
        self.Push._cron_send_push_queue()
        payload = self.session.calls_to(FCM_SEND_URL)[0][1]['json']
        # under the limit both in UTF-8 and as the escaped JSON sent by requests
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode()), 4096)
        self.assertLess(len(json.dumps(payload).encode()), 4096)
        body = payload['message']['notification']['body']
        self.assertTrue(body.startswith('សូមទូរស័ព្ទ'))
        self.assertTrue(body.endswith('…'))
        self.assertLessEqual(len(body.encode()), fcm.MAX_BODY_BYTES)
        self.assertEqual(message.state, 'sent')
        # the helper cuts on a character boundary
        self.assertEqual(fcm.clip_utf8('ABC', 3), 'ABC')
        self.assertEqual(fcm.clip_utf8('កខគឃ', 9), 'កខ…')
        self.assertEqual(fcm.clip_utf8(None, 10), '')
        title = fcm.FcmClient(self.service_account).build_message('t', 'ក' * 300, '')['message']['notification']['title']
        self.assertLessEqual(len(title.encode()), fcm.MAX_TITLE_BYTES)

    def test_parse_service_account(self):
        info = fcm.parse_service_account(json.dumps(self.service_account).encode())
        self.assertEqual(info['project_id'], FCM_PROJECT)
        self.assertEqual(fcm.parse_service_account(self.service_account), self.service_account)
        bad_values = [
            b'\xff\xfe not text',
            'not json',
            '[1, 2]',
            json.dumps(dict(self.service_account, type='authorized_user')),
            json.dumps({'project_info': {'project_id': 'x'}, 'client': []}),  # google-services.json
            json.dumps(dict(self.service_account, client_email='')),
            json.dumps({key: value for key, value in self.service_account.items() if key != 'project_id'}),
            json.dumps(dict(self.service_account, private_key='-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n')),
        ]
        ec_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        bad_values.append(json.dumps(dict(self.service_account, private_key=ec_key)))
        for value in bad_values:
            with self.assertRaises(ValueError, msg=repr(value)[:80]):
                fcm.parse_service_account(value)

    def test_should_deactivate(self):
        self.assertTrue(fcm.should_deactivate('UNREGISTERED'))
        self.assertTrue(fcm.should_deactivate('SENDER_ID_MISMATCH'))
        self.assertTrue(fcm.should_deactivate('INVALID_ARGUMENT', 'The registration token is not a valid FCM registration token'))
        self.assertFalse(fcm.should_deactivate('INVALID_ARGUMENT', 'Invalid JSON payload'))
        for code in ('QUOTA_EXCEEDED', 'UNAVAILABLE', 'INTERNAL', 'HTTP_500', 'NETWORK_ERROR', None):
            self.assertFalse(fcm.should_deactivate(code, 'registration token'))


@tagged('post_install', '-at_install')
class TestPushHooks(PushCommon):

    def _manager_checkin_model(self):
        return self.env['checkinme.checkin'].with_user(self.manager).with_context(**MAIL_CONTEXT)

    def test_planned_visit_by_manager(self):
        self._add_web_device(self.rep)
        visit = self._manager_checkin_model().create({
            'employee_id': self.rep_employee.id, 'partner_id': self.partner_geo.id,
            'checkin_type_id': self.type_visit.id, 'state': 'draft',
            'checkin_time': '2026-10-21 02:30:00',  # 09:30 in Phnom Penh
        })
        message = self._messages(event='planned_visit')
        self.assertEqual(len(message), 1)
        self.assertEqual(message.user_ids, self.rep)
        self.assertEqual(message.name, 'New visit planned')
        self.assertEqual(message.body, 'Phnom Penh Mart · 21 Oct 2026 09:30')
        self.assertEqual(message.url, '/odoo/abj/%d' % visit.id)
        # without customer: the activity type
        self._manager_checkin_model().create({
            'employee_id': self.rep_employee.id, 'checkin_type_id': self.type_other.id, 'state': 'draft',
            'partner_id': False, 'checkin_time': '2026-10-21 02:30:00',
        })
        self.assertEqual(self._messages(event='planned_visit')[-1].body,
                         '%s · 21 Oct 2026 09:30' % self.type_other.name)

    def test_planned_visit_in_the_user_language(self):
        """C5: the date of the push follows the salesperson's language."""
        self.env['res.lang']._activate_lang('km_KH')
        self.rep.partner_id.lang = 'km_KH'
        self._add_web_device(self.rep)
        self._manager_checkin_model().create({
            'employee_id': self.rep_employee.id, 'partner_id': self.partner_geo.id,
            'checkin_type_id': self.type_visit.id, 'state': 'draft',
            'checkin_time': '2026-10-21 02:30:00',  # 09:30 in Phnom Penh
        })
        self.assertEqual(self._messages(event='planned_visit').body, 'Phnom Penh Mart · 21 តុលា 2026 09:30')

    def test_planned_visit_self_created_or_disabled(self):
        self._add_web_device(self.rep)
        self._create_checkin(self.rep, state='draft')
        self.assertFalse(self._messages(event='planned_visit'), "own plans are not notified")
        self._create_checkin(self.rep)  # a check-in is not a planned visit
        self.assertFalse(self._messages())
        self._set_param('checkinme.push_planned_visit', 'False')
        self._manager_checkin_model().create({
            'employee_id': self.rep_employee.id, 'partner_id': self.partner_geo.id,
            'checkin_type_id': self.type_visit.id, 'state': 'draft',
        })
        self.assertFalse(self._messages(event='planned_visit'))

    def test_planned_visit_reassigned(self):
        self._add_web_device(self.rep)
        self._add_web_device(self.other_rep)
        visit = self._create_checkin(self.rep, state='draft')
        visit.with_user(self.manager).write({'purpose': 'Price list'})
        self.assertFalse(self._messages())
        visit.with_user(self.manager).write({'employee_id': self.other_rep_employee.id})
        message = self._messages(event='planned_visit')
        self.assertEqual(message.user_ids, self.other_rep)
        # not for a visit already checked in
        checked_in = self._create_checkin(self.rep)
        checked_in.with_user(self.manager).write({'employee_id': self.other_rep_employee.id})
        self.assertEqual(len(self._messages(event='planned_visit')), 1)

    @mute_logger(CHECKIN_LOGGER, PUSH_LOGGER)
    def test_hooks_never_break_the_business_flow(self):
        self._add_web_device(self.rep)
        with patch.object(CheckinmePush, '_notify', side_effect=RuntimeError('push down')):
            visit = self._manager_checkin_model().create({
                'employee_id': self.rep_employee.id, 'partner_id': self.partner_geo.id,
                'checkin_type_id': self.type_visit.id, 'state': 'draft',
            })
        self.assertEqual(visit.state, 'draft')

    def test_team_checkin_push(self):
        self._add_web_device(self.manager)
        visit = self._create_checkin(self.rep)
        self.assertFalse(self._messages(), "off by default: Telegram covers the managers")
        visit.action_check_out()
        self.assertFalse(self._messages())

        self._set_param('checkinme.push_team_checkin', 'True')
        visit = self._create_checkin(self.rep)
        message = self._messages(event='team_checkin')
        self.assertEqual(message.user_ids, self.manager)
        self.assertEqual(message.name, 'Sok Dara checked in')
        self.assertIn('Phnom Penh Mart', message.body)
        self.assertIn('Verified On Site', message.body)
        self.assertEqual(message.url, '/odoo/abj/%d' % visit.id)
        visit.write({'outcome': 'order'})
        visit.action_check_out()
        message = self._messages(event='team_checkout')
        self.assertEqual(message.name, 'Sok Dara checked out')
        self.assertIn('Order Taken', message.body)
        # the manager's own visits: nobody above him
        self._create_checkin(self.manager)
        self.assertEqual(len(self._messages(event='team_checkin')), 1)

    def test_team_checkin_only_for_managers_who_can_read(self):
        self._set_param('checkinme.push_team_checkin', 'True')
        department = self.env['hr.department'].create({'name': 'Phnom Penh Sales',
                                                       'manager_id': self.other_rep_employee.id})
        self.rep_employee.department_id = department
        self.rep_employee.parent_id = self.manager_employee  # the department sets its manager by default
        self._add_web_device(self.manager)
        self._add_web_device(self.other_rep)
        self._create_checkin(self.rep)
        message = self._messages(event='team_checkin')
        self.assertEqual(message.user_ids, self.manager,
                         "the department manager cannot open the visit (not a Sale Check manager)")

    def test_team_checkin_manager_without_language(self):
        """server-correctness-2: one notification per manager, even when one has no language."""
        self._set_param('checkinme.push_team_checkin', 'True')
        department = self.env['hr.department'].create({'name': 'Phnom Penh Sales',
                                                       'manager_id': self.other_rep_employee.id})
        self.rep_employee.department_id = department
        self.rep_employee.parent_id = self.manager_employee
        self.other_rep.write({'groups_id': [Command.link(
            self.env.ref('checkinme_sales_activity.group_checkinme_manager').id)]})
        self.manager.partner_id.lang = 'en_US'
        self.other_rep.partner_id.lang = False
        self.assertFalse(self.other_rep.lang)
        self._add_web_device(self.manager)
        self._add_web_device(self.other_rep)
        # checked in from a request in English
        self.env['checkinme.checkin'].with_user(self.rep).with_context(MAIL_CONTEXT, lang='en_US').create({
            'employee_id': self.rep_employee.id, 'partner_id': self.partner_geo.id,
            'checkin_type_id': self.type_visit.id, 'latitude': 11.5565, 'longitude': 104.9283})
        messages = self._messages(event='team_checkin')
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages.user_ids, self.manager | self.other_rep)

    def test_followup_reminder_across_companies(self):
        """server-correctness-3: one reminder per salesperson, with the follow-ups of all companies
        (two reminders with the same tag would replace each other on the phone)."""
        company_b = self.env['res.company'].create({'name': 'ABJ Siem Reap'})
        company_b.partner_id.tz = 'Asia/Phnom_Penh'
        self.rep.write({'company_ids': [Command.link(company_b.id)]})
        employee_b = self.env['hr.employee'].create({
            'name': 'Sok Dara', 'user_id': self.rep.id, 'company_id': company_b.id, 'tz': 'Asia/Phnom_Penh'})
        shop_b = self.env['res.partner'].create({'name': 'Siem Reap Shop', 'is_company': True})
        today = self._today()
        self._add_web_device(self.rep)
        visit_a = self._create_checkin(self.rep)
        visit_a.write({'next_action_date': today})
        visit_a.action_check_out()
        visit_b = self.env['checkinme.checkin'].with_context(**MAIL_CONTEXT).create({
            'employee_id': employee_b.id, 'company_id': company_b.id, 'partner_id': shop_b.id,
            'checkin_type_id': self.type_visit.id, 'latitude': 11.5565, 'longitude': 104.9283,
            'next_action_date': today})
        visit_b.action_check_out()
        self.env['checkinme.checkin']._cron_push_followup_reminders()
        message = self._messages(event='followup_reminder')
        self.assertEqual(len(message), 1)
        self.assertEqual(message.user_ids, self.rep)
        self.assertEqual(message.name, '2 follow-ups today')
        self.assertEqual(message.body, 'Phnom Penh Mart, Siem Reap Shop')

    def test_followup_reminders(self):
        today = self._today()
        self._add_web_device(self.rep)
        self._add_web_device(self.other_rep)
        partners = [self.partner_geo, self.partner_nogeo] + [
            self.env['res.partner'].create({'name': 'ហាង %d' % index, 'is_company': True}) for index in range(3)]
        for partner in partners:
            visit = self._create_checkin(self.rep, partner_id=partner.id)
            visit.write({'next_action_date': today})
            visit.action_check_out()
        open_visit = self._create_checkin(self.rep, partner_id=partners[0].id)
        open_visit.write({'next_action_date': today})  # not checked out: ignored
        tomorrow = self._create_checkin(self.other_rep)
        tomorrow.write({'next_action_date': today + timedelta(days=1)})
        tomorrow.action_check_out()

        self.env['checkinme.checkin']._cron_push_followup_reminders()
        message = self._messages(event='followup_reminder')
        self.assertEqual(len(message), 1)
        self.assertEqual(message.user_ids, self.rep)
        self.assertEqual(message.name, '5 follow-ups today')
        self.assertEqual(message.body, 'Phnom Penh Mart, Kampot Corner Store, ហាង 0 +2')
        self.assertEqual(message.url, '/odoo/abj')
        self.assertEqual(message.tag, 'abj-followup-%s' % today)

        self._set_param('checkinme.push_followup_reminder', 'False')
        self.assertFalse(self.env['checkinme.checkin']._cron_push_followup_reminders())
        self.assertEqual(len(self._messages(event='followup_reminder')), 1)

    def test_single_followup_wording(self):
        self._add_web_device(self.rep)
        visit = self._create_checkin(self.rep)
        visit.write({'next_action_date': self._today()})
        visit.action_check_out()
        self.env['checkinme.checkin']._cron_push_followup_reminders()
        self.assertEqual(self._messages(event='followup_reminder').name, '1 follow-up today')


@tagged('post_install', '-at_install')
class TestPushMailMirror(PushCommon):

    def _post_to_rep(self, record):
        return record.with_user(self.manager).message_post(
            body=Markup('Please call <b>Phnom Penh Mart</b> back'), partner_ids=self.rep.partner_id.ids,
            message_type='comment', subtype_xmlid='mail.mt_comment')

    def test_no_native_device_is_cheap(self):
        self._add_web_device(self.rep)
        visit = self._create_checkin(self.rep)
        self._post_to_rep(visit)
        self.assertFalse(self._messages(event='odoo_message'))
        self.assertTrue(self.mail_push_calls, "mail itself pushed to the browser")
        # one single query when no app device exists at all
        message = self.env['mail.message'].search([('model', '=', 'checkinme.checkin'), ('res_id', '=', visit.id)],
                                                  limit=1)
        recipients = [{'active': True, 'id': self.rep.partner_id.id, 'notif': 'inbox', 'share': False,
                       'uid': self.rep.id, 'type': 'user', 'groups': [], 'is_follower': False,
                       'lang': 'en_US', 'ushare': False}]
        self.env['checkinme.config']._get_bool('checkinme.push_odoo_messages', True)  # warm the cache
        with self.assertQueryCount(1):
            self.assertFalse(visit._checkinme_mirror_web_push(message, recipients))

    def test_mirror_to_native_devices_only(self):
        self._configure_fcm()
        self._add_web_device(self.rep)
        self._add_native_device(self.rep, token='rep-app-token')
        self._add_native_device(self.other_rep, token='other-app-token')
        visit = self._create_checkin(self.rep)
        self._post_to_rep(visit)
        message = self._messages(event='odoo_message')
        self.assertEqual(len(message), 1)
        self.assertTrue(message.native_only)
        self.assertEqual(message.user_ids, self.rep, "only the recipients of the message")
        self.assertEqual(message.url, '/odoo/checkinme.checkin/%d' % visit.id)
        self.assertEqual((message.res_model, message.res_id), ('checkinme.checkin', visit.id))
        self.assertIn('Chan Sophea', message.name)
        # plain text, as built by mail for the browsers (html2plaintext)
        self.assertEqual(message.body, 'Please call *Phnom Penh Mart* back')
        self.Push._cron_send_push_queue()
        # browsers got it from mail: our queue only talks to Google
        self.assertEqual(self.session.urls(), [fcm.TOKEN_URL, FCM_SEND_URL])
        self.assertEqual(self.session.calls[1][1]['json']['message']['token'], 'rep-app-token')
        self.assertEqual(self.session.calls[1][1]['json']['message']['notification']['body'],
                         'Please call *Phnom Penh Mart* back')
        self.assertEqual((message.state, message.web_push_count, message.native_count), ('sent', 0, 1))
        self.assertFalse(message.body, "the copy of the message is not kept once delivered")

    def test_mirror_disabled_and_author_excluded(self):
        self._add_native_device(self.rep)
        self._add_native_device(self.manager)
        visit = self._create_checkin(self.rep)
        self._set_param('checkinme.push_odoo_messages', 'False')
        self._post_to_rep(visit)
        self.assertFalse(self._messages(event='odoo_message'))
        self._set_param('checkinme.push_odoo_messages', 'True')
        self._post_to_rep(visit)
        self.assertEqual(self._messages(event='odoo_message').user_ids, self.rep, "never the author")

    def test_discuss_chat_link(self):
        self._add_native_device(self.rep)
        chat = self.env['discuss.channel'].with_user(self.manager).channel_get([self.rep.partner_id.id])
        chat.with_user(self.manager).message_post(body='Hello Dara', message_type='comment',
                                                  subtype_xmlid='mail.mt_comment')
        message = self._messages(event='odoo_message')
        self.assertEqual(len(message), 1)
        self.assertEqual(message.url, '/odoo/action-mail.action_discuss?active_id=discuss.channel_%d' % chat.id)
        self.assertEqual(message.body, 'Hello Dara')

    def test_private_messages_hidden_from_managers(self):
        """server-security-2: a Sale Check manager cannot read other people's chats in the push log."""
        self.Device.with_user(self.other_rep).register_device('other-rep-token')
        chat = self.env['discuss.channel'].with_user(self.rep).channel_get([self.other_rep.partner_id.id])
        message = chat.with_user(self.rep).message_post(body='TOPSECRET ' + 'x' * 5000, message_type='comment',
                                                        subtype_xmlid='mail.mt_comment')
        with self.assertRaises(AccessError):
            message.with_user(self.manager).read(['body'])
        self.assertFalse(self.Message.with_user(self.manager).search_read(
            [('event', '=', 'odoo_message')], ['body']))
        row = self._messages(event='odoo_message')
        self.assertTrue(row.body.startswith('TOPSECRET'))
        self.assertLessEqual(len(row.body), MAX_BODY_LENGTH)

    @mute_logger('odoo.addons.checkinme_sales_activity.models.mail_thread')
    def test_mirror_failure_does_not_break_posting(self):
        self._add_native_device(self.rep)
        visit = self._create_checkin(self.rep)
        with patch.object(type(self.env['mail.thread']), '_checkinme_mirror_web_push',
                          side_effect=RuntimeError('boom')):
            message = self._post_to_rep(visit)
        self.assertTrue(message)


@tagged('post_install', '-at_install')
class TestPushSettings(PushCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref('base.user_admin')

    def _settings(self, **vals):
        return self.env['res.config.settings'].with_user(self.admin).create(vals)

    def test_push_flags(self):
        settings = self._settings()
        self.assertTrue(settings.checkinme_push_planned_visit)
        self.assertTrue(settings.checkinme_push_followup_reminder)
        self.assertFalse(settings.checkinme_push_team_checkin)
        self.assertTrue(settings.checkinme_push_odoo_messages)
        settings.write({'checkinme_push_planned_visit': False, 'checkinme_push_team_checkin': True,
                        'checkinme_push_followup_reminder': False})
        settings.execute()
        self.assertEqual(self._get_param('checkinme.push_planned_visit'), 'False')
        self.assertEqual(self._get_param('checkinme.push_team_checkin'), 'True')
        self.assertFalse(self.env.ref('checkinme_sales_activity.ir_cron_checkinme_push_followup').active)
        self._settings(checkinme_push_followup_reminder=True).execute()
        self.assertTrue(self.env.ref('checkinme_sales_activity.ir_cron_checkinme_push_followup').active)

    def test_service_account_upload(self):
        settings = self._settings(checkinme_fcm_service_account_file=base64.b64encode(
            json.dumps(self.service_account).encode()), checkinme_fcm_service_account_filename='key.json')
        settings.execute()
        stored = json.loads(self._get_param('checkinme.fcm_service_account'))
        self.assertEqual(stored['client_email'], self.service_account['client_email'])
        # E2E B4: the uploaded key does not stay in the settings table
        self.env.flush_all()
        self.env.cr.execute("SELECT checkinme_fcm_service_account_file, checkinme_fcm_service_account_filename "
                            "FROM res_config_settings WHERE id = %s", (settings.id,))
        self.assertEqual(self.env.cr.fetchone(), (None, None))
        settings = self._settings()
        self.assertEqual(settings.checkinme_fcm_project_id, FCM_PROJECT)
        self.assertEqual(settings.checkinme_fcm_client_email, self.service_account['client_email'])
        self.assertFalse(settings.checkinme_fcm_service_account_file, "the key is never sent back")
        # E2E B1: the settings form (onchange) shows the configured project; the form holds
        # company_id (base_setup), the only field with a default these fields depend on
        values = self.env['res.config.settings'].with_user(self.admin).onchange({}, [], {
            'company_id': {'fields': {}}, 'checkinme_fcm_project_id': {}, 'checkinme_fcm_client_email': {},
        })['value']
        self.assertEqual(values['checkinme_fcm_project_id'], FCM_PROJECT)
        self.assertEqual(values['checkinme_fcm_client_email'], self.service_account['client_email'])
        self.assertTrue(self.env['checkinme.checkin'].with_user(self.rep).mobile_get_dashboard()['app']['push']['native_ready'])
        # saving the settings again without a new file keeps it
        settings.execute()
        self.assertTrue(self._get_param('checkinme.fcm_service_account'))
        # removal
        settings.action_checkinme_clear_fcm()
        self.assertFalse(self._get_param('checkinme.fcm_service_account'))
        self.assertFalse(self._settings().checkinme_fcm_project_id)

    def test_service_account_validation(self):
        for content in (b'not json', json.dumps({'type': 'service_account'}).encode(),
                        json.dumps({'project_info': {}, 'client': []}).encode(), b'\x89PNG\r\n'):
            settings = self._settings(checkinme_fcm_service_account_file=base64.b64encode(content))
            with self.assertRaises(UserError):
                settings.execute()
        self.assertFalse(self._get_param('checkinme.fcm_service_account'))

    def test_test_push_button(self):
        settings = self._settings()
        action = settings.action_checkinme_test_push()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(action['params']['type'], 'warning')
        self.assertIn('No device', action['params']['message'])
        self.assertFalse(self.session.calls)

        self._configure_fcm()
        self._add_web_device(self.admin)
        self._add_native_device(self.admin)
        action = self._settings().action_checkinme_test_push()
        self.assertEqual(action['params']['type'], 'success')
        self.assertIn('Sent to 1 browser/PWA device(s) and 1 Android app device(s).', action['params']['message'])
        message = self._messages(event='test')
        self.assertEqual(message.state, 'sent', "sent right away, not left to the cron")
        self.assertEqual(len(self.session.calls_to('https://push.example.com/')), 1)

    def test_test_push_from_the_phone(self):
        Checkin = self.env['checkinme.checkin'].with_user(self.rep)
        result = Checkin.mobile_send_test_push()
        self.assertEqual((result['web'], result['native'], result['state']), (0, 0, 'no_device'))
        self.assertIn('Add to Home Screen', result['message'])
        self._add_web_device(self.rep)
        result = Checkin.mobile_send_test_push()
        self.assertEqual((result['web'], result['native'], result['state']), (1, 0, 'sent'))
        self.assertIn('Sent to 1 browser/PWA device(s)', result['message'])
        self.assertEqual(self._messages(event='test').user_ids, self.rep)
        self.assertIsNone(result['this_device'])
        # a failure is reported, not raised, and the raw answer of the push service stays in the log
        self.session.handler = lambda url, kwargs: FakeResponse(400, text='bad request db_password=hunter2')
        with mute_logger(WEB_PUSH_LOGGER, PUSH_LOGGER):
            result = Checkin.mobile_send_test_push()
        self.assertEqual(result['state'], 'failed')
        self.assertIn('1 device(s) could not be reached', result['message'])
        for raw in ('400', 'hunter2', 'Web push'):
            self.assertNotIn(raw, result['message'])
        self.assertIn('hunter2', self._messages(event='test')[-1].error)

    @mute_logger(PUSH_LOGGER)
    def test_test_push_to_this_device(self):
        """C3 / requirements-test-push-misleading: the phone asks for a test on itself."""
        Checkin = self.env['checkinme.checkin'].with_user(self.rep)
        desktop, _key, _auth = self._add_web_device(self.rep, 'https://push.example.com/send/desktop')
        # this phone is not registered: nothing is sent to the desktop browser
        result = Checkin.mobile_send_test_push(endpoint='https://push.example.com/send/phone')
        self.assertEqual((result['this_device'], result['state'], result['web']), (False, 'no_device', 0))
        self.assertIn('not registered', result['message'])
        self.assertFalse(self.session.calls)
        # somebody else's subscription is not this user's device
        self._add_web_device(self.other_rep, 'https://push.example.com/send/other')
        self.assertFalse(Checkin.mobile_send_test_push(endpoint='https://push.example.com/send/other')['this_device'])
        self.assertFalse(self.session.calls)
        # the registered phone only
        self._add_web_device(self.rep, 'https://push.example.com/send/phone')
        result = api.call_kw(Checkin, 'mobile_send_test_push', [], {'endpoint': 'https://push.example.com/send/phone'})
        self.assertEqual((result['this_device'], result['state'], result['web'], result['native']),
                         (True, 'sent', 1, 0))
        self.assertEqual(self.session.urls(), ['https://push.example.com/send/phone'])
        self.assertEqual(result['message'], 'Test notification sent to this device.')
        # the push service refuses it
        self.session.handler = lambda url, kwargs: FakeResponse(500, text='internal: 10.1.2.3:5432')
        with mute_logger(WEB_PUSH_LOGGER):
            result = Checkin.mobile_send_test_push(endpoint='https://push.example.com/send/phone')
        self.assertFalse(result['this_device'])
        self.assertNotIn('10.1.2.3', result['message'])
        self.assertIn('could not be reached', result['message'])
        self.session.handler = None
        # the Android app: its FCM token
        self._configure_fcm()
        self._add_native_device(self.rep, token='rep-app-token')
        self.session.calls.clear()
        result = Checkin.mobile_send_test_push(token='rep-app-token')
        self.assertEqual((result['this_device'], result['web'], result['native']), (True, 0, 1))
        self.assertEqual(self.session.urls(), [fcm.TOKEN_URL, FCM_SEND_URL])
        self.assertFalse(Checkin.mobile_send_test_push(token='unknown-token')['this_device'])
        self.assertFalse(Checkin.mobile_send_test_push(token=['not', 'a', 'string'])['this_device'])
        self.assertTrue(desktop.exists())
