"""Phase 0 POC — PySide6 desktop window hitting /api/v1/contacts.

Run OUTSIDE the docker stack: ``python scripts/poc_contacts_qt.py``.
Expects the server reachable at ``http://localhost:8042`` (the port
published by ``docker-compose.yml``) and the bearer token exported as
``SAEBOOKS_DEV_API_TOKEN``.

This is the GO/NO-GO gate for Phase 0. The goal isn't a polished UI —
it's proving the pattern end-to-end: list, create, edit, handle 409
conflicts. Feel is what matters. Leave a subjective paragraph in
``saebooks-autonomous-state.md`` after running it.
"""
from __future__ import annotations

import os
import sys
import uuid
from typing import Any

import httpx

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QHBoxLayout,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # pragma: no cover — script is optional
    sys.stderr.write(
        "PySide6 is not installed. Install with: pip install PySide6\n"
    )
    sys.exit(1)


API_BASE = os.environ.get("SAEBOOKS_API_BASE", "http://localhost:8042/api/v1")
TOKEN = os.environ.get("SAEBOOKS_DEV_API_TOKEN", "")


def _client() -> httpx.Client:
    if not TOKEN:
        raise SystemExit(
            "SAEBOOKS_DEV_API_TOKEN is not set. Export it (same value the "
            "server logged at boot) and re-run."
        )
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# Contact edit dialog
# ---------------------------------------------------------------------------


class ContactDialog(QDialog):
    def __init__(self, parent: QWidget, contact: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New contact" if contact is None else f"Edit {contact['name']}")
        self._contact = contact
        layout = QFormLayout(self)

        self.name = QLineEdit(contact["name"] if contact else "")
        self.email = QLineEdit(contact["email"] or "" if contact else "")
        self.phone = QLineEdit(contact["phone"] or "" if contact else "")
        self.contact_type = QLineEdit(
            contact["contact_type"] if contact else "CUSTOMER"
        )
        layout.addRow("Name", self.name)
        layout.addRow("Type (CUSTOMER/SUPPLIER/BOTH)", self.contact_type)
        layout.addRow("Email", self.email)
        layout.addRow("Phone", self.phone)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip(),
            "contact_type": self.contact_type.text().strip().upper(),
            "email": self.email.text().strip() or None,
            "phone": self.phone.text().strip() or None,
        }


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class ContactsWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SAE Books POC — Contacts")
        self.resize(900, 500)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        bar = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self.new_contact)
        bar.addWidget(refresh)
        bar.addWidget(new_btn)
        bar.addStretch()
        outer.addLayout(bar)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Email", "Phone", "Version"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        outer.addWidget(self.table)

        self._rows: list[dict[str, Any]] = []
        self.refresh()

    def refresh(self) -> None:
        try:
            with _client() as c:
                r = c.get("/contacts", params={"limit": 200})
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPError as exc:
            QMessageBox.critical(self, "API error", f"Failed to load contacts:\n{exc}")
            return
        self._rows = body["items"]
        self.table.setRowCount(len(self._rows))
        for i, c in enumerate(self._rows):
            self.table.setItem(i, 0, QTableWidgetItem(c["name"]))
            self.table.setItem(i, 1, QTableWidgetItem(c["contact_type"]))
            self.table.setItem(i, 2, QTableWidgetItem(c["email"] or ""))
            self.table.setItem(i, 3, QTableWidgetItem(c["phone"] or ""))
            self.table.setItem(i, 4, QTableWidgetItem(str(c["version"])))
        self.statusBar().showMessage(f"Loaded {len(self._rows)} contacts")

    def new_contact(self) -> None:
        dlg = ContactDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.payload()
        try:
            with _client() as c:
                r = c.post(
                    "/contacts",
                    json=payload,
                    headers={"X-Idempotency-Key": str(uuid.uuid4())},
                )
                r.raise_for_status()
        except httpx.HTTPError as exc:
            QMessageBox.critical(self, "Create failed", f"{exc}\n{getattr(exc, 'response', None) and exc.response.text}")
            return
        self.refresh()

    def _on_double_click(self, row: int, _col: int) -> None:
        contact = self._rows[row]
        dlg = ContactDialog(self, contact=contact)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.payload()
        try:
            with _client() as c:
                r = c.patch(
                    f"/contacts/{contact['id']}",
                    json=payload,
                    headers={
                        "If-Match": str(contact["version"]),
                        "X-Idempotency-Key": str(uuid.uuid4()),
                    },
                )
        except httpx.HTTPError as exc:
            QMessageBox.critical(self, "Update failed", str(exc))
            return
        if r.status_code == 409:
            current = r.json().get("current", {})
            ret = QMessageBox.question(
                self,
                "Conflict",
                f"This contact changed on the server (now at version "
                f"{current.get('version')}). Reload and try again?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if ret == QMessageBox.StandardButton.Yes:
                self.refresh()
            return
        if r.is_error:
            QMessageBox.critical(self, "Update failed", f"{r.status_code}: {r.text}")
            return
        self.refresh()


def main() -> None:
    app = QApplication(sys.argv)
    w = ContactsWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
