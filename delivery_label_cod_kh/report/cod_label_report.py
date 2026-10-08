# -*- coding: utf-8 -*-
from odoo import api, models


class ReportCodLabel(models.AbstractModel):
    """Rendering values of the 100 x 80 mm COD label.

    All the label data is computed in Python by ``stock.picking._kh_get_label_values()`` (one dict
    per parcel) so the QWeb template stays declarative. The ambiguity guard runs here as well, so
    printing from the Print menu is protected like the "COD Label" button.
    """
    _name = 'report.delivery_label_cod_kh.report_cod_label'
    _description = 'COD Delivery Label 100x80'

    @api.model
    def _get_report_values(self, docids, data=None):
        pickings = self.env['stock.picking'].browse(docids).exists()
        pickings._kh_check_cod_ambiguity()
        return {
            'doc_ids': pickings.ids,
            'doc_model': 'stock.picking',
            'docs': pickings,
            'data': data,
            'labels': pickings._kh_get_label_values(),
        }
