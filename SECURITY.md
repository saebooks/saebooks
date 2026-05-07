# Security Policy

## Reporting a vulnerability

Please email <security@saee.com.au> with:

- A description of the issue
- Reproduction steps (or a minimal proof of concept)
- Your assessment of the impact
- Optionally, a suggested fix

**Do not open a public GitHub issue for vulnerabilities.** We will
acknowledge your report within 72 hours and coordinate a disclosure
timeline with you.

## Supported versions

SAE Books is in public alpha (v0.1). Only the latest tagged release is
currently supported with security fixes. Once v1.0 ships, this policy
will be updated to cover the most recent two minor versions.

## Scope

In scope:

- The `saebooks` (API), `saebooks-web`, and `saebooks-desktop` repos.
- Pre-built Docker images published under the `saebooks/*` namespace
  on Docker Hub.
- The Windows installer attached to a tagged release.

Out of scope:

- Third-party dependencies — please report those upstream first; we'll
  pick them up in our next dependency-bump cycle.
- Self-hosted instances run by third parties (configuration issues are
  the operator's responsibility unless they stem from an unsafe
  default in the project itself).

## Hall of fame

Reporters who follow responsible disclosure are credited (with their
permission) in the release notes for the fix.
