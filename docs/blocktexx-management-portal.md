# BlockTexx management portal

`/binflow/client/login` opens a separate client session. The only production client account is `blocktexx`; its initial password is provisioned as a one-time hash. The migration disables prior client accounts and revokes their sessions. Later deployments preserve password changes and access selections. Staff authentication is unchanged.

Three main tabs:
- Collection calendar: current week by default, state filter, published saved weeks only, read-only stop progress refreshed every 20 seconds. No internal notes or driver details.
- Daily PODs: final signed, weighed records with their existing QR codes and bin journeys.
- Collection requests: scoped site requests and scheduling status, without staff review actions or requester contact details.

Initial site scope comes from explicitly BlockTexx-tagged import provenance. Admins use Setup → Client access to review/add collection sites or reset the password. Membership is site-specific, including when an unrelated site shares the same client company. New sites require explicit admin selection. POD access additionally checks historical client ownership. The site restriction covers lists, PDF downloads, QR images, signatures and journey routes.

The production bootstrap runs after all register imports. Tests invoke the same bootstrap explicitly on isolated fixtures. Regression coverage includes previous account revocation, migration idempotence, draft calendar exclusion, same-company site isolation, state filtering, private notes, unsigned POD exclusion, direct PDF/QR authorization and access-change session revocation. Mobile browser checks exercise login, all three tabs, signed POD navigation and logout.
