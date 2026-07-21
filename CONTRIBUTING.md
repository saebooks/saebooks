# Contributing to SAE Books

**SAE Books is in public beta and contributions are welcome.** Bug
reports and pull requests are appreciated — a signed CLA is required
before any pull request can be merged (CLA coverage only works if it's
in place from the first external PR).

## Licence

SAE Books is licensed under AGPL-3.0. By contributing, you license your
contribution to the project under the same terms.

## Contributor License Agreement (CLA)

Every contributor must sign the SAE Books CLA before any pull request
can be merged. This includes:

- Individuals submitting personal work.
- Employees submitting work their employer owns (employer signs the
  corporate CLA in addition to the individual CLA).

The CLA is modelled on the **Apache Individual Contributor License
Agreement (ICLA)**. In plain English, it does two things:

1. You grant Richard Sauer / SAE Engineering the right to distribute
   your contribution under AGPL-3.0 *and* under alternative commercial
   licences. This keeps dual-licensing and paid exemptions possible
   without re-contacting every contributor.
2. You warrant that the contribution is yours to give — not owned by an
   employer who hasn't agreed, not derived from non-compatible code.

The signed CLA is a standing agreement covering all current and future
contributions by that contributor.

## Code of Conduct

- Be kind. Be technical. Disagreements are about code, not people.
- Harassment, discrimination, or personal attacks get a single warning,
  then a ban.
- If you're unsure whether something is OK, ask privately first.

## How to contribute (once public)

1. Open an issue describing the bug, feature, or change.
2. Wait for a reply confirming it's in scope and not already claimed.
3. Fork, branch, implement, test.
4. Sign the CLA (one-time, per contributor).
5. Open a pull request referencing the issue.
6. Address review feedback.
7. Once merged, your contribution is in.

## Commit style

- One logical change per commit.
- Imperative subject line, ≤72 characters.
- Body explains *why*, not *what* (the diff shows what).
- Co-author trailers welcome for paired work.

## Tests

- Every bug fix includes a regression test.
- Every feature includes user-facing docs + tests.
- Money arithmetic changes require Hypothesis property tests.
- CI must be green before merge.

## Security

Do not open public issues for security vulnerabilities. Email
`security@saee.com.au` with:

- Description of the issue
- Reproduction steps
- Impact assessment
- Suggested fix (optional)

We will respond within 72 hours and coordinate a disclosure timeline.

## Questions

For anything not covered here, open a discussion on the repo (once
public) or email the maintainer.
