# SAE Books — Trademark Policy

> *Your books. Your database. Your control.*
>
> SAE Books is open-source software under AGPL-3.0. The **code** is
> open. The **brand** — the name "SAE Books," the SAE Books logo, and
> related marks — is not. This document explains what you can and
> cannot do with the brand. The point is not to be hostile to the
> community; it's to make sure that when somebody downloads "SAE Books,"
> they know what they're getting.

---

## 1. The marks

The following are trademarks of SAE Engineering / Sauer Pty Ltd ATF
Saueesti Trust (ABN 87 744 586 592):

- The word mark **"SAE Books"**
- The word mark **"saebooks"** (lowercase, used as a package name and
  module identifier)
- The SAE Books logo (registered separately; see the project website
  for the canonical asset)
- The domain `saebooks.com.au` and any future country-coded equivalents
  (`saebooks.com`, `saebooks.eu`, `saebooks.us` etc.) when registered
  by SAE Engineering

Where this document refers to "the marks," it means all of the above
collectively.

The marks are owned by SAE Engineering. The AGPL-3.0 licence on the
source code grants you copyright permissions; **it does not grant you
any trademark rights** (AGPL-3.0 §7 expressly preserves trademark
restrictions).

## 2. What you can do without asking

You don't need our permission to:

1. **Refer to SAE Books accurately** in articles, blog posts, talks,
   tutorials, books, course material, comparisons, and reviews. Use
   the name. Show screenshots. Reproduce the logo at reasonable size.
   That's nominative use; we encourage it.
2. **Say your product is compatible with SAE Books**, integrates with
   SAE Books, imports from SAE Books, exports to SAE Books, runs
   alongside SAE Books, or is built using SAE Books — provided the
   statement is accurate and does not imply endorsement.
3. **Run a personal, internal, or unmodified copy of SAE Books** under
   the name "SAE Books." If you're hosting it for your own business or
   on a private network for colleagues and you haven't changed the
   software in ways that materially alter its behaviour, you can keep
   the name on the login page. We don't expect every self-hoster to
   rebrand.
4. **Discuss, debug, support, document, train on, or recommend SAE
   Books** in a community capacity, including on forums you operate,
   in classes you teach, or in consulting work — provided you do not
   imply that SAE Engineering endorses or has certified your services
   when it has not.
5. **Quote our trademark in source code** that interacts with SAE
   Books programmatically. Variable names, comments, configuration
   keys, OpenAPI tags, and so on may use the marks where useful for
   clarity.

If you fit one of the cases above, you don't need to ask. Just be
accurate.

## 3. What requires our permission

You need written permission from SAE Engineering before doing any of
the following:

