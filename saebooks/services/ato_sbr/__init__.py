"""ATO SBR (Standard Business Reporting) integration — COMMUNITY EDITION.

Houses the Machine Credential onboarding wizard's DB/state layer plus
STP Phase 2 payroll lodgement and BAS e-lodgement scaffolding. AUSkey is
retired — STP / BAS lodgement uses a Machine Credential issued via RAM
(Relationship Authorisation Manager) linked to the admin's myGovID, plus a
Software Service ID (SSID) from ATO Software Developer onboarding.

The community (AGPL) edition ships this package's wizard bookkeeping, but
``ato_sbr.keystore.load_keystore`` and ``ato_sbr.ping.ping_environment`` —
the actual Machine Credential parsing and ATO reachability checks — are
community-edition stubs. Certified e-lodgement (this package plus the SBR
document generators under ``services.lodgement.sbr``) is a commercial SAE
Books feature; see CHARTER.md / LICENSING.md.
"""
