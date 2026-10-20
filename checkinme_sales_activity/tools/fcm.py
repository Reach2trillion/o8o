# -*- coding: utf-8 -*-
"""Firebase Cloud Messaging (HTTP v1 API) client for the ABJ Android app.

Dependency free: the OAuth2 service-account flow (RFC 7523 JWT bearer grant, RS256) is done with
``cryptography`` and the HTTP calls with ``requests``, both already required by Odoo.

    client = FcmClient(service_account_info, dbname=env.cr.dbname)
    ok, error_code, message = client.send(token, title, body, data, session)

Access tokens are cached per process for every (database, service account, key) and refreshed
5 minutes before they expire.
"""
import base64
import json
import logging
import threading
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_logger = logging.getLogger(__name__)

FCM_SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SEND_URL = 'https://fcm.googleapis.com/v1/projects/%s/messages:send'
JWT_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:jwt-bearer'
FCM_ERROR_TYPE = 'type.googleapis.com/google.firebase.fcm.v1.FcmError'
TIMEOUT = 10
ASSERTION_LIFETIME = 3600
TOKEN_REFRESH_MARGIN = 300
DEFAULT_CHANNEL_ID = 'abj_default'
REQUIRED_KEYS = ('project_id', 'client_email', 'private_key')
# FCM refuses messages over 4096 bytes: the notification text is clipped well below that, in
# UTF-8 bytes (3 per Khmer character), so that even the JSON-escaped request stays under the limit
MAX_TITLE_BYTES = 256
MAX_BODY_BYTES = 1024

# (dbname, client_email, private_key_id) -> (access_token, expires_at)
_token_cache = {}
_token_lock = threading.Lock()


class FcmAuthError(Exception):
    """Google refused the service account (wrong / revoked key, clock skew...)."""

    def __init__(self, message, error_code='AUTH_ERROR'):
        super().__init__(message)
        self.error_code = error_code


def clear_token_cache():
    with _token_lock:
        _token_cache.clear()


def parse_service_account(value):
    """Parse and validate the service-account key file downloaded from the Firebase console
    (Project settings > Service accounts > Generate new private key).

    :param value: JSON text (str or bytes) or an already parsed dict
    :return: the service account as a dict
    :raise ValueError: with a message suitable for the user
    """
    if isinstance(value, bytes):
        try:
            value = value.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError("The file is not a text (JSON) file.")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError("The service account is not valid JSON.")
    if not isinstance(value, dict):
        raise ValueError("The service account must be a JSON object.")
    if value.get('type') != 'service_account':
        raise ValueError('This is not a service account key: "type" must be "service_account" '
                         '(do not use google-services.json here).')
    missing = [key for key in REQUIRED_KEYS if not (isinstance(value.get(key), str) and value[key].strip())]
    if missing:
        raise ValueError("The service account is missing: %s." % ', '.join(missing))
    try:
        key = serialization.load_pem_private_key(value['private_key'].encode(), password=None)
    except (ValueError, TypeError):
        raise ValueError("The private key of the service account cannot be read.")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("The private key of the service account must be an RSA key.")
    return value


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def clip_utf8(text, max_bytes):
    """``text`` cut to at most ``max_bytes`` UTF-8 bytes (on a character boundary, with an
    ellipsis when cut)."""
    text = text or ''
    raw = text.encode('utf-8')
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes - 3].decode('utf-8', 'ignore') + '\u2026'


def should_deactivate(error_code, message=''):
    """True when FCM says the registration token will never work again."""
    if error_code in ('UNREGISTERED', 'SENDER_ID_MISMATCH'):
        return True
    return error_code == 'INVALID_ARGUMENT' and 'registration token' in (message or '').lower()


