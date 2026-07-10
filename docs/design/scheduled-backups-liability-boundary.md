# Scheduled backups — the liability boundary (product/terms note)

Status: draft note for the product/terms surface (marketing site,
signup flow, or a Terms of Service clause) — NOT engine code. Nothing
in this file is enforced by the application; it documents what the
*architecture* already guarantees (see
`saebooks/services/backup_crypto.py` and
`saebooks/services/scheduled_backups.py` module docstrings) so
whoever writes the customer-facing terms has the accurate technical
boundary to describe, per planned-modules build plan decision 6 and
[[saebooks-liability-pricing-principle]].

## What SAE Books does

1. Builds a **per-tenant logical export** of your data — never a
   whole-database dump, never another tenant's rows (this is
   architecturally enforced, not policy — see the export module's
   cross-tenant test).
2. **Encrypts it immediately**, using a passphrase **you supply**, with
   a documented, standard algorithm (scrypt key derivation + AES-256-GCM).
3. **Discards the passphrase** the instant encryption finishes. SAE
   Books does not store it, log it, or send it anywhere. From that
   moment on, SAE Books is **structurally unable** to decrypt your own
   export.
4. Makes the encrypted file available to download, and — if you
   configure a destination — copies the *encrypted* file there (a
   local path today; a remote `rclone` destination is planned but not
   yet built, see the extension-point note below).

## What that means for you

* **You own the passphrase.** If you lose it, SAE Books cannot recover
  your backup for you — nobody at SAE can decrypt it either, because
  nobody kept the key. This is the trade: real security instead of a
  recoverable-by-support convenience.
* **You own what happens after download.** Once the encrypted file
  leaves SAE's systems (downloaded to your machine, or pushed to a
  destination you configured), its handling, storage, and protection
  from that point on is your responsibility. SAE Books protected the
  data while it lived in the product (tenant-isolated database rows,
  encrypted the moment it left as a file); it can't protect a copy
  that's no longer on SAE's systems.
* **This is the baseline included at your tier**, not an add-on. You
  are never restricted from doing your own backups your own way — you
  can always bring your own passphrase, your own storage.

## The paid extension point (not yet built)

The `managed_by` field on a backup configuration reserves a future
option where **SAE manages the certificate/key and guarantees the
handling** of your backup end-to-end (custody, availability, recovery
support if you lose access). That is a materially different — and
priced — offer, because it means SAE assumes a liability it does not
assume today: [[saebooks-liability-pricing-principle]]'s "risk is
liability, whoever bears it must be compensated for it." **This option
does not exist yet** — the config field exists so the schema won't
need a migration when it's built, and the API explicitly refuses to
accept `managed_by="sae"` today rather than silently pretending to
offer something that isn't built. When/if this ships, it needs its own
priced tier and its own terms clause — this note does not attempt to
write those terms in advance.

## Suggested terms clause (starting point, not final copy)

> Scheduled Backups produces an export of your own tenant data,
> encrypted with a passphrase you provide before it leaves our
> systems. We do not retain your passphrase and cannot decrypt your
> backup once created — recovery of a lost passphrase is not possible
> through us. You are responsible for safeguarding your passphrase and
> for the security of any location you choose to store or transfer
> your encrypted backup to after it leaves our systems.

This is a starting draft for whoever owns the actual Terms of Service
document — not itself a binding term.
