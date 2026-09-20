# -*- coding: utf-8 -*-
"""Telegram Bot API integration for CheckinMe (instant notifications and periodic reports)."""
import html
import logging
from datetime import timedelta

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang, format_date

_logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot%s/%s"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_MAX_CAPTION_LENGTH = 1024
DEFAULT_TIMEOUT = 10

# Bot token fallbacks: other Telegram modules deployed alongside this one may already store a
# bot token. The first configured key wins; our own key always takes precedence.
BOT_TOKEN_PARAM_KEYS = (
    'checkinme.telegram_bot_token',
    'send_by_telegram.bot_token',
    'abj.telegram.bot_token',
)

MESSAGE_TYPES = [
    ('checkin', 'Check-in Notification'),
    ('checkout', 'Check-out Notification'),
    ('daily', 'Daily Report'),
    ('weekly', 'Weekly Report'),
    ('monthly', 'Monthly Report'),
    ('report', 'Manual Report'),
    ('test', 'Test Message'),
    ('other', 'Other'),
]

REPORT_TITLES = {
    'daily': 'Daily Sales Activity Report',
    'weekly': 'Weekly Sales Activity Report',
    'monthly': 'Monthly Sales Activity Report',
    'report': 'Sales Activity Report',
}


class CheckinmeTelegramLog(models.Model):
    _name = 'checkinme.telegram.log'
    _description = 'Telegram Message Log'
    _order = 'create_date desc, id desc'
    _rec_name = 'chat_id'

    chat_id = fields.Char(string='Chat ID', index=True)
    message_type = fields.Selection(MESSAGE_TYPES, default='other', index=True)
    method = fields.Char(string='API Method', default='sendMessage')
    message = fields.Text(string='Message')
    state = fields.Selection([('sent', 'Sent'), ('failed', 'Failed'), ('skipped', 'Skipped')],
                             default='sent', index=True)
    response = fields.Text(string='API Response / Error')
    checkin_id = fields.Many2one('checkinme.checkin', string='Check-in', ondelete='set null', index=True)
    employee_id = fields.Many2one('hr.employee', string='Salesperson', ondelete='set null', index=True)
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company)

    @api.autovacuum
    def _gc_telegram_logs(self):
        """Keep the log table small: drop entries older than 90 days."""
        limit = fields.Datetime.now() - timedelta(days=90)
        self.sudo().search([('create_date', '<', limit)]).unlink()


