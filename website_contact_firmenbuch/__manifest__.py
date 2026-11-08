# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
{
    "name": "Website contact form with Austrian business register check",
    "summary": "Contact form that identifies the company in the Austrian business register (Firmenbuch), "
    "asks a puzzle, creates the company, the contact and an opportunity and mails the sales team",
    "version": "19.0.1.0.0",
    "category": "Website/Website",
    "author": "Biricon IT Services GmbH",
    "website": "https://github.com/Alex0176/odoo-website-contact-firmenbuch",
    "license": "LGPL-3",
    "depends": ["website_crm", "partner_firmenbuch_at"],
    "data": [
        "security/ir.model.access.csv",
        "data/data.xml",
        "views/templates.xml",
    ],
    "installable": True,
}
