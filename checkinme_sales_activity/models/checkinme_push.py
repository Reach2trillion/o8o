# -*- coding: utf-8 -*-
"""Push notifications of the ABJ phone app.

Two delivery channels, one queue:

* **Web Push** to the browser / installed PWA subscriptions that Odoo's mail module already
  records in ``mail.push.device`` (iOS 16.4+ Home Screen apps, Chrome on Android and desktop),
  signed with mail's VAPID keys;
* **FCM** (Firebase Cloud Messaging HTTP v1) to the optional ABJ Android app, whose registration
  tokens are stored in ``checkinme.push.device``.

Business code calls ``env['checkinme.push']._notify(users, title, body, ...)``: it only queues a
``checkinme.push.message`` (cheap, never raises) and triggers the queue cron, which sends it right
after the transaction is committed, so a slow push service never delays the salesperson.
"""
import json
import logging
import threading
from datetime import datetime, timedelta

import requests

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError
from odoo.addons.mail.tools.web_push import DeviceUnreachableError

from ..tools.fcm import FcmClient, parse_service_account, should_deactivate
from ..tools.web_push import send_web_push
from .checkinme_checkin import safe_timezone

_logger = logging.getLogger(__name__)

APP_URL = '/odoo/abj'
ICON_URL = '/checkinme_sales_activity/static/img/abj-192.png'
BADGE_URL = '/checkinme_sales_activity/static/img/abj-badge-96.png'
FCM_PARAM = 'checkinme.fcm_service_account'
FCM_CHANNEL_ID = 'abj_default'
MAX_TOKEN_LENGTH = 4096
GC_DAYS = 30

PLATFORMS = [('android', 'Android'), ('ios', 'iOS')]

PUSH_EVENTS = [
    ('planned_visit', 'Planned Visit'),
    ('followup_reminder', 'Follow-up Reminder'),
    ('team_checkin', 'Team Check-in'),
    ('team_checkout', 'Team Check-out'),
    ('odoo_message', 'Odoo Message'),
    ('test', 'Test'),
    ('other', 'Other'),
]

PUSH_STATES = [
    ('pending', 'Pending'),
    ('sent', 'Sent'),
    ('partial', 'Partially Sent'),
    ('failed', 'Failed'),
    ('no_device', 'No Device'),
]

# How long a push service keeps an undelivered message (phone off / offline), in seconds.
DEFAULT_TTL = 24 * 3600
EVENT_TTL = {
    'test': 3600,
    'team_checkin': 4 * 3600,
    'team_checkout': 4 * 3600,
    'followup_reminder': 12 * 3600,
}


class CheckinmePushDevice(models.Model):
    """A phone running the ABJ Android app (FCM registration token)."""
    _name = 'checkinme.push.device'
    _description = 'ABJ App Device'
    _order = 'last_seen desc, id desc'

    user_id = fields.Many2one('res.users', string='User', required=True, index=True, ondelete='cascade')
    partner_id = fields.Many2one(related='user_id.partner_id', store=True, index=True)
    token = fields.Char(string='Registration Token', required=True, copy=False)
    platform = fields.Selection(PLATFORMS, string='Platform', default='android', required=True)
    device_name = fields.Char(string='Device')
    app_version = fields.Char(string='App Version')
    last_seen = fields.Datetime(string='Last Seen', default=fields.Datetime.now)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('token_unique', 'unique(token)', 'This device is already registered.'),
    ]

    @api.depends('device_name', 'token')
    def _compute_display_name(self):
        for device in self:
            device.display_name = device.device_name or (device.token or '')[:12]

    @api.model
    def register_device(self, token, platform='android', device_name='', app_version=''):
        """Called by the Android app after Firebase gave it a registration token. The token is
        attached to the current user (a phone that changes hands moves to its new user)."""
        user = self.env.user
        if not user._is_internal():
            raise AccessError(_("Only internal users can receive ABJ app notifications."))
        token = (token or '').strip()
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise UserError(_("Invalid push notification token."))
        vals = {
            'user_id': user.id,
            'platform': platform if platform in dict(PLATFORMS) else 'android',
            'device_name': (device_name or '').strip()[:128] or False,
            'app_version': (app_version or '').strip()[:64] or False,
            'last_seen': fields.Datetime.now(),
            'active': True,
        }
        Device = self.sudo().with_context(active_test=False)
        device = Device.search([('token', '=', token)], limit=1)
        if device:
            device.write(vals)
        else:
            device = Device.create(dict(vals, token=token))
        return {'id': device.id, 'active': device.active}

    @api.model
    def unregister_device(self, token):
        """Called on logout: stop sending to this phone (only for the current user's token)."""
        device = self.sudo().search([('token', '=', (token or '').strip()), ('user_id', '=', self.env.user.id)])
        if device:
            device.active = False
        return True


