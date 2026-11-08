# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import base64
import collections
import hashlib
import hmac
import logging
import re
import secrets
import time

from markupsafe import Markup, escape

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError, ValidationError
from odoo.addons.partner_firmenbuch_at.models.firmenbuch_client import (
    FirmenbuchError, format_fnr, normalize_fnr)

_logger = logging.getLogger(__name__)

PUZZLE_MIN_SECONDS = 6
PUZZLE_MAX_SECONDS = 2 * 3600
MAX_HITS = 12

# (bucket, limit, window in seconds); the counters live in the memory of the worker process
LIMITS = {"puzzle": (60, 600), "search": (25, 600), "submit": (6, 3600)}
GLOBAL_SEARCH = (400, 3600)
_hits = collections.defaultdict(collections.deque)


def _rate_limited(bucket, key, limit, window):
    now = time.monotonic()
    queue = _hits[(bucket, key)]
    while queue and queue[0] < now - window:
        queue.popleft()
    if len(queue) >= limit:
        return True
    queue.append(now)
    if len(_hits) > 5000:
        for k in [k for k, q in _hits.items() if not q or q[-1] < now - 3600]:
            _hits.pop(k, None)
    return False


class WebsiteContactNonce(models.Model):
    """Puzzles that were already tried: every puzzle can be answered once."""
    _name = "website.contact.nonce"
    _description = "Used contact form puzzle"

    nonce = fields.Char(required=True, index=True)

    _nonce_unique = models.Constraint("unique(nonce)", "The puzzle was already used.")

    @api.autovacuum
    def _gc_nonces(self):
        self.env.cr.execute("DELETE FROM website_contact_nonce WHERE create_date < now() - interval '1 day'")


