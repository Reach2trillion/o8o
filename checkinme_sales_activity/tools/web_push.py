# -*- coding: utf-8 -*-
"""Web Push (RFC 8030 / 8291 / 8292) sending for the ABJ app.

Same protocol and encryption as ``odoo.addons.mail.tools.web_push.push_to_end_point`` (it reuses
its payload encryption and VAPID JWT helpers and the browser subscriptions of ``mail.push.device``),
but with a real ``TTL`` and ``Urgency``: mail sends ``TTL: 60``, so a phone that is asleep or
offline for more than a minute never receives the notification, and push services may delay
normal-urgency messages on battery saving devices (iOS, Android doze).
"""
import logging
import textwrap
from urllib.parse import urlsplit

from odoo.addons.mail.tools import jwt
from odoo.addons.mail.tools.web_push import DeviceUnreachableError, _encrypt_payload

_logger = logging.getLogger(__name__)

DEFAULT_TTL = 24 * 3600
URGENCIES = ('very-low', 'low', 'normal', 'high')
VAPID_JWT_TTL = 12 * 3600
TIMEOUT = 10


class WebPushError(Exception):
    """The push service refused the message (the subscription itself is still valid)."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def send_web_push(base_url, device, payload, vapid_private_key, vapid_public_key, session,
                  ttl=DEFAULT_TTL, urgency='high'):
    """Encrypt ``payload`` (a JSON string) for the browser subscription ``device`` and post it to
    its push service endpoint.

    :param str base_url: ``web.base.url``, sent as the VAPID contact (``sub`` claim); Apple's push
        service only accepts an https URL or a mailto: address there
    :param dict device: ``{'id', 'endpoint', 'keys'}`` of a ``mail.push.device`` (keys as JSON text)
    :param session: a ``requests.Session``
    :param int ttl: seconds the push service keeps the message while the device is unreachable
    :param str urgency: one of ``URGENCIES``
    :raise DeviceUnreachableError: the subscription is gone (HTTP 404 / 410, or ``.invalid`` host):
        the device must be deleted
    :raise WebPushError: any other refusal of the push service
    """
    endpoint = device['endpoint']
    url = urlsplit(endpoint)
    # ".invalid" is the TLD reserved for names that can never resolve (RFC 2606), as in mail
    if url.netloc.endswith('.invalid'):
        raise DeviceUnreachableError("Device Unreachable")
    claims = {
        'aud': '{}://{}'.format(url.scheme, url.netloc),
        'sub': base_url,
    }
    token = jwt.sign(claims, vapid_private_key, ttl=VAPID_JWT_TTL, algorithm=jwt.Algorithm.ES256)
    body = _encrypt_payload(payload.encode(), device)
    headers = {
        'Authorization': 'vapid t={}, k={}'.format(token, vapid_public_key),
        'Content-Encoding': 'aes128gcm',
        'TTL': str(int(ttl)),
        'Urgency': urgency if urgency in URGENCIES else 'normal',
    }
    response = session.post(endpoint, headers=headers, data=body, timeout=TIMEOUT)
    if 200 <= response.status_code < 300:
        _logger.debug("ABJ web push sent to %s", url.netloc)
        return True
    message = "%s %s" % (response.status_code, textwrap.shorten(response.text or '', 200))
    _logger.warning("ABJ web push to %s failed: %s", url.netloc, message)
    if response.status_code in (404, 410):
        raise DeviceUnreachableError("Device Unreachable")
    raise WebPushError(message, status_code=response.status_code)