class CheckinmePushMessage(models.Model):
    """Queue and log of the notifications sent to the ABJ app."""
    _name = 'checkinme.push.message'
    _description = 'ABJ Push Notification'
    _order = 'id desc'

    name = fields.Char(string='Title', required=True)
    body = fields.Text(string='Message')
    url = fields.Char(string='Link', help="Page opened when the notification is tapped (relative URL).")
    event = fields.Selection(PUSH_EVENTS, string='Event', default='other', required=True, index=True)
    user_ids = fields.Many2many(
        'res.users', 'checkinme_push_message_res_users_rel', 'message_id', 'user_id', string='Recipients')
    res_model = fields.Char(string='Related Model')
    res_id = fields.Integer(string='Related Record ID')
    tag = fields.Char(string='Tag', help="Notifications with the same tag replace each other on the phone.")
    native_only = fields.Boolean(
        string='Android App Only',
        help="Odoo's own notifications (chatter, discuss) already reach the browsers through Odoo; "
             "only the Android app devices receive the copy.")
    state = fields.Selection(PUSH_STATES, string='Status', default='pending', required=True, index=True)
    web_push_count = fields.Integer(string='Browser / PWA Devices', readonly=True)
    native_count = fields.Integer(string='Android App Devices', readonly=True)
    error = fields.Text(string='Error', readonly=True)
    sent_date = fields.Datetime(string='Sent On', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company)

    @api.autovacuum
    def _gc_push_messages(self):
        """Keep the queue table small: drop entries older than 30 days."""
        limit = fields.Datetime.now() - timedelta(days=GC_DAYS)
        self.sudo().search([('create_date', '<', limit), ('state', '!=', 'pending')]).unlink()

    def action_retry(self):
        if not self.env.user.has_group('checkinme_sales_activity.group_checkinme_manager'):
            raise AccessError(_("Only Sale Check managers can resend notifications."))
        self.sudo().write({'state': 'pending', 'error': False})
        self.env['checkinme.push']._trigger_queue()
        return True


