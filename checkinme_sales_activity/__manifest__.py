# -*- coding: utf-8 -*-
{
    'name': 'CheckinMe Sales Activity',
    'summary': 'GPS check-ins, customer visits, monthly KPI targets and Telegram reports for outside sales teams',
    'description': """
CheckinMe Sales Activity System
===============================
Mobile-friendly sales activity management and tracking for outside sales teams.

* GPS Check-ins: verify field visits with real-time location capture linked to Google Maps,
  with automatic distance verification against the customer's geolocation.
* Customer Management: every visit / meeting is tied to a customer, a time and a location,
  with notes, outcome, photo proof and follow-up dates.
* KPI Tracking: monthly targets per salesperson (visits, new customers, orders, sales amount)
  monitored in real time with progress bars and on-track / behind status.
* Telegram Integration: instant check-in / check-out notifications to managers and
  automatic daily, weekly and monthly activity reports sent to a Telegram group.
* Performance Reports: daily, weekly, monthly and yearly results per employee
  (pivot / graph analysis, printable PDF activity report).
""",
    'version': '18.0.1.0.0',
    'category': 'Sales/Sales',
    'author': 'Reach2trillion',
    'website': 'https://github.com/Reach2trillion/o8o',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'hr',
        'sale',
        'base_geolocalize',
    ],
    'data': [
        # security
        'security/checkinme_security.xml',
        'security/ir.model.access.csv',
        # data
        'data/ir_sequence_data.xml',
        'data/checkinme_activity_type_data.xml',
        'data/ir_cron_data.xml',
        # views
        'views/checkinme_activity_type_views.xml',
        'views/checkinme_checkin_views.xml',
        'views/checkinme_target_views.xml',
        'views/checkinme_telegram_log_views.xml',
        'views/checkinme_performance_report_views.xml',
        'views/hr_employee_views.xml',
        'views/res_partner_views.xml',
        'views/sale_order_views.xml',
        'views/res_config_settings_views.xml',
        # wizard
        'wizard/checkinme_report_wizard_views.xml',
        # reports
        'report/checkinme_report_templates.xml',
        'report/checkinme_report_actions.xml',
        # menus (last: references all actions)
        'views/checkinme_menus.xml',
    ],
    'demo': [
        'demo/checkinme_demo.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'checkinme_sales_activity/static/src/**/*',
        ],
    },
    'images': ['static/description/banner.png'],
    'application': True,
    'installable': True,
    'auto_install': False,
}
