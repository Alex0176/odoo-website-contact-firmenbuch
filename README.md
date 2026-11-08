# Odoo Website Contact Firmenbuch

Odoo 19.0 addon by **Biricon IT Services GmbH** (LGPL-3).

`website_contact_firmenbuch` replaces the "send us an email" button of the contact page by a form that
identifies the company in the Austrian business register (Firmenbuch):

* first name, last name, company name, email, phone, message, consent;
* while the visitor types the company name the register is searched (module
  [`partner_firmenbuch_at`](https://github.com/Alex0176/odoo-partner-firmenbuch-at)); the visitor picks the
  company from the hits, so it is identified by its register number;
* a small puzzle (signed, expires, one answer per puzzle), a honeypot field, a minimum fill time and rate limits
  keep bots out - no third party service is involved;
* on submit the register extract is read again on the server (nothing the browser sends is trusted) and the module
  creates the company (register data, status check) unless it exists, the contact person below it, and an
  opportunity (team and source configurable), and sends one internal email to the sales team
  (`Auto-Submitted`, no reply to the visitor, reply-to = visitor). The visitor gets no email.
* a company that is not in the register can tick "not listed": only the opportunity is created then.

## Use

Put `<t t-call="website_contact_firmenbuch.contact_form"/>` where the form shall appear (for example in the
website specific copy of `website.contactus`). The form is inline HTML/JS, so no asset bundle has to be rebuilt.

System parameters: `website_contact_firmenbuch.recipient` (default: email of the company),
`website_contact_firmenbuch.team_id` (sales team id of the opportunity, optional).

Needs the API key of the register in `partner_firmenbuch_at`. Routes (JSON): `/biricon/kontakt/puzzle`,
`/biricon/kontakt/firmensuche`, `/biricon/kontakt/senden`.
