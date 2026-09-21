# -*- coding: utf-8 -*-
"""Shared fixtures for the CheckinMe Sales Activity tests.

Every test class inherits :class:`CheckinmeCommon`, which sets up a Cambodian company
(Asia/Phnom_Penh), three users (a salesperson, a manager and a second salesperson that is
not managed by the manager), their employees, two customers (one geolocated, one not) and
the default configuration parameters.

Telegram HTTP calls are never issued for real: :func:`mock_telegram` patches
``requests.post`` as used by the Telegram service and records every call.
"""
from contextlib import contextmanager
from unittest.mock import patch

from odoo import Command
from odoo.tests.common import TransactionCase, new_test_user

# Target patched by mock_telegram(): ``requests.post`` as seen from the Telegram service.
TELEGRAM_POST_TARGET = 'odoo.addons.checkinme_sales_activity.models.checkinme_telegram.requests.post'

# Context used for every record creation to skip mail notifications / tracking.
MAIL_CONTEXT = {
    # send Telegram notifications synchronously in tests (production queues them for the cron)
    'checkinme_telegram_sync': True,
    'tracking_disable': True,
    'mail_create_nolog': True,
    'mail_notrack': True,
    'no_reset_password': True,
}

TZ = 'Asia/Phnom_Penh'
TEST_BOT_TOKEN = '123456789:TEST-BOT-TOKEN'
TEST_CHAT_ID = '-1001234567890'
MANAGER_CHAT_ID = '999'

# Customer position (Phnom Penh) and the salesperson position, roughly 15 m away.
PARTNER_LAT, PARTNER_LNG = 11.5564, 104.9282
CHECKIN_LAT, CHECKIN_LNG = 11.5565, 104.9283

# 1x1 transparent PNG (valid image for fields.Image and for sendPhoto).
TINY_PNG_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='
)

# All the system parameter keys the Telegram service may read a bot token from.
BOT_TOKEN_PARAM_KEYS = (
    'checkinme.telegram_bot_token',
    'send_by_telegram.bot_token',
    'abj.telegram.bot_token',
)

# Boolean flags reset to their default before every test class.
BOOL_PARAM_DEFAULTS = {
    'checkinme.notify_checkin': 'True',
    'checkinme.notify_checkout': 'True',
    'checkinme.send_location_pin': 'True',
    'checkinme.send_photo': 'True',
    'checkinme.daily_report': 'True',
    'checkinme.weekly_report': 'True',
    'checkinme.monthly_report': 'True',
}


class FakeTelegramResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, data=None, status_code=200, text=''):
        self._data = data if data is not None else {
            'ok': True,
            'result': {'message_id': 1, 'username': 'test_bot'},
        }
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._data


class TelegramMock:
    """Callable replacing ``requests.post``; records ``(url, kwargs)`` for every call.

    :param response: dict returned by ``response.json()`` (defaults to an ``ok`` answer)
    :param side_effect: exception (class or instance) to raise, or a callable
                        ``(url, **kwargs) -> FakeTelegramResponse``
    """

    def __init__(self, response=None, side_effect=None):
        self.calls = []
        self.response = response
        self.side_effect = side_effect

    def __call__(self, url, *args, **kwargs):
        self.calls.append((url, kwargs))
        effect = self.side_effect
        if effect is not None:
            if isinstance(effect, BaseException):
                raise effect
            if isinstance(effect, type) and issubclass(effect, BaseException):
                raise effect('mocked Telegram failure')
            return effect(url, **kwargs)
        return FakeTelegramResponse(self.response)

    @property
    def urls(self):
        return [url for url, _kwargs in self.calls]

    @property
    def methods(self):
        """Telegram API method names (last URL segment) in call order."""
        return [url.rstrip('/').rsplit('/', 1)[-1] for url in self.urls]

    @property
    def payloads(self):
        """The JSON (or multipart ``data``) payload of every call, in call order."""
        return [kwargs.get('json', kwargs.get('data')) for _url, kwargs in self.calls]


@contextmanager
def mock_telegram(response=None, side_effect=None):
    """Patch the Telegram HTTP layer and yield the recorder (see :class:`TelegramMock`)."""
    recorder = TelegramMock(response=response, side_effect=side_effect)
    with patch(TELEGRAM_POST_TARGET, new=recorder):
        yield recorder


class CheckinmeCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.partner_id.tz = TZ

        # --- configuration -------------------------------------------------
        icp = cls.env['ir.config_parameter'].sudo()
        for key in BOT_TOKEN_PARAM_KEYS + ('checkinme.telegram_chat_id',):
            icp.set_param(key, False)  # falsy value: the parameter is removed
        for key, value in BOOL_PARAM_DEFAULTS.items():
            icp.set_param(key, value)
        icp.set_param('checkinme.require_gps', 'True')
        icp.set_param('checkinme.max_distance_m', '500')
        icp.set_param('checkinme.sales_source', 'sale_order')

        # --- users ---------------------------------------------------------
        rep_groups = ('base.group_user,checkinme_sales_activity.group_checkinme_user,'
                      'sales_team.group_sale_salesman')
        manager_groups = rep_groups + ',checkinme_sales_activity.group_checkinme_manager'
        cls.rep = new_test_user(
            cls.env, login='checkinme_test_rep', name='Sok Dara', groups=rep_groups,
            context=MAIL_CONTEXT, tz=TZ)
        cls.manager = new_test_user(
            cls.env, login='checkinme_test_manager', name='Chan Sophea', groups=manager_groups,
            context=MAIL_CONTEXT, tz=TZ)
        cls.other_rep = new_test_user(
            cls.env, login='checkinme_test_other_rep', name='Vanna Rith', groups=rep_groups,
            context=MAIL_CONTEXT, tz=TZ)

        # --- employees -----------------------------------------------------
        Employee = cls.env['hr.employee'].with_context(**MAIL_CONTEXT)
        cls.manager_employee = Employee.create({
            'name': 'Chan Sophea',
            'user_id': cls.manager.id,
            'company_id': cls.company.id,
            'tz': TZ,
            'checkinme_telegram_chat_id': MANAGER_CHAT_ID,
        })
        cls.rep_employee = Employee.create({
            'name': 'Sok Dara',
            'user_id': cls.rep.id,
            'company_id': cls.company.id,
            'tz': TZ,
            'parent_id': cls.manager_employee.id,
        })
        # Not managed by the manager employee (no parent).
        cls.other_rep_employee = Employee.create({
            'name': 'Vanna Rith',
            'user_id': cls.other_rep.id,
            'company_id': cls.company.id,
            'tz': TZ,
        })

        # --- customers -----------------------------------------------------
        Partner = cls.env['res.partner'].with_context(**MAIL_CONTEXT)
        cls.partner_geo = Partner.create({
            'name': 'Phnom Penh Mart',
            'is_company': True,
            'street': 'Street 271',
            'city': 'Phnom Penh',
            'partner_latitude': PARTNER_LAT,
            'partner_longitude': PARTNER_LNG,
        })
        cls.partner_nogeo = Partner.create({
            'name': 'Kampot Corner Store',
            'is_company': True,
            'city': 'Kampot',
        })

        # --- activity types ------------------------------------------------
        cls.type_visit = cls.env.ref('checkinme_sales_activity.activity_type_visit')
        cls.type_call = cls.env.ref('checkinme_sales_activity.activity_type_call')
        cls.type_other = cls.env.ref('checkinme_sales_activity.activity_type_other')

        # --- product used for sales orders ---------------------------------
        cls.product = cls.env['product.product'].create({
            'name': 'CheckinMe Test Product',
            'type': 'consu',
            'list_price': 100.0,
            'taxes_id': [Command.clear()],
            'supplier_taxes_id': [Command.clear()],
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @classmethod
    def _set_param(cls, key, value):
        """Set (or remove, when ``value`` is falsy) a system parameter."""
        cls.env['ir.config_parameter'].sudo().set_param(key, value)

    @classmethod
    def _get_param(cls, key):
        return cls.env['ir.config_parameter'].sudo().get_param(key)

    @classmethod
    def _configure_telegram(cls, token=TEST_BOT_TOKEN, chat_id=TEST_CHAT_ID):
        cls._set_param('checkinme.telegram_bot_token', token)
        cls._set_param('checkinme.telegram_chat_id', chat_id)

    @classmethod
    def _today(cls):
        return cls.env['checkinme.checkin']._get_today()

    @classmethod
    def _employee_of(cls, user):
        return cls.env['hr.employee'].sudo().search(
            [('user_id', '=', user.id), ('company_id', '=', cls.company.id)], limit=1)

    @classmethod
    def _create_checkin(cls, user, **vals):
        """Create a check-in as ``user``: customer visit at partner_geo, roughly 15 m away."""
        values = {
            'employee_id': cls._employee_of(user).id,
            'partner_id': cls.partner_geo.id,
            'checkin_type_id': cls.type_visit.id,
            'latitude': CHECKIN_LAT,
            'longitude': CHECKIN_LNG,
            'accuracy': 8.0,
        }
        values.update(vals)
        return cls.env['checkinme.checkin'].with_user(user).with_context(**MAIL_CONTEXT).create(values)

    @classmethod
    def _create_target(cls, employee, year=None, month=None, **vals):
        today = cls._today()
        values = {
            'employee_id': employee.id,
            'company_id': cls.company.id,
            'year': year or today.year,
            'month': str(month or today.month),
        }
        values.update(vals)
        return cls.env['checkinme.target'].with_context(**MAIL_CONTEXT).create(values)

    @classmethod
    def _create_confirmed_order(cls, user, partner=None, price_unit=100.0, **vals):
        """Create and confirm a one-line sales order for the salesperson ``user``."""
        values = {
            'partner_id': (partner or cls.partner_geo).id,
            'user_id': user.id,
            'company_id': cls.company.id,
            'order_line': [Command.create({
                'product_id': cls.product.id,
                'product_uom_qty': 1.0,
                'price_unit': price_unit,
            })],
        }
        values.update(vals)
        order = cls.env['sale.order'].with_context(**MAIL_CONTEXT).create(values)
        order.action_confirm()
        return order

    def mock_telegram(self, response=None, side_effect=None):
        """Instance shortcut for :func:`mock_telegram`."""
        return mock_telegram(response=response, side_effect=side_effect)

    def _patch_telegram(self, response=None, side_effect=None):
        """Patch the Telegram HTTP layer for the rest of the test and return the recorder."""
        recorder = TelegramMock(response=response, side_effect=side_effect)
        patcher = patch(TELEGRAM_POST_TARGET, new=recorder)
        patcher.start()
        self.addCleanup(patcher.stop)
        return recorder
