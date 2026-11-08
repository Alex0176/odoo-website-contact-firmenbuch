# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import logging

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)

FIELDS = ("first_name", "last_name", "company", "fnr", "no_register", "email", "phone", "message", "consent",
          "token", "answer", "hp")


class WebsiteContactFirmenbuch(http.Controller):

    @staticmethod
    def _ip():
        return request.httprequest.remote_addr

    @staticmethod
    def _model():
        return request.env["website.contact.firmenbuch"].sudo()

    @http.route("/biricon/kontakt/puzzle", type="jsonrpc", auth="public", website=True, methods=["POST"],
                csrf=False, readonly=True)
    def puzzle(self):
        model = self._model()
        if model._limited("puzzle", self._ip()):
            return {"error": "rate_limit"}
        return model._new_puzzle()

    @http.route("/biricon/kontakt/firmensuche", type="jsonrpc", auth="public", website=True, methods=["POST"],
                csrf=False, readonly=True)
    def search(self, q="", token=""):
        model = self._model()
        if not model._token_valid(token):
            return {"error": "puzzle_expired"}
        if model._limited("search", self._ip()):
            return {"error": "rate_limit"}
        try:
            return {"hits": model._search_register(str(q)[:80])}
        except UserError:
            return {"error": "register_down"}

    @http.route("/biricon/kontakt/senden", type="jsonrpc", auth="public", website=True, methods=["POST"],
                csrf=False)
    def send(self, **post):
        model = self._model()
        if model._limited("submit", self._ip()):
            return {"error": "rate_limit"}
        try:
            error = model.submit({k: post.get(k) for k in FIELDS}, ip=self._ip())
        except Exception:  # noqa: BLE001
            _logger.exception("Contact form: processing failed")
            request.env.cr.rollback()
            return {"error": "failed"}
        return {"error": error} if error else {"ok": True}