class CheckinmePush(models.AbstractModel):
    _name = 'checkinme.push'
    _description = 'ABJ Push Notification Service'

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_vapid_keys(self):
        """Mail's VAPID key pair. Never generated here: mail creates it when the first browser
        subscribes, and regenerating it would invalidate every subscription."""
        icp = self.env['ir.config_parameter'].sudo()
        return icp.get_param('mail.web_push_vapid_private_key'), icp.get_param('mail.web_push_vapid_public_key')

    @api.model
    def _get_fcm_service_account(self):
        raw = self.env['checkinme.config']._get_param(FCM_PARAM, '')
        if not raw:
            return None
        try:
            return parse_service_account(raw)
        except ValueError as exc:
            _logger.warning("ABJ push: invalid Firebase service account: %s", exc)
            return None

    @api.model
    def _is_native_ready(self):
        return bool(self.env['checkinme.config']._get_param(FCM_PARAM, ''))

    @api.model
    def _get_http_session(self):
        return requests.Session()

    @api.model
    def _trigger_queue(self):
        cron = self.env.ref('checkinme_sales_activity.ir_cron_checkinme_push_queue', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @api.model
    def _sanitize_url(self, url):
        """Only same-origin relative links ("/odoo/..."): a notification must never open
        another site."""
        url = (url or '').strip()
        if url.startswith('/') and not url.startswith('//') and '\\' not in url:
            return url
        return False

    @api.model
    def _default_url(self, record=None):
        if not record:
            return APP_URL
        if record._name == 'checkinme.checkin':
            return '%s/%d' % (APP_URL, record.id)
        model_path = record._name if '.' in record._name else 'm-%s' % record._name
        return '/odoo/%s/%d' % (model_path, record.id)

    @api.model
    def _get_reachable_users(self, users, native_only=False):
        """The active internal users among ``users`` that have at least one device: an Android
        app token, or (unless ``native_only``) a browser / PWA subscription."""
        users = users.sudo().filtered(lambda user: user.active and not user.share)
        if not users:
            return users
        reachable = self.env['checkinme.push.device'].sudo().search([('user_id', 'in', users.ids)]).user_id
        if not native_only:
            partners = self.env['mail.push.device'].sudo().search(
                [('partner_id', 'in', users.partner_id.ids)]).partner_id
            reachable |= users.filtered(lambda user: user.partner_id in partners)
        return users & reachable

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
    @api.model
    def _notify(self, users, title, body, url=None, event='other', record=None, tag=None, native_only=False):
        """Queue a notification for ``users`` (``res.users`` recordset) and wake the queue cron.

        Nothing is queued (an empty recordset is returned) when none of them has a device.
        Never raises: notifications must not break the business flow.

        :param str url: page opened on tap; defaults to the record in the ABJ screen
            (``/odoo/abj/<id>`` for a check-in, ``/odoo/<model>/<id>`` otherwise) or ``/odoo/abj``
        :param record: related record (any model)
        :param str tag: notifications with the same tag replace each other on the phone
        :param bool native_only: send to the Android app devices only
        """
        Message = self.env['checkinme.push.message'].sudo()
        try:
            with self.env.cr.savepoint():
                users = self._get_reachable_users(users or self.env['res.users'], native_only=native_only)
                if not users:
                    return Message
                record = record[:1] if record else None
                company = record.sudo().company_id if record and 'company_id' in record._fields else False
                message = Message.create({
                    'name': (title or '').strip() or 'ABJ',
                    'body': (body or '').strip() or False,
                    'url': self._sanitize_url(url) or self._default_url(record),
                    'event': event if event in dict(PUSH_EVENTS) else 'other',
                    'user_ids': [Command.set(users.ids)],
                    'res_model': record._name if record else False,
                    'res_id': record.id if record else False,
                    'tag': tag or False,
                    'native_only': native_only,
                    'company_id': company.id if company else self.env.company.id,
                })
                self._trigger_queue()
                return message
        except Exception:  # noqa: BLE001 - never block the business flow because of a notification
            _logger.exception("ABJ push: could not queue the notification %r", title)
            return Message

    @api.model
    def _cron_send_push_queue(self, limit=50):
        """Send the queued notifications (oldest first). Triggered right after each event and
        swept hourly as a safety net."""
        Message = self.env['checkinme.push.message'].sudo()
        pending = Message.search([('state', '=', 'pending')], order='id', limit=limit)
        auto_commit = not getattr(threading.current_thread(), 'testing', False)
        if pending:
            session = self._get_http_session()
            try:
                for message in pending:
                    try:
                        with self.env.cr.savepoint():
                            self._send_message(message, session=session)
                    except Exception as exc:  # noqa: BLE001 - one bad message must not block the queue
                        _logger.exception("ABJ push: sending notification %s failed", message.id)
                        message.write({'state': 'failed', 'error': str(exc)[:2000], 'sent_date': fields.Datetime.now()})
                    if auto_commit:
                        self.env.cr.commit()
            finally:
                session.close()
        if len(pending) >= limit:
            self._trigger_queue()
        Message._gc_push_messages()
        return True

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    @api.model
    def _get_tag(self, message):
        return message.tag or 'abj-%s-%s' % (message.event, message.res_id or message.id)

    @api.model
    def _prepare_web_push_payload(self, message):
        """Payload understood by both the ABJ service worker snippet (``options.data.abj``) and
        mail's service worker (``options.data.model`` / ``res_id``)."""
        payload = {
            'title': message.name,
            'options': {
                'body': message.body or '',
                'icon': ICON_URL,
                'badge': BADGE_URL,
                'tag': self._get_tag(message),
                'data': {
                    'abj': 1,
                    'abj_url': message.url or APP_URL,
                    'model': message.res_model or 'checkinme.checkin',
                    'res_id': message.res_id or 0,
                },
            },
        }
        return self.env['mail.thread']._truncate_payload(payload)

    @api.model
    def _prepare_fcm_data(self, message):
        return {
            'abj_url': message.url or APP_URL,
            'abj_event': message.event or 'other',
            'abj_model': message.res_model or '',
            'abj_res_id': str(message.res_id or 0),
        }

    @api.model
    def _send_message(self, message, session=None):
        """Deliver one queued message to every device of its recipients and log the result.

        :return: dict ``{'web', 'native', 'state', 'error'}`` (number of devices reached)
        """
        message.ensure_one()
        message = message.sudo()
        own_session = session is None
        session = session or self._get_http_session()
        users = message.user_ids.filtered('active')
        ttl = EVENT_TTL.get(message.event, DEFAULT_TTL)
        errors = []
        web_ok = web_attempts = native_ok = native_attempts = 0
        try:
            # --- browsers / PWA (Web Push) -----------------------------------
            web_devices = self.env['mail.push.device'].sudo().search(
                [('partner_id', 'in', users.partner_id.ids)]) if users and not message.native_only else []
            if web_devices:
                private_key, public_key = self._get_vapid_keys()
                if not (private_key and public_key):
                    errors.append(_("Web push skipped: the VAPID keys of the mail module are missing."))
                else:
                    payload = json.dumps(self._prepare_web_push_payload(message))
                    base_url = self.get_base_url()
                    unreachable = []
                    for device in web_devices:
                        web_attempts += 1
                        try:
                            send_web_push(base_url, {
                                'id': device.id, 'endpoint': device.endpoint, 'keys': device.keys,
                            }, payload, private_key, public_key, session, ttl=ttl, urgency='high')
                            web_ok += 1
                        except DeviceUnreachableError:
                            unreachable.append(device.id)
                        except Exception as exc:  # noqa: BLE001
                            errors.append(_("Web push: %s", exc))
                    if unreachable:
                        errors.append(_("%s expired browser subscription(s) removed.", len(unreachable)))
                        self.env['mail.push.device'].sudo().browse(unreachable).unlink()

            # --- Android app (FCM) -------------------------------------------
            native_devices = self.env['checkinme.push.device'].sudo().search(
                [('user_id', 'in', users.ids)]) if users else []
            if native_devices:
                service_account = self._get_fcm_service_account()
                if not service_account:
                    errors.append(_("Android app push skipped: the Firebase service account is not configured."))
                else:
                    client = FcmClient(service_account, dbname=self.env.cr.dbname)
                    data = self._prepare_fcm_data(message)
                    tag = self._get_tag(message)
                    dead = []
                    for device in native_devices:
                        native_attempts += 1
                        ok, code, text = client.send(
                            device.token, message.name, message.body or '', data, session,
                            ttl_seconds=ttl, channel_id=FCM_CHANNEL_ID, tag=tag)
                        if ok:
                            native_ok += 1
                            continue
                        errors.append("FCM %s: %s" % (code, text))
                        if should_deactivate(code, text):
                            dead.append(device.id)
                    if dead:
                        self.env['checkinme.push.device'].sudo().browse(dead).write({'active': False})
        finally:
            if own_session:
                session.close()

        delivered = web_ok + native_ok
        if not (web_devices or native_devices):
            state = 'no_device'
        elif delivered and delivered == web_attempts + native_attempts and not errors:
            state = 'sent'
        elif delivered:
            state = 'partial'
        else:
            state = 'failed'
        error = '\n'.join(str(error) for error in errors)[:4000] or False
        message.write({
            'state': state,
            'web_push_count': web_ok,
            'native_count': native_ok,
            'error': error,
            'sent_date': fields.Datetime.now(),
        })
        return {'web': web_ok, 'native': native_ok, 'state': state, 'error': error or ''}

    # ------------------------------------------------------------------
    # Test notification (settings button and phone screen)
    # ------------------------------------------------------------------
    @api.model
    def _send_test_notification(self, user=None):
        """Queue a test notification to ``user`` (default: current user) and send it now.

        :return: dict ``{'web', 'native', 'state', 'message'}``
        """
        user = user or self.env.user
        now = datetime.now(safe_timezone(user.tz or self.env.company.partner_id.tz))
        message = self._notify(
            user, _("ABJ test notification"),
            _("Notifications work on this device (%s).", now.strftime('%d %b %Y %H:%M')),
            url=APP_URL, event='test')
        if not message:
            return {
                'web': 0, 'native': 0, 'state': 'no_device',
                'message': _("No device is registered for your notifications yet. Open ABJ on your phone, "
                             "go to Settings and tap \"Enable notifications\" (on an iPhone, add ABJ to the "
                             "Home Screen first: Share > Add to Home Screen, iOS 16.4 or later)."),
            }
        result = self._send_message(message)
        text = _("Sent to %(web)s browser/PWA device(s) and %(native)s Android app device(s).",
                 web=result['web'], native=result['native'])
        if result['error']:
            text = "%s\n%s" % (text, result['error'])
        return {'web': result['web'], 'native': result['native'], 'state': result['state'], 'message': text}