class WebsiteContactFirmenbuch(models.AbstractModel):
    _name = "website.contact.firmenbuch"
    _description = "Contact form with business register check"

    # ------------------------------------------------------------ rate limit
    @api.model
    def _limited(self, bucket, ip):
        limit, window = LIMITS[bucket]
        if _rate_limited(bucket, ip or "?", limit, window):
            return True
        if bucket == "search":
            return _rate_limited("search-all", "*", *GLOBAL_SEARCH)
        return False

    # ------------------------------------------------------------ puzzle
    @api.model
    def _secret(self):
        return (self.env["ir.config_parameter"].sudo().get_param("database.secret") or "").encode()

    @api.model
    def _mac(self, *parts):
        return hmac.new(self._secret(), "|".join(str(p) for p in parts).encode(), hashlib.sha256).hexdigest()[:32]

    @api.model
    def _new_puzzle(self):
        """Question (numbers and symbols only, readable in every language) and the signed token."""
        rnd = secrets.SystemRandom()
        kind = rnd.choice(("sum", "difference", "product", "series", "series", "geometric", "missing"))
        if kind == "sum":
            a, b = rnd.randint(11, 49), rnd.randint(11, 49)
            question, answer = "%d + %d = ?" % (a, b), a + b
        elif kind == "difference":
            a, b = rnd.randint(40, 99), rnd.randint(11, 39)
            question, answer = "%d − %d = ?" % (a, b), a - b
        elif kind == "product":
            a, b = rnd.randint(3, 9), rnd.randint(4, 9)
            question, answer = "%d × %d = ?" % (a, b), a * b
        elif kind == "series":
            start, step = rnd.randint(1, 30), rnd.randint(2, 9)
            seq = [start + i * step for i in range(5)]
            question, answer = ", ".join(map(str, seq[:4])) + ", ?", seq[4]
        elif kind == "geometric":
            start, ratio = rnd.randint(1, 4), rnd.randint(2, 3)
            seq = [start * ratio ** i for i in range(5)]
            question, answer = ", ".join(map(str, seq[:4])) + ", ?", seq[4]
        else:
            a, c = rnd.randint(11, 39), rnd.randint(50, 99)
            question, answer = "? + %d = %d" % (a, c), c - a
        issued, nonce = int(time.time()), secrets.token_hex(8)
        token = "%d.%s.%s.%s" % (issued, nonce, self._mac("s", issued, nonce), self._mac("a", issued, nonce, answer))
        return {"question": question, "token": base64.urlsafe_b64encode(token.encode()).decode()}

    @api.model
    def _token_parts(self, token):
        try:
            issued, nonce, mac_s, mac_a = base64.urlsafe_b64decode((token or "").encode()).decode().split(".")
            issued = int(issued)
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
        if not re.fullmatch(r"[0-9a-f]{16}", nonce) or not hmac.compare_digest(mac_s, self._mac("s", issued, nonce)):
            return None
        if time.time() - issued > PUZZLE_MAX_SECONDS or time.time() - issued < 0:
            return None
        return issued, nonce, mac_a

    @api.model
    def _token_valid(self, token):
        return bool(self._token_parts(token))

    @api.model
    def _check_puzzle(self, token, answer):
        """Error code or None. A token is used up by the first answer, right or wrong."""
        parts = self._token_parts(token)
        if not parts:
            return "puzzle_expired"
        issued, nonce, mac_a = parts
        if time.time() - issued < PUZZLE_MIN_SECONDS:
            return "too_fast"
        try:
            with self.env.cr.savepoint():
                self.env["website.contact.nonce"].sudo().create({"nonce": nonce})
        except Exception:  # noqa: BLE001 - unique violation: already used
            return "puzzle_expired"
        digits = re.sub(r"\s", "", str(answer or ""))
        if not re.fullmatch(r"-?\d{1,6}", digits):
            return "puzzle_wrong"
        if not hmac.compare_digest(mac_a, self._mac("a", issued, nonce, int(digits))):
            return "puzzle_wrong"
        return None

    # ------------------------------------------------------------ register search
    @api.model
    def _search_register(self, query):
        """Hits of the register search by name (list of dicts); raises UserError when the service is down."""
        query = " ".join((query or "").split())
        if len(query) < 3:
            return []
        Partner = self.env["res.partner"].sudo()
        try:
            hits = Partner._fb_find(Partner._fb_client(), query)
        except (FirmenbuchError, UserError) as e:
            _logger.warning("Contact form: business register search failed: %s", e)
            raise UserError(_("The business register is not available at the moment.")) from e
        norm = Partner._fb_norm_name(query)
        hits = sorted(hits, key=lambda h: (not Partner._fb_norm_name(h["name"]).startswith(norm), h["name"].lower()))
        return [{"fnr": h["fnr"], "name": h["name"], "seat": h.get("seat") or "", "court": h.get("court") or "",
                 "legal_form": h.get("legal_form") or "", "label": format_fnr(h["fnr"])}
                for h in hits[:MAX_HITS] if h.get("fnr")]

    # ------------------------------------------------------------ validation
    @api.model
    def _clean_input(self, vals):
        """Cleaned values or an error code."""
        def text(key, limit):
            return " ".join(str(vals.get(key) or "").split())[:limit]
        data = {"first_name": text("first_name", 80), "last_name": text("last_name", 80),
                "company": text("company", 150), "phone": text("phone", 40),
                "email": text("email", 150)}
        data["fnr"] = normalize_fnr(vals.get("fnr")) if vals.get("fnr") else ""
        data["no_register"] = bool(vals.get("no_register"))
        data["message"] = "\n".join(line.rstrip() for line in str(vals.get("message") or "").strip().splitlines())[:5000]
        if not (data["first_name"] and data["last_name"] and data["company"]):
            return None, "missing_name"
        if not data["email"] or not tools.email_normalize(data["email"]) or \
                not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", data["email"]):
            return None, "bad_email"
        data["email"] = tools.email_normalize(data["email"])
        if data["phone"] and not re.fullmatch(r"[0-9+()/.\- ]{5,40}", data["phone"]):
            return None, "bad_phone"
        if len(data["message"]) < 10:
            return None, "message_short"
        if not vals.get("consent"):
            return None, "no_consent"
        if not data["fnr"] and not data["no_register"]:
            return None, "no_company_choice"
        return data, None

    # ------------------------------------------------------------ the request
    @api.model
    def submit(self, vals, ip=None):
        """Handle the form: returns ``None`` when done, otherwise an error code."""
        if vals.get("hp"):
            return "spam"
        self = self.with_context(lang=self.env.company.partner_id.lang or "de_DE")
        data, error = self._clean_input(vals)
        if error:
            return error
        error = self._check_puzzle(vals.get("token"), vals.get("answer"))
        if error:
            return error
        register = self._register_extract(data["fnr"]) if data["fnr"] else None
        partner = contact = self.env["res.partner"]
        if register:
            partner = self._company_partner(register, data)
            contact = self._contact_partner(partner, data)
        lead = self._create_lead(data, register, partner, contact)
        self._send_mail(data, register, partner, contact, lead)
        return None

    @api.model
    def _register_extract(self, fnr):
        """Current register extract, or ``{"error": ...}`` when the service does not answer."""
        Partner = self.env["res.partner"].sudo()
        try:
            data = Partner._fb_client().extract(fnr)
        except (FirmenbuchError, UserError) as e:
            _logger.warning("Contact form: register extract of %s failed: %s", fnr, e)
            return {"fnr": fnr, "error": str(e)}
        if not data.get("name"):
            return {"fnr": fnr, "error": "no name in the extract"}
        data["fnr"] = data.get("fnr") or fnr
        return data

    @api.model
    def _company_partner(self, register, data):
        Partner = self.env["res.partner"].sudo().with_context(
            active_test=False, fb_no_lookup=True, tracking_disable=True, mail_create_nosubscribe=True,
            mail_create_nolog=True)
        if register.get("error"):
            return Partner.browse()
        found = Partner.search([("fb_fnr", "=", normalize_fnr(register["fnr"])), ("parent_id", "=", False),
                                ("is_company", "=", True)],
                               order="active desc, id", limit=1)
        if found:
            return found
        country = self.env["res.country"].sudo().search([("code", "=", register.get("country") or "AT")], limit=1)
        site = register.get("website") or ""
        partner = Partner.create({
            "name": register["name"], "is_company": True, "company_type": "company",
            "street": register.get("street") or False, "zip": register.get("zip") or False,
            "city": register.get("city") or False, "country_id": country.id or False,
            "company_registry": normalize_fnr(register["fnr"]),
            "website": (site if site.startswith("http") else "https://" + site) if site else False,
            "fb_court": register.get("court") or False, "fb_legal_form": register.get("legal_form") or False,
            "fb_managers": ", ".join(register.get("managers") or []) or False,
        })
        status = partner._fb_status_vals(register)
        partner.write(dict(status, fb_last_check=fields.Datetime.now(), fb_checksum=register.get("checksum") or False))
        partner._fb_risk_changed("unknown")
        return partner

    @api.model
    def _contact_partner(self, company, data):
        if not company:
            return company
        Partner = self.env["res.partner"].sudo().with_context(
            active_test=False, fb_no_lookup=True, tracking_disable=True, mail_create_nosubscribe=True,
            mail_create_nolog=True)
        found = Partner.search([("parent_id", "=", company.id), ("email_normalized", "=", data["email"])], limit=1)
        if found:
            return found
        return Partner.create({
            "name": "%s %s" % (data["first_name"], data["last_name"]), "parent_id": company.id, "type": "contact",
            "is_company": False, "email": data["email"], "phone": data["phone"] or False})

    @api.model
    def _team(self):
        team_id = self.env["ir.config_parameter"].sudo().get_param("website_contact_firmenbuch.team_id")
        return self.env["crm.team"].sudo().browse(int(team_id)).exists() if (team_id or "").isdigit() else \
            self.env["crm.team"]

    @api.model
    def _create_lead(self, data, register, partner, contact):
        name = "%s %s" % (data["first_name"], data["last_name"])
        company = (register or {}).get("name") or data["company"]
        Lead = self.env["crm.lead"].sudo().with_context(
            tracking_disable=True, mail_create_nosubscribe=True, mail_create_nolog=True,
            mail_auto_subscribe_no_notify=True)
        lead = Lead.create({
            "name": _("Website enquiry: %(company)s - %(name)s", company=company, name=name)[:250],
            "type": "opportunity", "user_id": False, "team_id": self._team().id or False,
            "partner_id": contact.id or False,
            "partner_name": company, "contact_name": name, "email_from": data["email"],
            "phone": data["phone"] or False,
            "description": tools.plaintext2html(data["message"]),
            "source_id": self.env.ref("website_contact_firmenbuch.utm_source_contact_form").id,
        })
        lead.with_context(mail_notify_force_send=False).message_post(
            body=self._summary_html(data, register, partner, contact, link=False), message_type="comment",
            subtype_xmlid="mail.mt_note")
        return lead

    # ------------------------------------------------------------ texts
    @api.model
    def _manager_match(self, data, register):
        """Does the visitor's name appear among the managing directors of the register?"""
        if not register or register.get("error"):
            return None
        norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("ß", "ss"))  # noqa: E731
        first, last = norm(data["first_name"]), norm(data["last_name"])
        return any(first and last and first in norm(m) and last in norm(m) for m in register.get("managers") or [])

    @api.model
    def _summary_rows(self, data, register, partner, contact):
        rows = [(_("Name"), "%s %s" % (data["first_name"], data["last_name"])),
                (_("Company (entered)"), data["company"]),
                (_("Email"), data["email"]), (_("Phone"), data["phone"] or "-")]
        if register and not register.get("error"):
            rows += [(_("Register number"), format_fnr(register["fnr"])), (_("Company (register)"), register["name"]),
                     (_("Address"), ", ".join(filter(None, [register.get("street"), " ".join(filter(None, [
                         register.get("zip"), register.get("city")]))]))),
                     (_("Legal form"), register.get("legal_form") or "-"), (_("Court"), register.get("court") or "-"),
                     (_("Managing directors"), ", ".join(register.get("managers") or []) or "-")]
            match = self._manager_match(data, register)
            rows.append((_("Name found among managing directors"), _("yes") if match else _("no")))
            if partner:
                rows.append((_("Register status"), "%s%s" % (dict(partner._fields["fb_risk"].selection).get(
                    partner.fb_risk, ""), (": " + partner.fb_risk_note.replace("\n", "; ")) if partner.fb_risk_note
                    else "")))
        elif register:
            rows.append((_("Register number"), "%s (%s)" % (format_fnr(register["fnr"]), _(
                "the register could not be read, the number is not verified"))))
        else:
            rows.append((_("Register number"), _("The visitor is not listed in the Austrian business register")))
        return rows

    @api.model
    def _summary_html(self, data, register, partner, contact, link=True):
        rows = Markup("").join(
            Markup("<tr><td style='padding:2px 12px 2px 0;color:#666;vertical-align:top'>%s</td><td>%s</td></tr>")
            % (k, v) for k, v in self._summary_rows(data, register, partner, contact))
        return Markup("<table>%s</table><p style='white-space:pre-wrap'>%s</p>") % (rows, data["message"])

    @api.model
    def _send_mail(self, data, register, partner, contact, lead):
        recipient = self.env["ir.config_parameter"].sudo().get_param("website_contact_firmenbuch.recipient") \
            or self.env.company.email
        if not recipient:
            _logger.warning("Contact form: no recipient configured; the opportunity %s was created.", lead.id)
            return
        base = (self.env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
        warn = partner and partner.fb_risk in ("warning", "critical")
        subject = "%s%s" % ("[%s] " % _("Check the register status") if warn else "", _(
            "Website enquiry: %(company)s - %(name)s", company=(register or {}).get("name") or data["company"],
            name="%s %s" % (data["first_name"], data["last_name"])))
        links = Markup("<p>")
        links += Markup("<a href='%s/odoo/crm.lead/%d'>%s</a>") % (base, lead.id, _("Open the opportunity"))
        if partner:
            links += Markup(" | <a href='%s/odoo/res.partner/%d'>%s</a>") % (base, partner.id, _("Open the company"))
        links += Markup("</p>")
        body = Markup("<p>%s</p>") % _("A visitor sent the contact form of the website.") + \
            self._summary_html(data, register, partner, contact) + links
        mail = self.env["mail.mail"].sudo().create({
            "subject": subject[:250], "body_html": body, "email_to": recipient,
            "reply_to": tools.formataddr(("%s %s" % (data["first_name"], data["last_name"]), data["email"])),
            "auto_delete": True, "headers": "{'Auto-Submitted': 'auto-generated'}",
        })
        try:
            mail.send(raise_exception=False)
        except Exception:  # noqa: BLE001 - stays queued, the mail queue tries again
            _logger.exception("Contact form: sending the mail to %s failed", recipient)
