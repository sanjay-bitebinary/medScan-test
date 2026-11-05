"""Persistent duplicate-invoice registry stored locally in SQLite."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone


DDL = """
CREATE TABLE IF NOT EXISTS scanned_documents (
    job_id TEXT PRIMARY KEY,
    filename TEXT,
    file_sha256 TEXT,
    invoice_keys_json TEXT,
    headers_json TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scanned_documents_hash
ON scanned_documents(file_sha256);
"""


class InvoiceRegistry:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executescript(DDL)

    @staticmethod
    def _invoice_keys(headers: list[dict]) -> list[str]:
        keys: list[str] = []
        for header in headers:
            number = str(header.get("invoice_number") or "").upper().strip()
            party = str(
                header.get("gstin") or header.get("supplier") or ""
            ).upper().strip()
            if number:
                keys.append(f"{party}|{number}")
        return sorted(set(keys))

    def find_duplicates(
        self,
        file_sha256: str,
        headers: list[dict] | None = None,
        *,
        exclude_job_id: str | None = None,
    ) -> list[dict]:
        wanted_keys = set(self._invoice_keys(headers or []))
        matches: list[dict] = []
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM scanned_documents WHERE job_id != ? ORDER BY created_at DESC",
                (exclude_job_id or "",),
            ).fetchall()
        for row in rows:
            stored_keys = set(json.loads(row["invoice_keys_json"] or "[]"))
            stored_headers = json.loads(row["headers_json"] or "[]")
            reasons: list[str] = []
            match_types: list[str] = []
            if file_sha256 and row["file_sha256"] == file_sha256:
                reasons.append("identical file")
                match_types.append("file")
            if wanted_keys & stored_keys:
                reasons.append("same supplier/invoice number")
                match_types.append("invoice")
            if reasons:
                matched_headers = [
                    header for header in stored_headers
                    if set(self._invoice_keys([header])) & wanted_keys
                ]
                reference_header = (
                    matched_headers[0] if matched_headers
                    else (stored_headers[0] if stored_headers else {})
                )
                matches.append({
                    "job_id": row["job_id"],
                    "filename": row["filename"],
                    "created_at": row["created_at"],
                    "reason": " and ".join(reasons),
                    "match_types": match_types,
                    "supplier": reference_header.get("supplier"),
                    "invoice_number": reference_header.get("invoice_number"),
                    "gstin": reference_header.get("gstin"),
                })
        return matches[:10]

    def record(
        self,
        job_id: str,
        filename: str,
        file_sha256: str,
        headers: list[dict],
    ) -> None:
        keys = self._invoice_keys(headers)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO scanned_documents
                (job_id, filename, file_sha256, invoice_keys_json, headers_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    filename,
                    file_sha256,
                    json.dumps(keys),
                    json.dumps(headers, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()
