from __future__ import annotations

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str, credentials_path: str):
        self.path = path
        self.credentials_path = credentials_path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._bootstrap_users()
        self.recover_interrupted_scans()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('maker','checker','admin')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS import_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN (
                        'processing','maker_review','submitted',
                        'approved','rejected','failed'
                    )),
                    created_by INTEGER NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL,
                    submitted_at TEXT,
                    reviewed_by INTEGER REFERENCES users(id),
                    reviewed_at TEXT,
                    review_note TEXT,
                    lock_version INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS batch_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES import_batches(id)
                        ON DELETE CASCADE,
                    file_order INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    file_sha256 TEXT NOT NULL,
                    scan_status TEXT NOT NULL CHECK(scan_status IN (
                        'queued','processing','done','failed'
                    )),
                    scan_progress INTEGER NOT NULL DEFAULT 0,
                    scan_message TEXT,
                    vendor_name TEXT,
                    vendor_gstin TEXT,
                    invoice_number TEXT,
                    invoice_date TEXT,
                    gr_number TEXT,
                    po_number TEXT,
                    ocr_grand_total REAL,
                    page_count INTEGER,
                    diagnostics_json TEXT,
                    duplicate_warning TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id,file_order)
                );

                CREATE TABLE IF NOT EXISTS batch_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id INTEGER NOT NULL REFERENCES batch_documents(id)
                        ON DELETE CASCADE,
                    line_no INTEGER NOT NULL,
                    page_no INTEGER,
                    raw_item_text TEXT,
                    canonical_name TEXT NOT NULL,
                    strength TEXT,
                    form TEXT,
                    pack TEXT,
                    batch TEXT,
                    expiry TEXT,
                    hsn TEXT,
                    paid_qty REAL,
                    free_qty REAL NOT NULL DEFAULT 0,
                    total_qty REAL,
                    unit_price REAL,
                    amount REAL,
                    scheme_flag INTEGER NOT NULL DEFAULT 0,
                    scheme_text TEXT,
                    previous_unit_price REAL,
                    price_difference REAL,
                    price_change_percent REAL,
                    price_status TEXT,
                    expiry_status TEXT,
                    days_to_expiry INTEGER,
                    match_confidence REAL,
                    catalog_name TEXT,
                    catalog_match_score REAL,
                    catalog_exact_match INTEGER NOT NULL DEFAULT 0,
                    ocr_confidence REAL,
                    catalog_status TEXT,
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    source_bbox TEXT,
                    user_edited INTEGER NOT NULL DEFAULT 0,
                    updated_by INTEGER REFERENCES users(id),
                    updated_at TEXT NOT NULL,
                    UNIQUE(document_id,line_no)
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES import_batches(id),
                    document_id INTEGER REFERENCES batch_documents(id),
                    line_id INTEGER REFERENCES batch_lines(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL,
                    detail TEXT,
                    old_values TEXT,
                    new_values TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_batches_status_time
                    ON import_batches(status,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_documents_batch
                    ON batch_documents(batch_id,file_order);
                CREATE INDEX IF NOT EXISTS idx_documents_hash
                    ON batch_documents(file_sha256);
                CREATE INDEX IF NOT EXISTS idx_lines_document
                    ON batch_lines(document_id,line_no);
                """
            )
            # Existing V3 installations receive the richer V1 review metadata
            # without rebuilding or losing their current batches.
            existing = {
                row["name"]
                for row in db.execute("PRAGMA table_info(batch_lines)").fetchall()
            }
            additions = {
                "expiry_status": "TEXT",
                "days_to_expiry": "INTEGER",
                "match_confidence": "REAL",
                "catalog_name": "TEXT",
                "catalog_match_score": "REAL",
                "catalog_exact_match": "INTEGER NOT NULL DEFAULT 0",
                "ocr_confidence": "REAL",
                "user_edited": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in additions.items():
                if name not in existing:
                    db.execute(
                        f"ALTER TABLE batch_lines ADD COLUMN {name} {declaration}"
                    )

    def _bootstrap_users(self) -> None:
        with self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                return
            configured = {
                "maker": os.environ.get("MEDSCAN_BOOTSTRAP_MAKER_PASSWORD"),
                "checker": os.environ.get("MEDSCAN_BOOTSTRAP_CHECKER_PASSWORD"),
                "admin": os.environ.get("MEDSCAN_BOOTSTRAP_ADMIN_PASSWORD"),
            }
            passwords = {
                name: value or secrets.token_urlsafe(12)
                for name, value in configured.items()
            }
            now = utc_now()
            for username, display_name, role in (
                ("maker", "Purchase Maker", "maker"),
                ("checker", "Purchase Checker", "checker"),
                ("admin", "System Administrator", "admin"),
            ):
                db.execute(
                    """
                    INSERT INTO users(
                        username,display_name,password_hash,role,active,created_at
                    ) VALUES(?,?,?,?,1,?)
                    """,
                    (
                        username,
                        display_name,
                        generate_password_hash(passwords[username]),
                        role,
                        now,
                    ),
                )
            Path(self.credentials_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.credentials_path).write_text(
                json.dumps(
                    {
                        "created_at": now,
                        "note": "Protect this one-time bootstrap file.",
                        "users": passwords,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username=? AND active=1",
                (username,),
            ).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            return None
        return dict(row)

    def user(self, user_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._dict(
                db.execute(
                    "SELECT * FROM users WHERE id=? AND active=1", (user_id,)
                ).fetchone()
            )

    def verify_user_password(self, user_id: int, password: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT password_hash FROM users WHERE id=? AND active=1",
                (user_id,),
            ).fetchone()
        return bool(row and check_password_hash(row["password_hash"], password))

    def recover_interrupted_scans(self) -> None:
        now = utc_now()
        with self.connect() as db:
            interrupted = db.execute(
                """
                SELECT DISTINCT batch_id FROM batch_documents
                WHERE scan_status IN ('queued','processing')
                """
            ).fetchall()
            if not interrupted:
                return
            db.execute(
                """
                UPDATE batch_documents
                SET scan_status='failed',scan_progress=0,
                    scan_message='Scan interrupted by service restart; upload again.',
                    updated_at=?
                WHERE scan_status IN ('queued','processing')
                """,
                (now,),
            )
            ids = [row["batch_id"] for row in interrupted]
            db.executemany(
                """
                UPDATE import_batches
                SET status='failed',lock_version=lock_version+1
                WHERE id=?
                """,
                [(identifier,) for identifier in ids],
            )

    def create_batch(
        self,
        user_id: int,
        files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        public_id = secrets.token_hex(6)
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO import_batches(
                    public_id,status,created_by,created_at
                ) VALUES(?,'processing',?,?)
                """,
                (public_id, user_id, now),
            )
            batch_id = int(cursor.lastrowid)
            document_ids = []
            for index, item in enumerate(files, 1):
                duplicate = db.execute(
                    """
                    SELECT d.filename,b.public_id
                    FROM batch_documents d
                    JOIN import_batches b ON b.id=d.batch_id
                    WHERE d.file_sha256=? AND b.status='approved'
                    ORDER BY b.reviewed_at DESC LIMIT 1
                    """,
                    (item["sha256"],),
                ).fetchone()
                warning = (
                    f"Identical to approved file {duplicate['filename']} "
                    f"(batch {duplicate['public_id']})"
                    if duplicate
                    else None
                )
                cursor = db.execute(
                    """
                    INSERT INTO batch_documents(
                        batch_id,file_order,filename,storage_path,file_sha256,
                        scan_status,scan_progress,scan_message,duplicate_warning,
                        created_at,updated_at
                    ) VALUES(?,?,?,?,?,'queued',0,'Waiting for scanner',?,?,?)
                    """,
                    (
                        batch_id,
                        index,
                        item["filename"],
                        item["storage_path"],
                        item["sha256"],
                        warning,
                        now,
                        now,
                    ),
                )
                document_ids.append(int(cursor.lastrowid))
            db.execute(
                """
                INSERT INTO audit_log(batch_id,user_id,action,detail,created_at)
                VALUES(?,?, 'batch_created', ?, ?)
                """,
                (
                    batch_id,
                    user_id,
                    f"Maker uploaded {len(files)} PDF files",
                    now,
                ),
            )
        return {
            "id": batch_id,
            "public_id": public_id,
            "document_ids": document_ids,
        }

    def document_for_scan(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._dict(
                db.execute(
                    """
                    SELECT d.*,b.public_id,b.created_by
                    FROM batch_documents d
                    JOIN import_batches b ON b.id=d.batch_id
                    WHERE d.id=?
                    """,
                    (document_id,),
                ).fetchone()
            )

    def set_document_progress(
        self,
        document_id: int,
        progress: int,
        message: str,
    ) -> None:
        with self.connect() as db:
            db.execute(
                """
                UPDATE batch_documents
                SET scan_status='processing',scan_progress=?,scan_message=?,
                    updated_at=?
                WHERE id=?
                """,
                (max(0, min(100, int(progress))), message, utc_now(), document_id),
            )

    def complete_document(
        self,
        document_id: int,
        header: dict[str, Any],
        records: list[dict[str, Any]],
        diagnostics: list[dict[str, Any]],
        page_count: int,
    ) -> None:
        now = utc_now()
        with self.connect() as db:
            document = db.execute(
                "SELECT batch_id FROM batch_documents WHERE id=?",
                (document_id,),
            ).fetchone()
            if not document:
                raise ValueError("Unknown document")
            db.execute(
                """
                UPDATE batch_documents
                SET scan_status='done',scan_progress=100,
                    scan_message='Ready for maker review',
                    vendor_name=?,vendor_gstin=?,invoice_number=?,invoice_date=?,
                    gr_number=?,po_number=?,ocr_grand_total=?,page_count=?,
                    diagnostics_json=?,updated_at=?
                WHERE id=?
                """,
                (
                    header.get("vendor_name"),
                    header.get("vendor_gstin"),
                    header.get("invoice_number"),
                    header.get("invoice_date"),
                    header.get("gr_number"),
                    header.get("po_number"),
                    header.get("grand_total"),
                    page_count,
                    json.dumps(diagnostics),
                    now,
                    document_id,
                ),
            )
            db.execute("DELETE FROM batch_lines WHERE document_id=?", (document_id,))
            for number, row in enumerate(records, 1):
                db.execute(
                    """
                    INSERT INTO batch_lines(
                        document_id,line_no,page_no,raw_item_text,canonical_name,
                        strength,form,pack,batch,expiry,hsn,paid_qty,free_qty,
                        total_qty,unit_price,amount,scheme_flag,scheme_text,
                        previous_unit_price,price_difference,price_change_percent,
                        price_status,expiry_status,days_to_expiry,match_confidence,
                        catalog_name,catalog_match_score,catalog_exact_match,
                        ocr_confidence,catalog_status,needs_review,source_bbox,
                        user_edited,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        document_id,
                        number,
                        row.get("page_no"),
                        row.get("raw_item_text"),
                        row.get("canonical_name") or row.get("raw_item_text") or "Unknown",
                        row.get("strength"),
                        row.get("form"),
                        row.get("pack"),
                        row.get("batch"),
                        row.get("expiry"),
                        row.get("hsn"),
                        row.get("paid_qty"),
                        float(row.get("free_qty") or 0),
                        row.get("qty") if row.get("qty") is not None else row.get("total_qty"),
                        row.get("unit_price"),
                        row.get("amount"),
                        int(bool(row.get("scheme_flag"))),
                        row.get("scheme_text"),
                        row.get("previous_unit_price"),
                        row.get("price_difference"),
                        row.get("price_change_percent"),
                        row.get("price_status"),
                        row.get("expiry_status"),
                        row.get("days_to_expiry"),
                        row.get("match_confidence"),
                        row.get("catalog_name"),
                        row.get("catalog_match_score"),
                        int(bool(row.get("catalog_exact_match"))),
                        row.get("ocr_confidence"),
                        row.get("catalog_status"),
                        int(bool(row.get("needs_review"))),
                        json.dumps(row.get("source_bbox"))
                        if row.get("source_bbox") is not None
                        else None,
                        int(bool(row.get("user_edited"))),
                        now,
                    ),
                )
            self._finish_batch_if_terminal(db, int(document["batch_id"]))

    def fail_document(self, document_id: int, message: str) -> None:
        now = utc_now()
        with self.connect() as db:
            document = db.execute(
                "SELECT batch_id FROM batch_documents WHERE id=?",
                (document_id,),
            ).fetchone()
            if not document:
                return
            db.execute(
                """
                UPDATE batch_documents
                SET scan_status='failed',scan_progress=0,scan_message=?,updated_at=?
                WHERE id=?
                """,
                (message[:500], now, document_id),
            )
            self._finish_batch_if_terminal(db, int(document["batch_id"]))

    @staticmethod
    def _finish_batch_if_terminal(db: sqlite3.Connection, batch_id: int) -> None:
        counts = db.execute(
            """
            SELECT COUNT(*) total,
                   SUM(scan_status IN ('done','failed')) terminal,
                   SUM(scan_status='done') done_count
            FROM batch_documents WHERE batch_id=?
            """,
            (batch_id,),
        ).fetchone()
        if counts["total"] and counts["terminal"] == counts["total"]:
            status = "maker_review" if counts["done_count"] else "failed"
            db.execute(
                """
                UPDATE import_batches
                SET status=?,lock_version=lock_version+1 WHERE id=?
                """,
                (status, batch_id),
            )

    def latest_price(self, canonical_name: str) -> float | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT l.unit_price
                FROM batch_lines l
                JOIN batch_documents d ON d.id=l.document_id
                JOIN import_batches b ON b.id=d.batch_id
                WHERE b.status='approved'
                  AND upper(trim(l.canonical_name))=upper(trim(?))
                  AND l.unit_price IS NOT NULL AND l.unit_price>0
                ORDER BY b.reviewed_at DESC,l.id DESC LIMIT 1
                """,
                (canonical_name,),
            ).fetchone()
        return float(row[0]) if row else None

    @staticmethod
    def _totals(lines: list[dict[str, Any]], ocr_total: Any) -> dict[str, Any]:
        computed = 0.0
        amount_total = 0.0
        comparable = 0
        missing = 0
        for line in lines:
            paid = line.get("paid_qty")
            rate = line.get("unit_price")
            amount_total += float(line.get("amount") or 0)
            if paid is None or rate is None:
                missing += 1
                continue
            computed += float(paid) * float(rate)
            comparable += 1
        computed = round(computed, 2)
        amount_total = round(amount_total, 2)
        fetched = float(ocr_total) if ocr_total is not None else None
        variance = round(computed - fetched, 2) if fetched is not None else None
        tolerance = max(1.0, abs(fetched or 0) * 0.001)
        matches = bool(
            fetched is not None
            and comparable > 0
            and missing == 0
            and abs(variance or 0) <= tolerance
        )
        return {
            "computed_total": computed,
            "line_amount_total": amount_total,
            "ocr_total": fetched,
            "variance": variance,
            "totals_match": matches,
            "comparable_lines": comparable,
            "missing_rate_lines": missing,
        }

    def batch(self, public_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT b.*,maker.display_name maker_name,
                       checker.display_name checker_name
                FROM import_batches b
                JOIN users maker ON maker.id=b.created_by
                LEFT JOIN users checker ON checker.id=b.reviewed_by
                WHERE b.public_id=?
                """,
                (public_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            documents = []
            for document in db.execute(
                """
                SELECT * FROM batch_documents
                WHERE batch_id=? ORDER BY file_order
                """,
                (row["id"],),
            ).fetchall():
                item = dict(document)
                lines = [
                    dict(line)
                    for line in db.execute(
                        """
                        SELECT * FROM batch_lines
                        WHERE document_id=? ORDER BY line_no,id
                        """,
                        (document["id"],),
                    ).fetchall()
                ]
                for line in lines:
                    if line.get("source_bbox"):
                        try:
                            line["source_bbox"] = json.loads(line["source_bbox"])
                        except (TypeError, ValueError, json.JSONDecodeError):
                            line["source_bbox"] = None
                item["lines"] = lines
                item["totals"] = self._totals(
                    lines, document["ocr_grand_total"]
                )
                documents.append(item)
            result["documents"] = documents
            result["progress"] = round(
                sum(int(doc["scan_progress"] or 0) for doc in documents)
                / max(1, len(documents))
            )
            result["line_count"] = sum(len(doc["lines"]) for doc in documents)
            result["review_count"] = sum(
                int(bool(line["needs_review"]))
                for doc in documents
                for line in doc["lines"]
            )
            result["ocr_total"] = round(
                sum(float(doc["ocr_grand_total"] or 0) for doc in documents), 2
            )
            result["computed_total"] = round(
                sum(float(doc["totals"]["computed_total"]) for doc in documents), 2
            )
            return result

    def update_header(
        self,
        document_id: int,
        values: dict[str, Any],
        user_id: int,
    ) -> None:
        allowed = {
            "vendor_name",
            "vendor_gstin",
            "invoice_number",
            "invoice_date",
            "gr_number",
            "po_number",
            "ocr_grand_total",
        }
        changes = {key: values[key] for key in allowed if key in values}
        if not changes:
            return
        with self.connect() as db:
            before = db.execute(
                "SELECT * FROM batch_documents WHERE id=?", (document_id,)
            ).fetchone()
            if not before:
                raise ValueError("Document not found")
            set_clause = ",".join(f"{key}=?" for key in changes)
            db.execute(
                f"UPDATE batch_documents SET {set_clause},updated_at=? WHERE id=?",
                (*changes.values(), utc_now(), document_id),
            )
            db.execute(
                """
                INSERT INTO audit_log(
                    batch_id,document_id,user_id,action,old_values,new_values,
                    created_at
                ) VALUES(?,?,?,'header_edited',?,?,?)
                """,
                (
                    before["batch_id"],
                    document_id,
                    user_id,
                    json.dumps({key: before[key] for key in changes}),
                    json.dumps(changes),
                    utc_now(),
                ),
            )

    def update_line(
        self,
        line_id: int,
        field: str,
        value: Any,
        user_id: int,
    ) -> None:
        text_fields = {
            "raw_item_text",
            "canonical_name",
            "strength",
            "form",
            "pack",
            "batch",
            "expiry",
            "hsn",
        }
        number_fields = {
            "paid_qty",
            "free_qty",
            "total_qty",
            "unit_price",
            "amount",
        }
        if field not in text_fields | number_fields:
            raise ValueError("This field cannot be edited")
        normalized = value
        if field in text_fields:
            normalized = " ".join(str(value or "").split()) or None
            if field == "canonical_name" and not normalized:
                raise ValueError("Medicine name cannot be blank")
        else:
            if value in ("", None):
                normalized = None
            else:
                normalized = float(value)
                if normalized < 0:
                    raise ValueError("Numeric values cannot be negative")
        now = utc_now()
        with self.connect() as db:
            before = db.execute(
                """
                SELECT l.*,d.batch_id FROM batch_lines l
                JOIN batch_documents d ON d.id=l.document_id
                WHERE l.id=?
                """,
                (line_id,),
            ).fetchone()
            if not before:
                raise ValueError("Medicine line not found")
            db.execute(
                f"""
                UPDATE batch_lines
                SET {field}=?,user_edited=1,updated_by=?,updated_at=? WHERE id=?
                """,
                (normalized, user_id, now, line_id),
            )
            if field in {"paid_qty", "free_qty", "total_qty"}:
                current = db.execute(
                    "SELECT paid_qty,free_qty FROM batch_lines WHERE id=?",
                    (line_id,),
                ).fetchone()
                paid = float(current["paid_qty"] or 0)
                free = float(current["free_qty"] or 0)
                db.execute(
                    """
                    UPDATE batch_lines
                    SET total_qty=?,scheme_flag=?,scheme_text=?
                    WHERE id=?
                    """,
                    (
                        paid + free,
                        int(free > 0),
                        f"Buy {paid:g}, get {free:g} free" if free > 0 else None,
                        line_id,
                    ),
                )
            db.execute(
                """
                INSERT INTO audit_log(
                    batch_id,document_id,line_id,user_id,action,
                    old_values,new_values,created_at
                ) VALUES(?,?,?,?, 'line_edited', ?, ?, ?)
                """,
                (
                    before["batch_id"],
                    before["document_id"],
                    line_id,
                    user_id,
                    json.dumps({field: before[field]}),
                    json.dumps({field: normalized}),
                    now,
                ),
            )

    def add_line(self, document_id: int, user_id: int) -> int:
        now = utc_now()
        with self.connect() as db:
            document = db.execute(
                "SELECT batch_id FROM batch_documents WHERE id=?", (document_id,)
            ).fetchone()
            if not document:
                raise ValueError("Document not found")
            line_no = (
                db.execute(
                    "SELECT COALESCE(MAX(line_no),0)+1 FROM batch_lines WHERE document_id=?",
                    (document_id,),
                ).fetchone()[0]
            )
            cursor = db.execute(
                """
                INSERT INTO batch_lines(
                    document_id,line_no,canonical_name,free_qty,scheme_flag,
                    needs_review,catalog_status,updated_by,updated_at
                ) VALUES(?,?,'New medicine',0,0,1,'unmatched',?,?)
                """,
                (document_id, line_no, user_id, now),
            )
            line_id = int(cursor.lastrowid)
            db.execute(
                """
                INSERT INTO audit_log(
                    batch_id,document_id,line_id,user_id,action,detail,created_at
                ) VALUES(?,?,?,?, 'line_added', 'Maker added a row', ?)
                """,
                (document["batch_id"], document_id, line_id, user_id, now),
            )
            return line_id

    def duplicate_line(self, line_id: int, user_id: int) -> int:
        """Copy a medicine row directly below the original."""
        now = utc_now()
        with self.connect() as db:
            line = db.execute(
                """
                SELECT l.*,d.batch_id FROM batch_lines l
                JOIN batch_documents d ON d.id=l.document_id
                WHERE l.id=?
                """,
                (line_id,),
            ).fetchone()
            if not line:
                raise ValueError("Medicine line not found")
            later = db.execute(
                """
                SELECT id,line_no FROM batch_lines
                WHERE document_id=? AND line_no>?
                ORDER BY line_no DESC,id DESC
                """,
                (line["document_id"], line["line_no"]),
            ).fetchall()
            for item in later:
                db.execute(
                    "UPDATE batch_lines SET line_no=? WHERE id=?",
                    (int(item["line_no"]) + 1, item["id"]),
                )
            columns = [
                row["name"]
                for row in db.execute("PRAGMA table_info(batch_lines)").fetchall()
                if row["name"]
                not in {"id", "line_no", "user_edited", "updated_by", "updated_at"}
            ]
            values = [line[column] for column in columns]
            cursor = db.execute(
                f"""
                INSERT INTO batch_lines(
                    line_no,{",".join(columns)},user_edited,updated_by,updated_at
                ) VALUES(?,{",".join("?" for _ in columns)},1,?,?)
                """,
                (
                    int(line["line_no"]) + 1,
                    *values,
                    user_id,
                    now,
                ),
            )
            duplicate_id = int(cursor.lastrowid)
            db.execute(
                """
                INSERT INTO audit_log(
                    batch_id,document_id,line_id,user_id,action,detail,created_at
                ) VALUES(?,?,?,?, 'line_duplicated', ?, ?)
                """,
                (
                    line["batch_id"],
                    line["document_id"],
                    duplicate_id,
                    user_id,
                    f"Duplicated medicine line {line_id}",
                    now,
                ),
            )
            return duplicate_id

    def update_line_review_metadata(
        self,
        line_id: int,
        values: dict[str, Any],
        user_id: int,
        action: str = "line_revalidated",
    ) -> None:
        allowed = {
            "canonical_name",
            "strength",
            "form",
            "pack",
            "expiry_status",
            "days_to_expiry",
            "match_confidence",
            "catalog_name",
            "catalog_match_score",
            "catalog_exact_match",
            "ocr_confidence",
            "catalog_status",
            "needs_review",
        }
        changes = {key: values[key] for key in allowed if key in values}
        if not changes:
            return
        now = utc_now()
        with self.connect() as db:
            line = db.execute(
                """
                SELECT l.*,d.batch_id FROM batch_lines l
                JOIN batch_documents d ON d.id=l.document_id
                WHERE l.id=?
                """,
                (line_id,),
            ).fetchone()
            if not line:
                raise ValueError("Medicine line not found")
            set_clause = ",".join(f"{key}=?" for key in changes)
            db.execute(
                f"""
                UPDATE batch_lines
                SET {set_clause},user_edited=1,updated_by=?,updated_at=?
                WHERE id=?
                """,
                (*changes.values(), user_id, now, line_id),
            )
            db.execute(
                """
                INSERT INTO audit_log(
                    batch_id,document_id,line_id,user_id,action,
                    old_values,new_values,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    line["batch_id"],
                    line["document_id"],
                    line_id,
                    user_id,
                    action,
                    json.dumps(
                        {key: line[key] for key in changes},
                        default=str,
                    ),
                    json.dumps(changes, default=str),
                    now,
                ),
            )

    def lines_missing_review_metadata(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                    SELECT * FROM batch_lines
                    WHERE match_confidence IS NULL
                       OR expiry_status IS NULL
                    ORDER BY id
                    """
                ).fetchall()
            ]

    def backfill_line_review_metadata(
        self,
        line_id: int,
        values: dict[str, Any],
    ) -> None:
        allowed = {
            "expiry_status",
            "days_to_expiry",
            "match_confidence",
            "catalog_name",
            "catalog_match_score",
            "catalog_exact_match",
            "ocr_confidence",
            "catalog_status",
            "needs_review",
        }
        changes = {key: values[key] for key in allowed if key in values}
        if not changes:
            return
        with self.connect() as db:
            set_clause = ",".join(f"{key}=?" for key in changes)
            db.execute(
                f"UPDATE batch_lines SET {set_clause} WHERE id=?",
                (*changes.values(), line_id),
            )

    def delete_line(self, line_id: int, user_id: int) -> None:
        now = utc_now()
        with self.connect() as db:
            line = db.execute(
                """
                SELECT l.*,d.batch_id FROM batch_lines l
                JOIN batch_documents d ON d.id=l.document_id
                WHERE l.id=?
                """,
                (line_id,),
            ).fetchone()
            if not line:
                raise ValueError("Medicine line not found")
            db.execute(
                """
                INSERT INTO audit_log(
                    batch_id,document_id,line_id,user_id,action,old_values,
                    created_at
                ) VALUES(?,?,?,?, 'line_deleted', ?, ?)
                """,
                (
                    line["batch_id"],
                    line["document_id"],
                    line_id,
                    user_id,
                    json.dumps(dict(line)),
                    now,
                ),
            )
            # audit_log historically references line_id without ON DELETE
            # SET NULL. Preserve the audit entry while allowing the editable
            # line itself to be removed.
            db.execute(
                "UPDATE audit_log SET line_id=NULL WHERE line_id=?",
                (line_id,),
            )
            db.execute("DELETE FROM batch_lines WHERE id=?", (line_id,))
            ids = db.execute(
                """
                SELECT id FROM batch_lines
                WHERE document_id=? ORDER BY line_no,id
                """,
                (line["document_id"],),
            ).fetchall()
            # Compact through a temporary range first. Updating rows directly
            # can violate UNIQUE(document_id, line_no) when deleting a middle
            # row (the next row still owns the target number).
            db.execute(
                "UPDATE batch_lines SET line_no=line_no+1000000 WHERE document_id=?",
                (line["document_id"],),
            )
            for number, row in enumerate(ids, 1):
                db.execute(
                    "UPDATE batch_lines SET line_no=? WHERE id=?",
                    (number, row["id"]),
                )

    def remove_document(self, document_id: int, user_id: int) -> str:
        """Remove one PDF from an editable batch and retain its audit trail."""
        now = utc_now()
        with self.connect() as db:
            document = db.execute(
                """
                SELECT d.*,b.status,b.created_by FROM batch_documents d
                JOIN import_batches b ON b.id=d.batch_id WHERE d.id=?
                """,
                (document_id,),
            ).fetchone()
            if not document:
                raise ValueError("PDF not found")
            if int(document["created_by"]) != int(user_id):
                raise ValueError("Only the Maker who uploaded this batch can remove a PDF")
            if document["status"] not in {"processing", "maker_review"}:
                raise ValueError("A PDF can only be removed before the batch is saved")
            count = db.execute(
                "SELECT COUNT(*) FROM batch_documents WHERE batch_id=?",
                (document["batch_id"],),
            ).fetchone()[0]
            if count <= 1:
                raise ValueError("A batch must contain at least one PDF")
            db.execute(
                """
                INSERT INTO audit_log(batch_id,document_id,user_id,action,detail,created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    document["batch_id"], document_id, user_id, "document_removed",
                    f"Maker removed {document['filename']} before submission", now,
                ),
            )
            # Audit rows are deliberately retained, so detach their optional
            # FK references before cascading the editable document/lines.
            line_ids = [
                item[0] for item in db.execute(
                    "SELECT id FROM batch_lines WHERE document_id=?", (document_id,)
                ).fetchall()
            ]
            if line_ids:
                db.executemany(
                    "UPDATE audit_log SET line_id=NULL WHERE line_id=?",
                    [(identifier,) for identifier in line_ids],
                )
            db.execute("UPDATE audit_log SET document_id=NULL WHERE document_id=?", (document_id,))
            db.execute("DELETE FROM batch_documents WHERE id=?", (document_id,))
            remaining = db.execute(
                "SELECT id FROM batch_documents WHERE batch_id=? ORDER BY file_order,id",
                (document["batch_id"],),
            ).fetchall()
            for order, row in enumerate(remaining, 1):
                db.execute("UPDATE batch_documents SET file_order=?,updated_at=? WHERE id=?", (order, now, row["id"]))
            self._finish_batch_if_terminal(db, int(document["batch_id"]))
            return str(document["storage_path"])

    def resolve_line(
        self,
        line_id: int,
        values: dict[str, Any],
        user_id: int,
        *,
        action: str,
    ) -> None:
        """Persist an explicit Maker decision for a flagged medicine line."""
        changes = dict(values)
        changes["needs_review"] = 0
        changes["user_edited"] = 1
        now = utc_now()
        with self.connect() as db:
            line = db.execute(
                """
                SELECT l.*,d.batch_id FROM batch_lines l
                JOIN batch_documents d ON d.id=l.document_id WHERE l.id=?
                """,
                (line_id,),
            ).fetchone()
            if not line:
                raise ValueError("Medicine line not found")
            allowed = {
                "canonical_name", "strength", "form", "pack", "catalog_name",
                "catalog_match_score", "catalog_exact_match", "catalog_status",
                "match_confidence", "needs_review", "user_edited",
            }
            changes = {key: value for key, value in changes.items() if key in allowed}
            if not changes:
                raise ValueError("No resolution was supplied")
            set_clause = ",".join(f"{key}=?" for key in changes)
            db.execute(
                f"UPDATE batch_lines SET {set_clause},updated_by=?,updated_at=? WHERE id=?",
                (*changes.values(), user_id, now, line_id),
            )
            db.execute(
                """
                INSERT INTO audit_log(batch_id,document_id,line_id,user_id,action,
                                      old_values,new_values,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    line["batch_id"], line["document_id"], line_id, user_id, action,
                    json.dumps({key: line[key] for key in changes}, default=str),
                    json.dumps(changes, default=str), now,
                ),
            )

    def submit_batch(self, public_id: str, user_id: int) -> None:
        now = utc_now()
        with self.connect() as db:
            batch = db.execute(
                """
                SELECT * FROM import_batches
                WHERE public_id=? AND created_by=?
                """,
                (public_id, user_id),
            ).fetchone()
            if not batch or batch["status"] != "maker_review":
                raise ValueError("This batch is not ready to save")
            documents = db.execute(
                """
                SELECT * FROM batch_documents
                WHERE batch_id=? ORDER BY file_order
                """,
                (batch["id"],),
            ).fetchall()
            if not documents or any(doc["scan_status"] != "done" for doc in documents):
                raise ValueError("Every PDF must finish scanning successfully")
            problems = []
            for document in documents:
                if not str(document["vendor_name"] or "").strip():
                    problems.append(f"{document['filename']}: vendor is required")
                if not str(document["invoice_date"] or "").strip():
                    problems.append(f"{document['filename']}: invoice date is required")
                line_count = db.execute(
                    "SELECT COUNT(*) FROM batch_lines WHERE document_id=?",
                    (document["id"],),
                ).fetchone()[0]
                if not line_count:
                    problems.append(f"{document['filename']}: no medicine rows")
                lines = db.execute(
                    "SELECT * FROM batch_lines WHERE document_id=? ORDER BY line_no,id",
                    (document["id"],),
                ).fetchall()
                for line in lines:
                    line_errors = self._required_line_errors(line)
                    for error in line_errors:
                        problems.append(
                            f"{document['filename']} · line {line['line_no']}: {error}"
                        )
            if problems:
                raise ValueError("; ".join(problems))
            changed = db.execute(
                """
                UPDATE import_batches
                SET status='submitted',submitted_at=?,
                    lock_version=lock_version+1
                WHERE id=? AND status='maker_review'
                """,
                (now, batch["id"]),
            ).rowcount
            if not changed:
                raise ValueError("This batch has already moved to the next stage")
            db.execute(
                """
                INSERT INTO audit_log(batch_id,user_id,action,detail,created_at)
                VALUES(?,?, 'submitted',
                       'Maker saved batch and submitted it to Checker', ?)
                """,
                (batch["id"], user_id, now),
            )

    @staticmethod
    def _required_line_errors(line: sqlite3.Row | dict[str, Any]) -> list[str]:
        errors: list[str] = []
        read = (lambda key: line[key]) if isinstance(line, sqlite3.Row) else line.get
        name = str(read("canonical_name") or "").strip()
        placeholder_names = {
            "",
            "NEW MEDICINE",
            "UNKNOWN",
            "N/A",
            "NA",
            "—",
            "-",
        }
        if name.upper() in placeholder_names:
            errors.append("medicine name is required")
        paid = read("paid_qty")
        rate = read("unit_price")
        if paid is None or str(paid).strip() == "":
            errors.append("paid quantity is required")
        else:
            try:
                if float(paid) < 0:
                    errors.append("paid quantity cannot be negative")
            except (TypeError, ValueError):
                errors.append("paid quantity must be numeric")
        try:
            paid_value = float(paid) if paid is not None else 0
        except (TypeError, ValueError):
            paid_value = 0
        if paid_value > 0 and (rate is None or str(rate).strip() == ""):
            errors.append("rate is required when paid quantity is greater than zero")
        elif rate is not None and str(rate).strip() != "":
            try:
                if float(rate) < 0:
                    errors.append("rate cannot be negative")
            except (TypeError, ValueError):
                errors.append("rate must be numeric")
        return errors

    def checker_queue(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """
                    SELECT b.public_id,b.submitted_at,maker.display_name maker_name,
                           COUNT(DISTINCT d.id) document_count,
                           COUNT(l.id) line_count,
                           COALESCE(SUM(l.needs_review),0) review_count,
                           (
                               SELECT COALESCE(SUM(d2.ocr_grand_total),0)
                               FROM batch_documents d2 WHERE d2.batch_id=b.id
                           ) ocr_total
                    FROM import_batches b
                    JOIN users maker ON maker.id=b.created_by
                    JOIN batch_documents d ON d.batch_id=b.id
                    LEFT JOIN batch_lines l ON l.document_id=d.id
                    WHERE b.status='submitted'
                    GROUP BY b.id ORDER BY b.submitted_at DESC
                    """
                ).fetchall()
            ]

    def review_batch(
        self,
        public_id: str,
        checker_id: int,
        action: str,
        note: str | None,
    ) -> None:
        if action not in {"approved", "rejected"}:
            raise ValueError("Unknown review action")
        now = utc_now()
        with self.connect() as db:
            row = db.execute(
                """
                UPDATE import_batches
                SET status=?,reviewed_by=?,reviewed_at=?,review_note=?,
                    lock_version=lock_version+1
                WHERE public_id=? AND status='submitted'
                """,
                (action, checker_id, now, note, public_id),
            )
            if not row.rowcount:
                raise ValueError("This batch is no longer awaiting review")
            batch_id = db.execute(
                "SELECT id FROM import_batches WHERE public_id=?", (public_id,)
            ).fetchone()[0]
            db.execute(
                """
                INSERT INTO audit_log(batch_id,user_id,action,detail,created_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    batch_id,
                    checker_id,
                    action,
                    note or f"Checker {action} the batch with password sign-off",
                    now,
                ),
            )

    def admin_summary(self) -> dict[str, Any]:
        with self.connect() as db:
            counts = {
                row["status"]: int(row["count"])
                for row in db.execute(
                    """
                    SELECT status,COUNT(*) count
                    FROM import_batches GROUP BY status
                    """
                ).fetchall()
            }
            central = db.execute(
                """
                SELECT COUNT(DISTINCT b.id) approved_batches,
                       (
                           SELECT COUNT(*) FROM batch_documents d
                           JOIN import_batches b2 ON b2.id=d.batch_id
                           WHERE b2.status='approved'
                       ) approved_documents,
                       (
                           SELECT COUNT(*) FROM batch_lines l
                           JOIN batch_documents d ON d.id=l.document_id
                           JOIN import_batches b2 ON b2.id=d.batch_id
                           WHERE b2.status='approved'
                       ) medicine_lines,
                       (
                           SELECT COALESCE(SUM(d.ocr_grand_total),0)
                           FROM batch_documents d
                           JOIN import_batches b2 ON b2.id=d.batch_id
                           WHERE b2.status='approved'
                       ) total_value
                FROM import_batches b
                WHERE b.status='approved'
                """
            ).fetchone()
            latest = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT b.public_id,b.status,b.created_at,b.submitted_at,
                           b.reviewed_at,maker.display_name maker_name,
                           checker.display_name checker_name,
                           COUNT(DISTINCT d.id) document_count,
                           COUNT(l.id) line_count,
                           GROUP_CONCAT(DISTINCT d.filename) filenames,
                           (
                               SELECT COALESCE(SUM(d2.ocr_grand_total),0)
                               FROM batch_documents d2 WHERE d2.batch_id=b.id
                           ) ocr_total
                    FROM import_batches b
                    JOIN users maker ON maker.id=b.created_by
                    LEFT JOIN users checker ON checker.id=b.reviewed_by
                    JOIN batch_documents d ON d.batch_id=b.id
                    LEFT JOIN batch_lines l ON l.document_id=d.id
                    GROUP BY b.id
                    ORDER BY COALESCE(b.reviewed_at,b.submitted_at,b.created_at) DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
        return {
            "counts": counts,
            "central": dict(central),
            "latest": latest,
        }

    def batch_history(self, user: dict[str, Any], limit: int = 80) -> list[dict[str, Any]]:
        """Role-scoped history used by Maker and Checker without leaking batches."""
        role = str(user["role"])
        where, values = "1=1", []
        if role == "maker":
            where, values = "b.created_by=?", [user["id"]]
        elif role == "checker":
            where, values = "b.reviewed_by=?", [user["id"]]
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"""
                    SELECT b.public_id,b.status,b.created_at,b.submitted_at,b.reviewed_at,
                           maker.display_name maker_name,checker.display_name checker_name,
                           COUNT(DISTINCT d.id) document_count,COUNT(l.id) line_count,
                           GROUP_CONCAT(DISTINCT d.filename) filenames,
                           COALESCE(SUM(l.needs_review),0) review_count,
                           (SELECT COALESCE(SUM(d2.ocr_grand_total),0)
                            FROM batch_documents d2 WHERE d2.batch_id=b.id) ocr_total
                    FROM import_batches b
                    JOIN users maker ON maker.id=b.created_by
                    LEFT JOIN users checker ON checker.id=b.reviewed_by
                    JOIN batch_documents d ON d.batch_id=b.id
                    LEFT JOIN batch_lines l ON l.document_id=d.id
                    WHERE {where}
                    GROUP BY b.id
                    ORDER BY COALESCE(b.reviewed_at,b.submitted_at,b.created_at) DESC
                    LIMIT ?
                    """,
                    (*values, max(1, min(250, int(limit)))),
                ).fetchall()
            ]

    def vendor_overview(
        self,
        *,
        vendor: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        include_submitted: bool = False,
    ) -> dict[str, Any]:
        """Approved central vendor data, optionally including Checker queue data."""
        statuses = ("approved", "submitted") if include_submitted else ("approved",)
        marks = ",".join("?" for _ in statuses)
        filters = [f"b.status IN ({marks})"]
        values: list[Any] = list(statuses)
        if vendor:
            filters.append("lower(d.vendor_name)=lower(?)")
            values.append(vendor)
        if date_from:
            filters.append("d.invoice_date>=?")
            values.append(date_from)
        if date_to:
            filters.append("d.invoice_date<=?")
            values.append(date_to)
        clause = " AND ".join(filters)
        with self.connect() as db:
            vendors = [
                dict(row)
                for row in db.execute(
                    f"""
                    SELECT COALESCE(NULLIF(d.vendor_name,''),'Unknown vendor') vendor_name,
                           COUNT(*) invoice_count,COALESCE(SUM(d.ocr_grand_total),0) total_value,
                           COALESCE(AVG(d.ocr_grand_total),0) average_ticket,
                           COALESCE(SUM((SELECT SUM(COALESCE(l.total_qty,l.paid_qty,0))
                                         FROM batch_lines l WHERE l.document_id=d.id)),0) medicine_volume
                    FROM batch_documents d JOIN import_batches b ON b.id=d.batch_id
                    WHERE {clause}
                    GROUP BY lower(d.vendor_name) ORDER BY total_value DESC, vendor_name
                    """,
                    values,
                ).fetchall()
            ]
            invoices = [
                dict(row)
                for row in db.execute(
                    f"""
                    SELECT b.public_id,b.status,d.id document_id,d.filename,d.vendor_name,
                           d.invoice_number,d.invoice_date,d.po_number,d.gr_number,
                           d.ocr_grand_total,
                           (SELECT COUNT(*) FROM batch_lines l WHERE l.document_id=d.id) line_count,
                           (SELECT COUNT(*) FROM batch_lines l WHERE l.document_id=d.id AND l.scheme_flag=1) scheme_count
                    FROM batch_documents d JOIN import_batches b ON b.id=d.batch_id
                    WHERE {clause}
                    ORDER BY d.invoice_date DESC,d.id DESC LIMIT 300
                    """,
                    values,
                ).fetchall()
            ]
        return {"vendors": vendors, "invoices": invoices}

    def alerts(self) -> dict[str, list[dict[str, Any]]]:
        """Central click-through alert evidence, never drafts or rejected work."""
        with self.connect() as db:
            schemes = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT b.public_id,d.id document_id,d.filename,d.vendor_name,d.invoice_number,
                           d.invoice_date,l.id line_id,l.canonical_name,l.raw_item_text,
                           l.paid_qty,l.free_qty,l.scheme_text
                    FROM batch_lines l JOIN batch_documents d ON d.id=l.document_id
                    JOIN import_batches b ON b.id=d.batch_id
                    WHERE b.status='approved' AND l.scheme_flag=1
                    ORDER BY d.invoice_date DESC,l.id DESC LIMIT 200
                    """
                ).fetchall()
            ]
            prices = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT b.public_id,d.id document_id,d.filename,d.vendor_name,d.invoice_number,
                           d.invoice_date,l.id line_id,l.canonical_name,l.unit_price,
                           l.previous_unit_price,l.price_difference,l.price_change_percent,l.price_status,
                           prevbatch.public_id previous_public_id,prevdoc.filename previous_filename,
                           prevdoc.vendor_name previous_vendor,prevdoc.invoice_number previous_invoice,
                           prevdoc.invoice_date previous_invoice_date,pl.unit_price previous_price
                    FROM batch_lines l JOIN batch_documents d ON d.id=l.document_id
                    JOIN import_batches b ON b.id=d.batch_id
                    LEFT JOIN batch_lines pl ON pl.id=(
                        SELECT l2.id FROM batch_lines l2
                        JOIN batch_documents d2 ON d2.id=l2.document_id
                        JOIN import_batches b2 ON b2.id=d2.batch_id
                        WHERE b2.status='approved' AND upper(trim(l2.canonical_name))=upper(trim(l.canonical_name))
                          AND l2.unit_price IS NOT NULL AND l2.unit_price>0
                          AND (d2.invoice_date<d.invoice_date OR (d2.invoice_date=d.invoice_date AND l2.id<l.id))
                        ORDER BY d2.invoice_date DESC,l2.id DESC LIMIT 1
                    )
                    LEFT JOIN batch_documents prevdoc ON prevdoc.id=pl.document_id
                    LEFT JOIN import_batches prevbatch ON prevbatch.id=prevdoc.batch_id
                    WHERE b.status='approved' AND l.price_status IN ('increased','decreased')
                    ORDER BY d.invoice_date DESC,l.id DESC LIMIT 200
                    """
                ).fetchall()
            ]
        return {"schemes": schemes, "prices": prices}