class CheckinmeTelegram(models.AbstractModel):
    _name = 'checkinme.telegram'
    _description = 'CheckinMe Telegram Service'

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_bot_token(self):
        icp = self.env['ir.config_parameter'].sudo()
        for key in BOT_TOKEN_PARAM_KEYS:
            token = (icp.get_param(key) or '').strip()
            if token:
                return token
        return False

    @api.model
    def _get_default_chat_id(self):
        return (self.env['checkinme.config']._get_param('checkinme.telegram_chat_id', '') or '').strip()

    @api.model
    def _get_user_fallback_chat_id(self, user):
        """Reuse a per-user Telegram chat id defined by another Telegram module, if any."""
        if user and 'telegram_group_id' in user._fields:
            return (user.sudo().telegram_group_id or '').strip()
        return ''

    @api.model
    def _get_manager_chat_ids(self, employee):
        """Chat ids to notify for an employee: the managers' group + personal chats of the
        employee's manager and department manager (deduplicated)."""
        chat_ids = []

        def add(chat_id):
            chat_id = (chat_id or '').strip()
            if chat_id and chat_id not in chat_ids:
                chat_ids.append(chat_id)

        add(self._get_default_chat_id())
        employee = employee.sudo()
        managers = employee.parent_id | employee.department_id.manager_id
        for manager in managers:
            if manager == employee:
                continue
            add(manager.checkinme_telegram_chat_id or self._get_user_fallback_chat_id(manager.user_id))
        return chat_ids

    @api.model
    def _escape(self, text):
        return html.escape(str(text or ''), quote=False)

    # ------------------------------------------------------------------
    # Low level API
    # ------------------------------------------------------------------
    @api.model
    def _api_call(self, method, payload=None, files=None, token=None, timeout=DEFAULT_TIMEOUT):
        """Call a Telegram Bot API method. Returns (ok, response_dict). Never raises."""
        token = token or self._get_bot_token()
        if not token:
            return False, {'ok': False, 'description': 'Telegram bot token is not configured.'}
        url = TELEGRAM_API_URL % (token, method)
        try:
            if files:
                response = requests.post(url, data=payload or {}, files=files, timeout=timeout)
            else:
                response = requests.post(url, json=payload or {}, timeout=timeout)
            try:
                data = response.json()
            except ValueError:
                data = {'ok': False, 'description': 'Invalid response (%s): %s' % (
                    response.status_code, (response.text or '')[:200])}
            if not isinstance(data, dict):
                data = {'ok': False, 'description': 'Unexpected response: %r' % (data,)}
            return bool(data.get('ok')), data
        except requests.RequestException as exc:
            _logger.warning("CheckinMe Telegram: %s failed: %s", method, exc)
            return False, {'ok': False, 'description': str(exc)}

    @api.model
    def _log(self, chat_id, message_type, message, ok, response, method='sendMessage',
             checkin=None, employee=None, state=None):
        try:
            self.env['checkinme.telegram.log'].sudo().create({
                'chat_id': chat_id,
                'message_type': message_type,
                'method': method,
                'message': message,
                'state': state or ('sent' if ok else 'failed'),
                'response': (response.get('description') if isinstance(response, dict) and not ok
                             else str(response)[:2000]) if response else False,
                'checkin_id': checkin.id if checkin else False,
                'employee_id': employee.id if employee else (checkin.employee_id.id if checkin else False),
                'company_id': (checkin.company_id.id if checkin else self.env.company.id),
            })
        except Exception:  # noqa: BLE001 - logging must never break the business flow
            _logger.exception("CheckinMe Telegram: could not write log entry")

    @api.model
    def _split_message(self, text, limit=TELEGRAM_MAX_MESSAGE_LENGTH):
        """Split a long message into chunks on line boundaries."""
        if len(text) <= limit:
            return [text]
        chunks, current = [], ''
        for line in text.split('\n'):
            while len(line) > limit:
                if current:
                    chunks.append(current)
                    current = ''
                chunks.append(line[:limit])
                line = line[limit:]
            candidate = line if not current else current + '\n' + line
            if len(candidate) > limit:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    @api.model
    def _send_message(self, chat_id, text, message_type='other', checkin=None, employee=None,
                      parse_mode='HTML', disable_web_page_preview=True):
        """Send a text message (HTML parse mode). Returns (ok, last_response)."""
        chat_id = (chat_id or '').strip()
        if not chat_id:
            response = {'ok': False, 'description': 'No Telegram chat id configured.'}
            self._log(chat_id, message_type, text, False, response, checkin=checkin,
                      employee=employee, state='skipped')
            return False, response
        ok_all, last_response = True, {}
        for chunk in self._split_message(text):
            payload = {
                'chat_id': chat_id,
                'text': chunk,
                'parse_mode': parse_mode,
                'disable_web_page_preview': disable_web_page_preview,
            }
            ok, last_response = self._api_call('sendMessage', payload)
            ok_all = ok_all and ok
            if not ok:
                break
        self._log(chat_id, message_type, text, ok_all, last_response, checkin=checkin, employee=employee)
        return ok_all, last_response

    @api.model
    def _send_location(self, chat_id, latitude, longitude, message_type='other', checkin=None):
        payload = {'chat_id': chat_id, 'latitude': latitude, 'longitude': longitude}
        ok, response = self._api_call('sendLocation', payload)
        self._log(chat_id, message_type, "%s, %s" % (latitude, longitude), ok, response,
                  method='sendLocation', checkin=checkin)
        return ok, response

    @api.model
    def _send_photo(self, chat_id, image_b64, caption='', message_type='other', checkin=None):
        import base64
        try:
            content = base64.b64decode(image_b64)
        except Exception:  # noqa: BLE001
            return False, {'ok': False, 'description': 'Invalid image data.'}
        payload = {'chat_id': chat_id, 'parse_mode': 'HTML'}
        if caption:
            payload['caption'] = caption[:TELEGRAM_MAX_CAPTION_LENGTH]
        files = {'photo': ('checkin.jpg', content)}
        ok, response = self._api_call('sendPhoto', payload, files=files)
        self._log(chat_id, message_type, caption or 'photo', ok, response, method='sendPhoto', checkin=checkin)
        return ok, response

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------
    @api.model
    def _format_amount(self, amount, currency):
        return formatLang(self.env, amount or 0.0, currency_obj=currency)

    @api.model
    def _format_checkin_message(self, checkin, event='checkin'):
        esc = self._escape
        checkin = checkin.sudo()
        is_checkout = event == 'checkout'
        when = checkin._to_local(checkin.checkout_time if is_checkout else checkin.checkin_time)
        lines = [
            "%s <b>%s</b> — %s" % ('🏁' if is_checkout else '📍',
                                   _('Check-out') if is_checkout else _('Check-in'),
                                   esc(checkin.name)),
            "👤 <b>%s</b>" % esc(checkin.employee_id.name),
        ]
        if checkin.partner_id:
            lines.append("🏢 %s: <b>%s</b>" % (_('Customer'), esc(checkin.partner_id.display_name)))
            address = ' '.join((checkin.partner_id._display_address(without_company=True) or '').split())
            if address:
                lines.append("📮 %s" % esc(address))
        lines.append("🗂 %s: %s" % (_('Type'), esc(checkin.checkin_type_id.name)))
        if checkin.purpose:
            lines.append("🎯 %s: %s" % (_('Purpose'), esc(checkin.purpose)))
        if when:
            lines.append("🕒 %s (%s)" % (when.strftime('%d %b %Y %H:%M'), esc(checkin._get_tz_name())))
        if is_checkout:
            if checkin.duration:
                hours, minutes = divmod(int(round(checkin.duration * 60)), 60)
                lines.append("⏱ %s: %dh %02dm" % (_('Duration'), hours, minutes))
            url = checkin.checkout_google_maps_url
        else:
            url = checkin.google_maps_url
            status = checkin.location_status
            if status == 'verified':
                lines.append("✅ %s (%d m)" % (_('Location verified on site'), round(checkin.distance_to_partner)))
            elif status == 'far':
                lines.append("⚠️ %s (%s)" % (_('Far from customer location'),
                                             self._format_distance(checkin.distance_to_partner)))
            elif status == 'unknown':
                lines.append("ℹ️ %s" % _('Customer has no geolocation yet'))
            else:
                lines.append("❌ %s" % _('No GPS position captured'))
            if checkin.accuracy:
                lines.append("🎯 %s: ±%d m" % (_('GPS accuracy'), round(checkin.accuracy)))
        if url:
            lines.append('🗺 <a href="%s">%s</a>' % (esc(url), _('Open in Google Maps')))
        if is_checkout or checkin.outcome:
            if checkin.outcome:
                outcome = dict(checkin._fields['outcome']._description_selection(self.env)).get(checkin.outcome, '')
                lines.append("📊 %s: %s" % (_('Outcome'), esc(outcome)))
            if checkin.sale_amount:
                lines.append("💰 %s: %s" % (_('Sales'), esc(self._format_amount(checkin.sale_amount, checkin.currency_id))))
            if checkin.next_action_date:
                lines.append("📅 %s: %s" % (_('Follow-up'), format_date(self.env, checkin.next_action_date)))
        if checkin.is_new_customer and not is_checkout:
            lines.append("🆕 %s" % _('New customer'))
        if checkin.notes:
            notes = checkin.notes.strip()
            if len(notes) > 500:
                notes = notes[:497] + '...'
            lines.append("📝 %s" % esc(notes))
        return '\n'.join(lines)

    @api.model
    def _format_distance(self, meters):
        meters = meters or 0.0
        if meters >= 1000:
            return "%.1f km" % (meters / 1000.0)
        return "%d m" % round(meters)

    @api.model
    def _format_period_label(self, date_from, date_to):
        if date_from == date_to:
            return format_date(self.env, date_from, date_format='EEE dd MMM yyyy')
        return "%s → %s" % (format_date(self.env, date_from, date_format='dd MMM yyyy'),
                            format_date(self.env, date_to, date_format='dd MMM yyyy'))

    @api.model
    def _format_period_report(self, summary, title):
        esc = self._escape
        currency = summary['currency']
        totals = summary['totals']
        lines = [
            "📊 <b>%s</b>" % esc(title),
            "📅 %s" % esc(self._format_period_label(summary['date_from'], summary['date_to'])),
        ]
        if summary['company']:
            lines.append("🏢 %s" % esc(summary['company'].name))
        lines.append("")
        lines.append("👥 %d %s · 📍 %d %s (%d ✅) · 🏬 %d %s · 🧾 %d %s · 💰 %s" % (
            totals['employees'], _('salespeople'),
            totals['visits'], _('visits'), totals['verified'],
            totals['customers'], _('customers'),
            totals['orders'], _('orders'),
            esc(self._format_amount(totals['sales_amount'], currency)),
        ))
        if totals.get('target_sales_amount'):
            pct = 100.0 * totals['sales_amount'] / totals['target_sales_amount']
            lines.append("🎯 %s: %s / %s (%.0f%%)" % (
                _('Team sales target'),
                esc(self._format_amount(totals['sales_amount'], currency)),
                esc(self._format_amount(totals['target_sales_amount'], currency)), pct))
        lines.append("")
        if not summary['employees']:
            lines.append("🚫 %s" % _('No activity recorded for this period.'))
        for index, row in enumerate(summary['employees'], 1):
            line = "%d. <b>%s</b> — 📍 %d (%d ✅) · 🏬 %d · 🆕 %d · 🧾 %d · 💰 %s" % (
                index, esc(row['employee'].name),
                row['visits'], row['verified'], row['customers'], row['new_customers'],
                row['orders'], esc(self._format_amount(row['sales_amount'], currency)))
            if row['checkins'] == 0 and row['orders'] == 0:
                line += "  ⚠️"
            if row.get('target'):
                status = dict(row['target']._fields['status']._description_selection(self.env)).get(
                    row.get('target_status'), '')
                parts = []
                if row.get('target_sales_amount'):
                    parts.append("%s %.0f%%" % (_('Sales'), row['achievement_sales']))
                if row.get('target_visits'):
                    parts.append("%s %.0f%%" % (_('Visits'), row['achievement_visits']))
                if row.get('target_new_customers'):
                    parts.append("%s %.0f%%" % (_('New'), row['achievement_new_customers']))
                if row.get('target_orders'):
                    parts.append("%s %.0f%%" % (_('Orders'), row['achievement_orders']))
                if parts:
                    line += "\n    🎯 %s: %s · %s" % (_('Month to date'), ' · '.join(parts), esc(status))
            lines.append(line)
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # High level operations
    # ------------------------------------------------------------------
    @api.model
    def _notify_checkin_event(self, checkin, event='checkin'):
        """Send the check-in / check-out notification to the managers. Returns True if at
        least one chat received the message."""
        checkin.ensure_one()
        config = self.env['checkinme.config']
        if not self._get_bot_token():
            self._log('', event, '', False, {'description': 'Telegram bot token is not configured.'},
                      checkin=checkin, state='skipped')
            return False
        chat_ids = self._get_manager_chat_ids(checkin.employee_id)
        if not chat_ids:
            self._log('', event, '', False, {'description': 'No manager Telegram chat id configured.'},
                      checkin=checkin, state='skipped')
            return False
        text = self._format_checkin_message(checkin, event)
        sent = False
        for chat_id in chat_ids:
            ok, _response = self._send_message(chat_id, text, message_type=event, checkin=checkin)
            if not ok:
                continue
            sent = True
            if event == 'checkin' and checkin.has_location and config._get_bool('checkinme.send_location_pin', True):
                self._send_location(chat_id, checkin.latitude, checkin.longitude, message_type=event, checkin=checkin)
            elif event == 'checkout' and (checkin.checkout_latitude or checkin.checkout_longitude) \
                    and config._get_bool('checkinme.send_location_pin', True):
                self._send_location(chat_id, checkin.checkout_latitude, checkin.checkout_longitude,
                                    message_type=event, checkin=checkin)
            if event == 'checkin' and checkin.photo and config._get_bool('checkinme.send_photo', True):
                caption = "📷 %s — %s" % (self._escape(checkin.name),
                                          self._escape(checkin.partner_id.display_name or checkin.employee_id.name))
                self._send_photo(chat_id, checkin.photo, caption=caption, message_type=event, checkin=checkin)
        return sent

    @api.model
    def _send_period_report(self, period, chat_id=None, employees=None, company=None, message_type='report',
                            title=None):
        """Build and send the activity report for a named period ('today', 'last_week', ...)
        or an explicit (date_from, date_to) tuple. Returns (ok, text)."""
        Checkin = self.env['checkinme.checkin']
        company = company or self.env.company
        if isinstance(period, str):
            date_from, date_to = Checkin._get_period_dates(period, today=Checkin._get_today(company))
        else:
            date_from, date_to = period
        summary = Checkin._get_performance_summary(date_from, date_to, employees=employees, company=company)
        title = title or _(REPORT_TITLES.get(message_type, REPORT_TITLES['report']))
        text = self._format_period_report(summary, title)
        chat_id = (chat_id or self._get_default_chat_id() or '').strip()
        ok, _response = self._send_message(chat_id, text, message_type=message_type)
        return ok, text

    @api.model
    def _run_scheduled_report(self, flag_key, period, message_type):
        if not self.env['checkinme.config']._get_bool(flag_key, True):
            return False
        if not self._get_bot_token() or not self._get_default_chat_id():
            _logger.info("CheckinMe: %s report skipped, Telegram is not configured.", message_type)
            return False
        results = []
        for company in self.env['res.company'].search([]):
            ok, _text = self.with_company(company)._send_period_report(
                period, company=company, message_type=message_type)
            results.append(ok)
        return all(results)

    @api.model
    def _cron_daily_report(self):
        return self._run_scheduled_report('checkinme.daily_report', 'today', 'daily')

    @api.model
    def _cron_weekly_report(self):
        return self._run_scheduled_report('checkinme.weekly_report', 'last_week', 'weekly')

    @api.model
    def _cron_monthly_report(self):
        return self._run_scheduled_report('checkinme.monthly_report', 'last_month', 'monthly')

    @api.model
    def action_test_connection(self, chat_id=None):
        """Check the bot token (getMe) and send a test message to the managers' chat."""
        token = self._get_bot_token()
        if not token:
            raise UserError(_("Please configure the Telegram bot token first."))
        ok, info = self._api_call('getMe', token=token)
        if not ok:
            raise UserError(_("Telegram rejected the bot token: %s", info.get('description', '')))
        bot_name = info.get('result', {}).get('username') or info.get('result', {}).get('first_name', '')
        chat_id = (chat_id or self._get_default_chat_id() or '').strip()
        if not chat_id:
            raise UserError(_("Bot @%s is valid. Please configure the managers' chat id to send messages.", bot_name))
        text = "✅ <b>%s</b>\n%s" % (
            self._escape(_('CheckinMe Sales Activity')),
            self._escape(_('Telegram integration is working. Bot: @%s, company: %s.',
                           bot_name, self.env.company.name)))
        ok, response = self._send_message(chat_id, text, message_type='test')
        if not ok:
            raise UserError(_("Could not send the test message: %s", response.get('description', '')))
        return bot_name
