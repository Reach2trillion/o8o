# -*- coding: utf-8 -*-
import logging

from odoo import models

_logger = logging.getLogger(__name__)


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    def _notify_thread_by_web_push(self, message, recipients_data, msg_vals=False, **kwargs):
        """Odoo pushes mentions, direct messages and assignments to the browsers (and PWAs) of the
        recipients. Mirror them to the ABJ Android app, which has no browser subscription: one
        ``checkinme.push.message`` restricted to the app devices (``native_only``)."""
        super()._notify_thread_by_web_push(message, recipients_data, msg_vals=msg_vals, **kwargs)
        try:
            self._checkinme_mirror_web_push(message, recipients_data, msg_vals=msg_vals)
        except Exception:  # noqa: BLE001 - never break message posting because of the app
            _logger.exception("ABJ push: could not mirror the notification of message %s", message.id)

    def _checkinme_mirror_web_push(self, message, recipients_data, msg_vals=False):
        if not self.env['checkinme.config']._get_bool('checkinme.push_odoo_messages', True):
            return False
        Device = self.env['checkinme.push.device'].sudo()
        # cheap exit (one query) on the vast majority of databases: no app device at all
        if not Device.search_count([], limit=1):
            return False
        msg_vals = dict(msg_vals or {})
        partner_ids = self._extract_partner_ids_for_notifications(message, msg_vals, recipients_data)
        if not partner_ids:
            return False
        users = Device.search([('partner_id', 'in', partner_ids)]).user_id.filtered(
            lambda user: user.active and not user.share)
        if not users:
            return False
        payload = self._notify_by_web_push_prepare_payload(message, msg_vals=msg_vals)
        options = payload.get('options') or {}
        data = options.get('data') or {}
        model, res_id = data.get('model') or False, data.get('res_id') or False
        try:
            res_id = int(res_id)
        except (TypeError, ValueError):
            res_id = False
        if model == 'discuss.channel' and res_id:
            url = '/odoo/action-mail.action_discuss?active_id=discuss.channel_%d' % res_id
        elif model and res_id and model in self.env:
            url = '/odoo/%s/%d' % (model if '.' in model else 'm-%s' % model, res_id)
        else:
            url = '/odoo/action-mail.action_discuss'
        record = self.env[model].browse(res_id).exists() if model and res_id and model in self.env else None
        return self.env['checkinme.push']._notify(
            users, payload.get('title') or '', options.get('body') or '', url=url, event='odoo_message',
            record=record, tag='odoo-%s' % message.id, native_only=True)
