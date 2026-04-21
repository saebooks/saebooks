"""Import + export helpers for SAE Books.

Three surfaces:

* ``bank_csv`` — CSV parsers for the big-four AU banks (CBA/ANZ/NAB/Westpac)
  producing ``ParsedLine`` rows.
* ``bank_ofx`` — OFX 1.x/2.x parser producing the same ``ParsedLine`` shape.
* ``coa`` — Chart-of-Accounts CSV round-trip (export + diff-preview import).
* ``qbo`` — QBO export CSV importer for contacts + CoA. Invoice/bill/payment
  import is deferred — migrating open balances needs the user to confirm
  every number, which wants an interactive flow.

Every surface is a pure function where possible — parsers take bytes or a
text string and return dataclasses. Persistence happens in a separate
``persist_*`` function so the router can render a preview page without
touching the DB.
"""

from saebooks.services.imports.bank_csv import (
    BankCsvFormat,
    ParsedLine,
    detect_format,
    parse_bank_csv,
)
from saebooks.services.imports.bank_ofx import parse_ofx
from saebooks.services.imports.coa import (
    CoaDiff,
    CoaRow,
    apply_coa_diff,
    diff_coa,
    export_coa_csv,
    parse_coa_csv,
)
from saebooks.services.imports.persist import persist_bank_lines
from saebooks.services.imports.qbo import (
    QboCoaRow,
    QboContactRow,
    parse_qbo_accounts,
    parse_qbo_contacts,
)

__all__ = [
    "BankCsvFormat",
    "CoaDiff",
    "CoaRow",
    "ParsedLine",
    "QboCoaRow",
    "QboContactRow",
    "apply_coa_diff",
    "detect_format",
    "diff_coa",
    "export_coa_csv",
    "parse_bank_csv",
    "parse_coa_csv",
    "parse_ofx",
    "parse_qbo_accounts",
    "parse_qbo_contacts",
    "persist_bank_lines",
]
