# -*- coding: utf-8 -*-
"""Shared fixtures for the ABJ app push notification tests.

No HTTP request ever leaves: :class:`FakeSession` replaces the ``requests.Session`` of the push
service (Web Push endpoints, Google OAuth and FCM all go through it), and mail's own web push
sender is patched too. Browser subscriptions use real EC keys so that the encrypted Web Push
payload can be decrypted and checked (:func:`decrypt_web_push`), and the Firebase service account
uses a throwaway RSA key so that the signed OAuth assertion can be verified.
"""
import base64
import json
import os
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from odoo.addons.mail.tools.jwt import generate_vapid_keys
from odoo.addons.checkinme_sales_activity.tools import fcm

from .common import CheckinmeCommon

SESSION_TARGET = 'odoo.addons.checkinme_sales_activity.models.checkinme_push.requests.Session'
MAIL_PUSH_TARGETS = (
    'odoo.addons.mail.models.mail_thread.push_to_end_point',
    'odoo.addons.mail.models.mail_push.push_to_end_point',
)
PUSH_PARAM_DEFAULTS = {
    'checkinme.push_planned_visit': 'True',
    'checkinme.push_followup_reminder': 'True',
    'checkinme.push_team_checkin': 'False',
    'checkinme.push_odoo_messages': 'True',
}
FCM_PROJECT = 'abj-test-project'
FCM_SEND_URL = 'https://fcm.googleapis.com/v1/projects/%s/messages:send' % FCM_PROJECT
ACCESS_TOKEN = 'ya29.test-access-token'


def b64url_decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def b64url_encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code=200, data=None, text=None):
        self.status_code = status_code
        self._data = data
        self.text = text if text is not None else (json.dumps(data) if data is not None else '')

    def json(self):
        if self._data is None:
            raise ValueError("no JSON")
        return self._data


class FakeSession:
    """Records every ``post`` and answers through ``handler(url, kwargs) -> FakeResponse``.

    Default answers: Google OAuth gives an access token, FCM accepts the message and any other
    URL (a Web Push endpoint) answers 201 Created.
    """

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.handler:
            response = self.handler(url, kwargs)
            if response is not None:
                if isinstance(response, BaseException):
                    raise response
                return response
        return self.default_response(url)

    @staticmethod
    def default_response(url):
        if url == fcm.TOKEN_URL:
            return FakeResponse(200, {'access_token': ACCESS_TOKEN, 'expires_in': 3599, 'token_type': 'Bearer'})
        if url.startswith('https://fcm.googleapis.com/'):
            return FakeResponse(200, {'name': 'projects/%s/messages/0:1' % FCM_PROJECT})
        return FakeResponse(201, text='')

    def close(self):
        self.closed = True

    def urls(self):
        return [url for url, _kwargs in self.calls]

    def calls_to(self, prefix):
        return [(url, kwargs) for url, kwargs in self.calls if url.startswith(prefix)]


