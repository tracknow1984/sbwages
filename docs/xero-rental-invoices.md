# BinFlow rental invoice connection

Open Setup → Xero connection. Create a Xero web app using standard OAuth 2.0
authorisation code, with the displayed HTTPS redirect URI. Save its Client ID
and secret in the admin form, connect, and select the intended AUD organisation.
The app's APP_URL must be the canonical public HTTPS origin, and SECRET_KEY must
remain stable: changing it invalidates stored encrypted Xero credentials.

The connection requests offline_access, accounting.invoices,
accounting.contacts.read and accounting.settings.read. Client secrets and both
tokens are encrypted with a purpose-specific key derived from SECRET_KEY. No
tokens are placed in Flask sessions, templates or logs. Token renewal is
serialised in SQLite. New additive tables hold connection state and invoice
history; existing bin and operational data are unchanged.

Connect to Xero completes the CSRF-protected POST on a BinFlow handoff page,
then uses an ordinary top-level navigation to Xero. This keeps Xero's redirects
out of the browser's form-action restrictions, including when already signed
in to Xero. A visible Continue to Xero link remains available if automatic
navigation is disabled. Form submissions retain the application's self-only
content security policy.

Rental Income → Rental invoices offers a labelled sample draft (1–10 bins) or
actual rentals. Choose an existing Xero contact, active income account, tax rate
and inclusive 14-day period. Billing choices are retrieved from Xero and are
valid for one hour; refresh if needed. Test drafts do not change bin allocations.

Actual billing is a reviewed CURRENT SNAPSHOT, not a historical rental ledger.
Current billable quantities and weekly rates are multiplied by two. The admin
must confirm that they apply throughout the selected fortnight. Changes within
the period require review; automatic historical proration is not implemented.
Missing actual rates, flagged bins, empty actual stock and overlapping actual
periods block preparation. Test drafts are explicitly marked TEST ONLY in their
reference and every line. Use a Xero demo organisation for initial tests where
possible; never approve or email a test draft as a real customer charge.

Previews freeze quantities, prices, selections, organisation and payload.
Exports always specify ACCREC / DRAFT / AUD and never call approval, email or
payment endpoints. A durable claim and Xero Idempotency-Key prevent duplicate
submissions. Any uncertain result is retained and cannot be resent blindly.
Check Xero result retrieves the original reference to reconcile an accepted
invoice. If no unique invoice is found, investigate in Xero before any further
export. Cancel only works for previews that have never been sent.

All routes require administrator access; POST routes use existing application
CSRF protection. OAuth state is session-bound, user-bound, expires after ten
minutes and is consumed once. Contact, account and tax selectors are validated
against Xero choices, and organisation changes cannot redirect an old preview.

Validation: python -m unittest discover -s tests -p 'test_binflow_xero.py'
Navigation check: node tests/test_binflow_xero_connect.js

Official references:
- https://developer.xero.com/documentation/guides/oauth2/auth-flow/
- https://developer.xero.com/documentation/guides/oauth2/scopes/
- https://developer.xero.com/documentation/api/accounting/invoices/
- https://developer.xero.com/documentation/guides/idempotent-requests/idempotency/