class FcmClient:

    def __init__(self, service_account_info, dbname=''):
        self.info = service_account_info
        self.project_id = service_account_info['project_id']
        self.client_email = service_account_info['client_email']
        self.key_id = service_account_info.get('private_key_id') or ''
        self.token_uri = TOKEN_URL
        self.cache_key = (dbname or '', self.client_email, self.key_id)
        self._private_key = None

    # ------------------------------------------------------------------
    # OAuth2
    # ------------------------------------------------------------------
    def _signed_assertion(self, now=None):
        """RS256 JWT asking for an access token with the Firebase Messaging scope."""
        if self._private_key is None:
            self._private_key = serialization.load_pem_private_key(
                self.info['private_key'].encode(), password=None)
        iat = int(now if now is not None else time.time())
        header = {'alg': 'RS256', 'typ': 'JWT'}
        if self.key_id:
            header['kid'] = self.key_id
        claims = {
            'iss': self.client_email,
            'scope': FCM_SCOPE,
            'aud': self.token_uri,
            'iat': iat,
            'exp': iat + ASSERTION_LIFETIME,
        }
        signing_input = '%s.%s' % (
            _b64url(json.dumps(header, separators=(',', ':')).encode()),
            _b64url(json.dumps(claims, separators=(',', ':')).encode()),
        )
        signature = self._private_key.sign(signing_input.encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
        return '%s.%s' % (signing_input, _b64url(signature))

    def _fetch_access_token(self, session):
        response = session.post(self.token_uri, data={
            'grant_type': JWT_GRANT_TYPE,
            'assertion': self._signed_assertion(),
        }, timeout=TIMEOUT)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code != 200 or not data.get('access_token'):
            error = str(data.get('error') or 'auth_error')
            description = data.get('error_description') or (response.text or '')[:200]
            raise FcmAuthError("Google OAuth %s: %s %s" % (response.status_code, error, description),
                               error_code=error.upper())
        try:
            expires_in = int(data.get('expires_in') or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        return data['access_token'], time.time() + expires_in

    def get_access_token(self, session, force_refresh=False):
        with _token_lock:
            cached = _token_cache.get(self.cache_key)
            if cached and not force_refresh and cached[1] - TOKEN_REFRESH_MARGIN > time.time():
                return cached[0]
            token, expires_at = self._fetch_access_token(session)
            _token_cache[self.cache_key] = (token, expires_at)
            return token

    def invalidate_access_token(self):
        with _token_lock:
            _token_cache.pop(self.cache_key, None)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def build_message(self, token, title, body, data=None, ttl_seconds=86400,
                      channel_id=DEFAULT_CHANNEL_ID, tag=None):
        android_notification = {'channel_id': channel_id or DEFAULT_CHANNEL_ID}
        if tag:
            android_notification['tag'] = tag
        return {
            'message': {
                'token': token,
                'notification': {'title': clip_utf8(title, MAX_TITLE_BYTES), 'body': clip_utf8(body, MAX_BODY_BYTES)},
                # FCM only accepts string values in "data"
                'data': {str(key): '' if value is None else str(value) for key, value in (data or {}).items()},
                'android': {
                    'priority': 'HIGH',
                    'ttl': '%ds' % int(ttl_seconds),
                    'notification': android_notification,
                },
            },
        }

    @staticmethod
    def _parse_error(response):
        """(error_code, message) of an FCM error answer: the FcmError detail code when present
        (UNREGISTERED, SENDER_ID_MISMATCH, QUOTA_EXCEEDED...), else the Google API status."""
        try:
            error = response.json().get('error') or {}
        except (ValueError, AttributeError):
            error = {}
        if not isinstance(error, dict):
            error = {}
        code = None
        for detail in error.get('details') or []:
            if isinstance(detail, dict) and detail.get('@type') == FCM_ERROR_TYPE and detail.get('errorCode'):
                code = detail['errorCode']
                break
        code = code or error.get('status') or 'HTTP_%s' % response.status_code
        message = error.get('message') or (response.text or '')[:200]
        return code, message

    def send(self, token, title, body, data, session, ttl_seconds=86400,
             channel_id=DEFAULT_CHANNEL_ID, tag=None):
        """Send one notification. Never raises.

        :return: ``(ok, error_code, message)``: the FCM message name when ok, else the error code
            (see ``should_deactivate``) and a short description
        """
        payload = self.build_message(token, title, body, data=data, ttl_seconds=ttl_seconds,
                                     channel_id=channel_id, tag=tag)
        url = SEND_URL % self.project_id
        try:
            for attempt in range(2):
                access_token = self.get_access_token(session)
                response = session.post(url, json=payload, timeout=TIMEOUT, headers={
                    'Authorization': 'Bearer %s' % access_token,
                    'Content-Type': 'application/json; charset=UTF-8',
                })
                if response.status_code == 401 and attempt == 0:
                    # revoked / expired token: drop it and retry once with a fresh one
                    self.invalidate_access_token()
                    continue
                break
        except FcmAuthError as exc:
            return False, exc.error_code, str(exc)
        except requests.RequestException as exc:
            return False, 'NETWORK_ERROR', '%s: %s' % (type(exc).__name__, exc)
        except (ValueError, TypeError) as exc:  # unreadable private key
            return False, 'INVALID_CREDENTIALS', str(exc)
        if response.status_code == 200:
            try:
                name = response.json().get('name') or ''
            except (ValueError, AttributeError):
                name = ''
            return True, None, name
        code, message = self._parse_error(response)
        _logger.info("ABJ FCM send failed: %s %s", code, message)
        return False, code, message