def make_subscription(endpoint):
    """A browser push subscription with real keys: (device values, browser private key, auth secret)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    keys = {'p256dh': b64url_encode(public), 'auth': b64url_encode(auth)}
    return {'endpoint': endpoint, 'keys': json.dumps(keys)}, private_key, auth


def decrypt_web_push(body, private_key, auth):
    """Decrypt an aes128gcm Web Push body (RFC 8188 / 8291) received by the browser ``private_key``."""
    salt, idlen = body[:16], body[20]
    sender_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    receiver_public = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = private_key.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth,
               info=b"WebPush: info\x00" + receiver_public + sender_public).derive(shared)
    key = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(key).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b'\x02'), "last record must end with the 0x02 padding delimiter"
    return json.loads(plain[:-1].decode())


def verify_vapid_authorization(header, vapid_public_key):
    """Check the ``vapid t=<jwt>, k=<key>`` header (ES256) and return the JWT claims."""
    assert header.startswith('vapid t='), header
    token_part, key_part = header[len('vapid '):].split(', ')
    token, key = token_part[2:], key_part[2:]
    assert key == vapid_public_key
    header_b64, claims_b64, signature_b64 = token.split('.')
    signature = b64url_decode(signature_b64)
    public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(vapid_public_key))
    public_key.verify(
        encode_dss_signature(int.from_bytes(signature[:32], 'big'), int.from_bytes(signature[32:], 'big')),
        ('%s.%s' % (header_b64, claims_b64)).encode(), ec.ECDSA(hashes.SHA256()))
    assert json.loads(b64url_decode(header_b64))['alg'] == 'ES256'
    return json.loads(b64url_decode(claims_b64))


def make_service_account(private_key=None, **overrides):
    """A Firebase service account JSON (dict) with a throwaway RSA key."""
    private_key = private_key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    info = {
        'type': 'service_account',
        'project_id': FCM_PROJECT,
        'private_key_id': 'test-key-id-1',
        'private_key': pem,
        'client_email': 'abj-push@%s.iam.gserviceaccount.com' % FCM_PROJECT,
        'client_id': '1234567890',
        'token_uri': 'https://oauth2.googleapis.com/token',
    }
    info.update(overrides)
    return info


def verify_rs256(assertion, public_key):
    """Verify a RS256 JWT with ``public_key`` and return (header, claims)."""
    header_b64, claims_b64, signature_b64 = assertion.split('.')
    public_key.verify(b64url_decode(signature_b64), ('%s.%s' % (header_b64, claims_b64)).encode(),
                      padding.PKCS1v15(), hashes.SHA256())
    return json.loads(b64url_decode(header_b64)), json.loads(b64url_decode(claims_b64))


class PushCommon(CheckinmeCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for key, value in PUSH_PARAM_DEFAULTS.items():
            cls._set_param(key, value)
        cls._set_param('checkinme.fcm_service_account', False)
        cls.vapid_private_key, cls.vapid_public_key = generate_vapid_keys()
        cls._set_param('mail.web_push_vapid_private_key', cls.vapid_private_key)
        cls._set_param('mail.web_push_vapid_public_key', cls.vapid_public_key)
        cls._set_param('web.base.url', 'https://erp.abj.example')
        cls.rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.service_account = make_service_account(cls.rsa_key)
        # devices left by other modules' data must not interfere with the counts
        cls.env['mail.push.device'].sudo().search([]).unlink()
        cls.env['checkinme.push.device'].sudo().with_context(active_test=False).search([]).unlink()
        cls.Push = cls.env['checkinme.push']
        cls.Message = cls.env['checkinme.push.message']
        cls.Device = cls.env['checkinme.push.device']

    def setUp(self):
        super().setUp()
        fcm.clear_token_cache()
        self.addCleanup(fcm.clear_token_cache)
        # Safety net: no HTTP request can ever leave, even in tests that do not inspect calls.
        self.session = self._patch_session()
        self.mail_push_calls = []
        for target in MAIL_PUSH_TARGETS:
            patcher = patch(target, new=lambda *args, **kwargs: self.mail_push_calls.append(kwargs))
            patcher.start()
            self.addCleanup(patcher.stop)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _patch_session(self, handler=None):
        """Every ``requests.Session()`` of the push service returns this recorder."""
        session = FakeSession(handler)
        patcher = patch(SESSION_TARGET, new=lambda: session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def _add_web_device(self, user, endpoint=None):
        """Subscribe a browser of ``user``; returns (mail.push.device, browser private key, auth)."""
        endpoint = endpoint or 'https://push.example.com/send/%s' % b64url_encode(os.urandom(8))
        values, private_key, auth = make_subscription(endpoint)
        device = self.env['mail.push.device'].sudo().create(dict(values, partner_id=user.partner_id.id))
        return device, private_key, auth

    def _add_native_device(self, user, token=None, **vals):
        values = {
            'user_id': user.id,
            'token': token or 'fcm-token-%s' % b64url_encode(os.urandom(6)),
            'device_name': 'Galaxy A15',
        }
        values.update(vals)
        return self.Device.sudo().create(values)

    def _configure_fcm(self, info=None):
        self._set_param('checkinme.fcm_service_account', json.dumps(info or self.service_account))

    def _messages(self, **filters):
        domain = [(key, '=', value) for key, value in filters.items()]
        return self.Message.sudo().search(domain, order='id')

    def _cron_triggers(self):
        cron = self.env.ref('checkinme_sales_activity.ir_cron_checkinme_push_queue')
        return self.env['ir.cron.trigger'].sudo().search_count([('cron_id', '=', cron.id)])
