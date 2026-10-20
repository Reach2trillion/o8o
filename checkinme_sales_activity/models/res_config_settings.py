# -*- coding: utf-8 -*-
import base64
import binascii
import json

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools import str2bool

from ..tools.fcm import clear_token_cache, parse_service_account
from .checkinme_push import FCM_PARAM

# Boolean settings that default to True cannot use ``config_parameter`` (an unchecked value is
# stored as "absent" and the default silently re-applies), so they are handled manually.
BOOL_SETTINGS = {
    'checkinme_notify_checkin': ('checkinme.notify_checkin', True),
    'checkinme_notify_checkout': ('checkinme.notify_checkout', True),
    'checkinme_send_location_pin': ('checkinme.send_location_pin', True),
    'checkinme_send_photo': ('checkinme.send_photo', True),
    'checkinme_daily_report': ('checkinme.daily_report', True),
    'checkinme_weekly_report': ('checkinme.weekly_report', True),
    'checkinme_monthly_report': ('checkinme.monthly_report', True),
    'checkinme_require_gps': ('checkinme.require_gps', True),
    # ABJ app push notifications
    'checkinme_push_planned_visit': ('checkinme.push_planned_visit', True),
    'checkinme_push_followup_reminder': ('checkinme.push_followup_reminder', True),
    'checkinme_push_team_checkin': ('checkinme.push_team_checkin', False),
    'checkinme_push_odoo_messages': ('checkinme.push_odoo_messages', True),
}

CRON_FLAGS = {
    'checkinme_daily_report': 'checkinme_sales_activity.ir_cron_checkinme_daily_report',
    'checkinme_weekly_report': 'checkinme_sales_activity.ir_cron_checkinme_weekly_report',
    'checkinme_monthly_report': 'checkinme_sales_activity.ir_cron_checkinme_monthly_report',
    'checkinme_push_followup_reminder': 'checkinme_sales_activity.ir_cron_checkinme_push_followup',
}

SALES_SOURCES = [
    ('sale_order', 'Confirmed sales orders of the salesperson'),
    ('checkin', 'Sales amount entered on check-ins'),
]


