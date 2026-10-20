# -*- coding: utf-8 -*-
"""Tests of the installable ABJ app: scoped app manifest, icons, service worker additions, the
stable /odoo/abj URL and the actions opening it."""
import io
from urllib.parse import urlencode

from PIL import Image

from odoo.tests.common import HttpCase, new_test_user, tagged
from odoo.tools import file_open

from .common import CheckinmeCommon, MAIL_CONTEXT, TZ

APP_ID = 'checkinme_sales_activity'
SNIPPET_PATH = 'checkinme_sales_activity/static/src/sw/abj_service_worker.js'
WEB_SW_MARKER = 'odoo-sw-cache'  # web/static/src/service_worker.js
MAIL_SW_MARKER = 'handlePushEventMessageFns'  # mail/static/src/service_worker.js


@tagged('post_install', '-at_install')
class TestAbjWebApp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        groups = 'base.group_user,checkinme_sales_activity.group_checkinme_user,sales_team.group_sale_salesman'
        cls.rep = new_test_user(cls.env, login='abj_http_rep', name='Sok Dara', groups=groups,
                                context=MAIL_CONTEXT, tz=TZ)
        cls.env['hr.employee'].with_context(**MAIL_CONTEXT).create({
            'name': 'Sok Dara', 'user_id': cls.rep.id, 'tz': TZ})
        with file_open(SNIPPET_PATH) as snippet:
            cls.snippet = snippet.read()

    def _manifest(self, app_id=APP_ID, path='/odoo/abj', app_name=''):
        query = {'app_id': app_id, 'path': path}
        if app_name:
            query['app_name'] = app_name
        response = self.url_open('/web/manifest.scoped_app_manifest?%s' % urlencode(query))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], 'application/manifest+json')
        return response.json()

    def test_scoped_app_manifest(self):
        manifest = self._manifest()
        self.assertEqual(manifest['name'], 'ABJ')
        self.assertEqual(manifest['short_name'], 'ABJ')
        self.assertEqual(manifest['id'], '/odoo/abj')
        self.assertEqual(manifest['scope'], '/odoo/abj')
        self.assertEqual(manifest['start_url'], '/odoo/abj')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['background_color'], '#f7f2ed')
        self.assertEqual(manifest['theme_color'], '#22343a')
        self.assertTrue(manifest['description'])
        self.assertEqual([(icon['sizes'], icon['type'], icon['purpose']) for icon in manifest['icons']], [
            ('192x192', 'image/png', 'any'), ('512x512', 'image/png', 'any'), ('512x512', 'image/png', 'maskable')])
        for icon in manifest['icons']:
            response = self.url_open(icon['src'])
            self.assertEqual(response.status_code, 200, icon['src'])
            image = Image.open(io.BytesIO(response.content))
            self.assertEqual(image.format, 'PNG')
            self.assertEqual('%sx%s' % image.size, icon['sizes'])
        # the user may rename the app on the install page
        self.assertEqual(self._manifest(app_name='ABJ Sales')['name'], 'ABJ Sales')

    def test_other_app_manifest_unchanged(self):
        manifest = self._manifest(app_id='mail', path='/odoo/discuss')
        self.assertNotEqual(manifest['name'], 'ABJ')
        self.assertNotIn('id', manifest)
        self.assertEqual(manifest['background_color'], '#714B67')
        self.assertEqual(manifest['scope'], '/odoo/discuss')
        self.assertFalse(any('abj' in icon['src'] for icon in manifest['icons']))

    def test_scoped_app_icon_png(self):
        response = self.url_open('/scoped_app_icon_png?app_id=%s' % APP_ID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Content-Type'], 'image/png')
        image = Image.open(io.BytesIO(response.content))
        self.assertEqual((image.format, image.size), ('PNG', (180, 180)))
        self.assertEqual(image.mode, 'RGB', "iOS home screen icons must not be transparent")
        with file_open('checkinme_sales_activity/static/img/abj-apple-180.png', 'rb') as icon:
            self.assertEqual(response.content, icon.read())
        # other apps keep Odoo's behaviour
        response = self.url_open('/scoped_app_icon_png?app_id=mail&add_padding=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Image.open(io.BytesIO(response.content)).format, 'PNG')
        with file_open('checkinme_sales_activity/static/img/abj-apple-180.png', 'rb') as icon:
            self.assertNotEqual(response.content, icon.read())

    def test_install_page(self):
        response = self.url_open('/scoped_app?app_id=%s&app_name=ABJ&path=odoo/abj' % APP_ID)
        self.assertEqual(response.status_code, 200)
        self.assertIn('/scoped_app_icon_png?app_id=%s' % APP_ID, response.text)
        self.assertIn('manifest.scoped_app_manifest?app_id=%s' % APP_ID, response.text)

    def _service_worker(self):
        response = self.url_open('/web/service-worker.js')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Service-Worker-Allowed'], '/odoo')
        return response.text

    def test_service_worker_public(self):
        body = self._service_worker()
        self.assertTrue(body.startswith(self.snippet), "the ABJ listeners come first")
        self.assertIn(WEB_SW_MARKER, body)
        self.assertNotIn(MAIL_SW_MARKER, body)

    def test_service_worker_logged_in(self):
        self.authenticate('abj_http_rep', 'abj_http_rep')
        body = self._service_worker()
        self.assertTrue(body.startswith(self.snippet))
        self.assertLess(len(self.snippet), body.index(WEB_SW_MARKER))
        self.assertLess(body.index(WEB_SW_MARKER), body.index(MAIL_SW_MARKER))
        self.assertEqual(body.count('options.data.abj'), self.snippet.count('options.data.abj'))

    def test_abj_url_opens_the_screen(self):
        """/odoo/abj is the action path: the phone screen opens full screen (no Odoo navbar)."""
        code = """
            const start = Date.now();
            (function check() {
                const loaded = document.querySelector('.o_checkinme_screen')
                    && !document.querySelector('.o_checkinme_loading');
                // the web client removes its navbar right after the full screen action is mounted
                if (loaded && !document.querySelector('.o_main_navbar')) {
                    if (!location.pathname.startsWith('/odoo/abj')) {
                        console.error('unexpected URL ' + location.pathname);
                    } else {
                        console.log('test successful');
                    }
                } else if (Date.now() - start > 30000) {
                    console.error(loaded ? 'the ABJ screen should be full screen' : 'the ABJ screen did not load');
                } else {
                    setTimeout(check, 100);
                }
            })();
        """
        self.browser_js('/odoo/abj', code, login='abj_http_rep', timeout=90)


@tagged('post_install', '-at_install')
class TestAbjActions(CheckinmeCommon):

    def test_screen_action(self):
        action = self.env.ref('checkinme_sales_activity.action_checkinme_screen')
        self.assertEqual((action.path, action.target, action.tag),
                         ('abj', 'fullscreen', 'checkinme_sales_activity.screen'))
        install = self.env.ref('checkinme_sales_activity.action_checkinme_install')
        self.assertEqual(install.url, '/scoped_app?app_id=checkinme_sales_activity&app_name=ABJ&path=odoo/abj')

    def test_open_in_screen_server_action(self):
        visit = self._create_checkin(self.rep)
        server_action = self.env.ref('checkinme_sales_activity.server_action_open_in_screen')
        result = server_action.with_user(self.rep).with_context(
            active_model='checkinme.checkin', active_id=visit.id, active_ids=[visit.id]).run()
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'checkinme_sales_activity.screen')
        self.assertEqual(result['path'], 'abj')
        self.assertEqual(result['target'], 'fullscreen')
        self.assertEqual(result['context'], {'checkinme_open_id': visit.id})
