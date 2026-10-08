# -*- coding: utf-8 -*-
from odoo import models
from odoo.http import request
from odoo.tools.misc import hmac as hmac_tool


class PaymentLinkWizard(models.TransientModel):
    _inherit = 'payment.link.wizard'

    def _prepare_access_token(self):
        """Allow building payment links outside of an HTTP request.

        ``payment.utils.generate_access_token`` signs with ``request.env``, so computing the
        ``link`` of the wizard from a scheduled action, a server-side PDF rendering or the shell
        crashes ("NoneType has no attribute env"). The COD label builds the sale order payment
        link with this wizard, so without a request the very same token (same HMAC scope, message
        and database secret) is computed with the wizard's own environment. Inside a request the
        standard implementation is used unchanged.
        """
        if request:
            return super()._prepare_access_token()
        self.ensure_one()
        token_str = '|'.join(str(value) for value in (self.partner_id.id, self.amount, self.currency_id.id))
        return hmac_tool(self.env(su=True), 'generate_access_token', token_str)