class CheckinmeConfig(models.AbstractModel):
    """Typed accessors for the module's system parameters."""
    _name = 'checkinme.config'
    _description = 'Sale Check Configuration Helper'

    @api.model
    def _get_param(self, key, default=False):
        value = self.env['ir.config_parameter'].sudo().get_param(key)
        return default if value in (False, None, '') else value

    @api.model
    def _get_bool(self, key, default=False):
        value = self.env['ir.config_parameter'].sudo().get_param(key)
        if value in (False, None, ''):
            return default
        try:
            return str2bool(str(value), default)
        except ValueError:
            return default

    @api.model
    def _get_int(self, key, default=0):
        try:
            return int(self._get_param(key, default))
        except (TypeError, ValueError):
            return default

    @api.model
    def _set_param(self, key, value):
        return self.env['ir.config_parameter'].sudo().set_param(key, value)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    checkinme_telegram_bot_token = fields.Char(
        string='Telegram Bot Token', config_parameter='checkinme.telegram_bot_token',
        help="Token given by @BotFather. Add the bot to the managers' group and make sure it may post there.")
    checkinme_telegram_chat_id = fields.Char(
        string="Managers' Telegram Chat ID", config_parameter='checkinme.telegram_chat_id',
        help="Chat / group / channel id receiving check-in notifications and periodic reports "
             "(e.g. -1001234567890). Managers may also have a personal chat id on their employee form.")
    checkinme_notify_checkin = fields.Boolean(string='Notify Managers on Check-in')
    checkinme_notify_checkout = fields.Boolean(string='Notify Managers on Check-out')
    checkinme_send_location_pin = fields.Boolean(string='Send Location Pin')
    checkinme_send_photo = fields.Boolean(string='Send Check-in Photo')
    checkinme_daily_report = fields.Boolean(string='Daily Report')
    checkinme_weekly_report = fields.Boolean(string='Weekly Report')
    checkinme_monthly_report = fields.Boolean(string='Monthly Report')
    checkinme_require_gps = fields.Boolean(string='Require GPS Position to Check In')
    checkinme_max_distance = fields.Integer(
        string='Maximum Distance (m)', config_parameter='checkinme.max_distance_m', default=500,
        help="A visit is marked 'Verified on site' when the check-in position is within this "
             "distance of the customer's geolocation.")
    checkinme_sales_source = fields.Selection(
        SALES_SOURCES, string='Sales Results Source', config_parameter='checkinme.sales_source',
        default='sale_order',
        help="How actual sales are measured against targets and in reports.")

    # ABJ app
    checkinme_push_planned_visit = fields.Boolean(string='Planned Visit Notifications')
    checkinme_push_followup_reminder = fields.Boolean(string='Morning Follow-up Reminders')
    checkinme_push_team_checkin = fields.Boolean(string='Team Check-in Notifications')
    checkinme_push_odoo_messages = fields.Boolean(string='Odoo Messages in the Android App')
    checkinme_fcm_service_account_file = fields.Binary(
        string='Firebase Service Account Key', attachment=False, groups='base.group_system',
        help="JSON key of a Firebase service account (Firebase console > Project settings > Service "
             "accounts > Generate new private key). Only needed for the optional ABJ Android app.")
    checkinme_fcm_service_account_filename = fields.Char(groups='base.group_system')
    checkinme_fcm_project_id = fields.Char(
        string='Firebase Project', compute='_compute_checkinme_fcm_info', groups='base.group_system')
    checkinme_fcm_client_email = fields.Char(
        string='Service Account', compute='_compute_checkinme_fcm_info', groups='base.group_system')

    def _compute_checkinme_fcm_info(self):
        info = self.env['checkinme.push']._get_fcm_service_account() or {}
        for settings in self:
            settings.checkinme_fcm_project_id = info.get('project_id', False)
            settings.checkinme_fcm_client_email = info.get('client_email', False)

    @api.model
    def get_values(self):
        res = super().get_values()
        config = self.env['checkinme.config']
        for fname, (key, default) in BOOL_SETTINGS.items():
            res[fname] = config._get_bool(key, default)
        return res

    def set_values(self):
        super().set_values()
        icp = self.env['ir.config_parameter'].sudo()
        for fname, (key, _default) in BOOL_SETTINGS.items():
            icp.set_param(key, 'True' if self[fname] else 'False')
        for fname, cron_xmlid in CRON_FLAGS.items():
            cron = self.env.ref(cron_xmlid, raise_if_not_found=False)
            if cron:
                cron.sudo().active = bool(self[fname])
        self._set_checkinme_fcm_service_account()

    def _set_checkinme_fcm_service_account(self):
        """Validate and store an uploaded service account key (the field itself stays empty:
        the private key is never sent back to the browser)."""
        if not self.env.user.has_group('base.group_system'):
            return
        content = self.sudo().with_context(bin_size=False).checkinme_fcm_service_account_file
        if not content:
            return
        try:
            info = parse_service_account(base64.b64decode(content, validate=True))
        except (ValueError, binascii.Error) as exc:
            raise UserError(_("Invalid Firebase service account key: %s", exc))
        self.env['ir.config_parameter'].sudo().set_param(FCM_PARAM, json.dumps(info))
        clear_token_cache()

    def action_checkinme_clear_fcm(self):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_("Only administrators can remove the Firebase service account."))
        self.env['ir.config_parameter'].sudo().set_param(FCM_PARAM, False)
        clear_token_cache()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_checkinme_test_push(self):
        """Send a test notification to the current user's phones (browsers / PWA and app)."""
        self.ensure_one()
        self.set_values()
        result = self.env['checkinme.push']._send_test_notification(self.env.user)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('ABJ notifications'),
                'message': result['message'],
                'type': {'sent': 'success', 'partial': 'warning', 'no_device': 'warning'}.get(
                    result['state'], 'danger'),
                'sticky': result['state'] != 'sent',
            },
        }

    def action_checkinme_test_telegram(self):
        self.ensure_one()
        self.set_values()
        bot_name = self.env['checkinme.telegram'].action_test_connection()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Telegram connected'),
                'message': _('Test message sent by bot @%s.', bot_name),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_checkinme_send_daily_report(self):
        self.ensure_one()
        self.set_values()
        ok, _text = self.env['checkinme.telegram']._send_period_report('today', message_type='daily')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Telegram'),
                'message': _("Today's report was sent to the managers' chat.") if ok
                else _("The report could not be sent. Check the Telegram logs."),
                'type': 'success' if ok else 'danger',
                'sticky': False,
            },
        }