1. **Publishing a fork or derivative work under the name "SAE Books"
   or any confusingly similar name.** This includes "SAE Books LTS,"
   "SAE Books Plus," "SAE Books Reloaded," "OpenSAEBooks," "SAE Books
   Community Edition (Maintained)," and so on. AGPL-3.0 lets you
   distribute the modified source; the trademark policy requires you
   to do so under a different name. Suggested rename pattern: pick
   something distinct. We've seen good results with names that hint
   at the lineage without copying the brand (e.g. "Ledger Foundry
   based on SAE Books," not "SAE Books Foundry").
2. **Selling commercial services under the SAE Books name** —
   bookkeeping services, hosted "SAE Books" instances, certified
   training, paid consulting branded as "SAE Books authorised" — when
   no such authorisation exists.
3. **Using the SAE Books logo on products, packaging, marketing
   collateral, swag, or business cards** in a way that could be
   understood as endorsement, certification, or partnership.
4. **Registering a domain name** that includes "saebooks" or "sae
   books" in the second-level label and could be confused with our
   own. Subdomains of existing community sites (e.g.
   `saebooks.example-community.org`) for genuine community-discussion
   purposes are fine; `saebooks-hosting.example.com` for a paid
   service is not.
5. **Translating "SAE Books" into another language as a brand name**
   for your product (e.g. running a German fork as "SAE Bücher"). The
   marks are not translatable without permission.
6. **Filing a trademark application for "SAE Books," "saebooks," or a
   confusingly similar mark** in any jurisdiction.

If you want to do any of the above, email
`trademark@saebooks.com.au` describing your intended use. We respond
within 14 days. We're not unreasonable; the process exists so we know
what's out there.

## 4. Specific cases worth calling out

### 4.1 Forking the source for your own use

Allowed under AGPL. No trademark issue if you don't redistribute or
publicise the fork.

### 4.2 Forking the source and publishing the fork

Allowed under AGPL **provided you rename** the fork. Strip the SAE
Books name and logo from:

- the package name (`pyproject.toml`)
- the docker image tags
- the login page header
- the favicon
- the documentation site

You can keep an "Originally based on SAE Books" credit anywhere
sensible — that's accurate attribution and we welcome it.

### 4.3 Hosted SaaS forks

If your business plan is "run a hosted SAE Books for paying
customers": you have two paths. Either run our codebase unmodified
and call it SAE Books (we offer a partner programme — talk to us);
or run a meaningfully modified fork under a different name. AGPL-3.0
forces you to publish your modifications either way; the trademark
policy decides which name you put on the login page.

### 4.4 Plugins, themes, and integrations

You can name your plugin or integration "SAE Books X" or "X for SAE
Books" provided your name is the first word and "SAE Books" appears
only in the descriptive part. Examples:

- ✅ `Stripe Bridge for SAE Books`
- ✅ `MYOB Migration Assistant for SAE Books`
- ✅ `xyz-saebooks-import` (PyPI / npm package)
- ❌ `SAE Books Stripe Bridge`
- ❌ `SAE Books Migration Tools`
- ❌ `SAE Books Pro Theme`

The reason: the second pattern reads like an official SAE Books
product. The first reads like a third-party tool that integrates,
which is what it is.

### 4.5 Educational and community content

Tutorials, video courses, books, and conference talks about SAE Books
are welcome and don't need permission. If you'd like to use the logo
on a course landing page or book cover, ask — we usually say yes and
provide higher-resolution assets.

### 4.6 Press, reviews, comparisons

Use the name. Use a screenshot. We'd appreciate a heads-up before
publication on `press@saebooks.com.au` so we can answer questions if
you have them, but it's not required.

## 5. Enforcement

We don't pursue trademark cases over honest mistakes. If you've done
something on the "needs permission" list without asking, the typical
sequence is:

1. We email you describing what we noticed and why we think it's
   over the line.
2. You either rename, remove the use, or explain why it's fine — most
   cases get resolved here.
3. If we genuinely can't agree, we may escalate to formal legal
   action.

We will pursue cases where:

- Customers are being misled into thinking a fork or service is
  official.
- Someone is squatting domains or trademarks to extract a payout.
- The use is materially deceptive — for example, a "SAE Books
  Verified" badge on a third-party product that has not been
  reviewed by us.

## 6. Changes to this policy

We may update this document. Substantive changes will:

- Be announced on the project blog and in release notes.
- Take effect 30 days after publication.
- Not apply retroactively to uses that were compliant under the
  previous version.

## 7. Why this exists

A community lives or dies on trust in the name. If anyone can publish
a fork called "SAE Books," users have no way to know whether the thing
they downloaded is the project we maintain or a stale fork from three
years ago with different security properties. Trademark policy is the
mechanism that lets us keep the source open while still giving users
something they can rely on when they see the name.

The source-code freedoms in AGPL-3.0 and the brand-protection rules
in this document are designed to coexist. You have full rights to the
code; we keep the name straight.

## 8. Contact

- **Trademark questions / permission requests:**
  `trademark@saebooks.com.au`
- **Press / media:** `press@saebooks.com.au`
- **Reporting suspected misuse:** `trademark@saebooks.com.au`

---

*TRADEMARK v1.0 — 2026-04-26.*
