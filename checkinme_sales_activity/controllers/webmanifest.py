# -*- coding: utf-8 -*-
"""Installable "ABJ" app: Odoo's scoped app (``/scoped_app?app_id=checkinme_sales_activity``)
with our own name, colors and PNG icons, and the ABJ service worker additions (push display and
notification tap handling) in front of Odoo's service worker."""
import json
from urllib.parse import unquote

from odoo import http
from odoo.http import request
from odoo.tools import file_open
from odoo.addons.mail.controllers import webmanifest

APP_ID = 'checkinme_sales_activity'
APP_NAME = 'ABJ'
APP_PATH = '/odoo/abj'
BACKGROUND_COLOR = '#f7f2ed'  # cream
THEME_COLOR = '#22343a'  # deep slate (top bar)
IMG_PATH = 'checkinme_sales_activity/static/img/'
SERVICE_WORKER_SNIPPET = 'checkinme_sales_activity/static/src/sw/abj_service_worker.js'


class WebManifest(webmanifest.WebManifest):

    def _get_scoped_app_icons(self, app_id):
        if app_id != APP_ID:
            return super()._get_scoped_app_icons(app_id)
        return [
            {'src': '/%sabj-192.png' % IMG_PATH, 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/%sabj-512.png' % IMG_PATH, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/%sabj-maskable-512.png' % IMG_PATH, 'sizes': '512x512', 'type': 'image/png',
             'purpose': 'maskable'},
        ]

    @http.route()
    def scoped_app_icon_png(self, app_id, add_padding=False):
        """Home Screen icon of Safari (apple-touch-icon): an opaque 180 px PNG."""
        if app_id != APP_ID:
            return super().scoped_app_icon_png(app_id, add_padding=add_padding)
        with file_open(IMG_PATH + 'abj-apple-180.png', 'rb') as icon:
            content = icon.read()
        return request.make_response(content, headers=[
            ('Content-Type', 'image/png'),
            ('Cache-Control', 'public, max-age=86400'),
        ])

    @http.route()
    def scoped_app_manifest(self, app_id, path, app_name=''):
        response = super().scoped_app_manifest(app_id, path, app_name=app_name)
        if app_id != APP_ID:
            return response
        manifest = json.loads(response.get_data())
        name = unquote(app_name) if app_name else APP_NAME
        manifest.update({
            'id': APP_PATH,
            'name': name,
            'short_name': name,
            'description': request.env._("ABJ SkinCare field sales: GPS check-ins, customer visits and orders."),
            'background_color': BACKGROUND_COLOR,
            'theme_color': THEME_COLOR,
            'categories': ['business', 'productivity'],
        })
        return request.make_json_response(manifest, {
            'Content-Type': 'application/manifest+json',
        })

    def _get_service_worker_content(self):
        """Prepend the ABJ snippet for every visitor (it only reacts to ABJ payloads), so its push
        and notificationclick listeners run before (and can stop) mail's ones, and stay installed
        even when the service worker was refreshed while logged out."""
        body = super()._get_service_worker_content()
        with file_open(SERVICE_WORKER_SNIPPET) as snippet:
            return snippet.read() + '\n' + body
