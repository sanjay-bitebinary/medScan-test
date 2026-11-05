from __future__ import annotations

import hashlib
import importlib
import os
import tempfile
import unittest
from pathlib import Path

class UnifiedWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(cls.temp.name)
        os.environ.update(
            {
                "V3_DATA_DIR": str(root / "data"),
                "MASTER_DATA_DIR": str(root / "master"),
                "V3_SECURE_COOKIE": "0",
                "MEDSCAN_BOOTSTRAP_MAKER_PASSWORD": "maker-test-pass",
                "MEDSCAN_BOOTSTRAP_CHECKER_PASSWORD": "checker-test-pass",
                "MEDSCAN_BOOTSTRAP_ADMIN_PASSWORD": "admin-test-pass",
            }
        )
        cls.module = importlib.import_module("v3.app")
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True, SESSION_COOKIE_PATH="/")
        cls.db = cls.module.DB

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def login(self, client, username: str, password: str):
        with client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        response = client.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": "test-csrf",
            },
        )
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            token = session.get("csrf_token")
            if not token:
                token = "authenticated-csrf"
                session["csrf_token"] = token
        return {"X-CSRF-Token": token}

    def test_complete_multi_pdf_maker_checker_admin_flow(self):
        maker = self.db.authenticate("maker", "maker-test-pass")
        self.assertIsNotNone(maker)
        source = Path(self.temp.name) / "invoice.pdf"
        source.write_bytes(b"%PDF-1.4 test")
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        batch = self.db.create_batch(
            maker["id"],
            [
                {
                    "filename": "vendor-a.pdf",
                    "storage_path": str(source),
                    "sha256": sha,
                },
                {
                    "filename": "vendor-b.pdf",
                    "storage_path": str(source),
                    "sha256": sha,
                },
            ],
        )
        rows = [
            {
                "page_no": 1,
                "raw_item_text": "MEDICINE 500MG 10S",
                "canonical_name": "MEDICINE",
                "strength": "500MG",
                "pack": "10S",
                "paid_qty": 10,
                "free_qty": 1,
                "qty": 11,
                "unit_price": 50,
                "amount": 500,
                "scheme_flag": True,
                "catalog_status": "exact",
            }
        ]
        for document_id, vendor, invoice in zip(
            batch["document_ids"], ("Vendor A", "Vendor B"), ("A-101", "B-202")
        ):
            self.db.complete_document(
                document_id,
                {
                    "vendor_name": vendor,
                    "invoice_number": invoice,
                    "invoice_date": "2026-08-11",
                    "grand_total": 500,
                },
                rows,
                [],
                1,
            )

        public_id = batch["public_id"]
        captured = self.db.batch(public_id)
        self.assertEqual(captured["status"], "maker_review")
        self.assertEqual(len(captured["documents"]), 2)
        self.assertEqual(captured["computed_total"], 1000)
        self.assertEqual(captured["ocr_total"], 1000)
        self.assertEqual(
            captured["documents"][0]["totals"]["computed_total"],
            500,
            "Free quantity must not increase paid quantity × rate",
        )

        with self.app.test_client() as maker_client:
            headers = self.login(maker_client, "maker", "maker-test-pass")
            response = maker_client.post(
                f"/api/batches/{public_id}/submit", headers=headers
            )
            self.assertEqual(response.status_code, 200, response.get_json())

        with self.app.test_client() as checker_client:
            headers = self.login(checker_client, "checker", "checker-test-pass")
            queue = checker_client.get("/api/checker/queue").get_json()["items"]
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["document_count"], 2)
            self.assertEqual(queue[0]["ocr_total"], 1000)
            response = checker_client.post(
                f"/api/checker/batches/{public_id}/decision",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "action": "approved",
                    "password": "checker-test-pass",
                    "note": "Verified against both source invoices.",
                },
            )
            self.assertEqual(response.status_code, 200, response.get_json())

        with self.app.test_client() as admin_client:
            self.login(admin_client, "admin", "admin-test-pass")
            summary = admin_client.get("/api/admin/summary").get_json()
            self.assertEqual(summary["central"]["approved_batches"], 1)
            self.assertEqual(summary["central"]["approved_documents"], 2)
            self.assertEqual(summary["central"]["medicine_lines"], 2)
            self.assertEqual(summary["central"]["total_value"], 1000)
            self.assertEqual(summary["latest"][0]["status"], "approved")
            self.assertIn("vendor-a.pdf", summary["latest"][0]["filenames"])

    def test_role_landing_and_access_boundaries(self):
        with self.app.test_client() as client:
            self.login(client, "maker", "maker-test-pass")
            self.assertEqual(client.get("/").location, "/v3/maker")
            self.assertEqual(client.get("/api/checker/queue").status_code, 403)
        with self.app.test_client() as client:
            self.login(client, "checker", "checker-test-pass")
            self.assertEqual(client.get("/").location, "/v3/checker")
            self.assertEqual(client.get("/maker").status_code, 302)

    def test_dosprinter_paid_and_free_rows_keep_rate_and_scheme(self):
        from dosocr.native_text import _parse_fixed_width

        text = """
ITEM NAME & PACKING HSN CODE QTY RATE NON FREE DISC NET AMOUNT
102.10 GYR01AAB 02-28 EXMO EXMOCEF 200 TAB 30042019 20 77.79 1555.80 4.00 1568.25
102.10 GYR01AAB 02-28 EXMO EXMOCEF 200 TAB 30042019 8 * FREE *
--------------------------------------------------------------------------------
"""
        rows = _parse_fixed_width(text, 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].paid_qty, 20)
        self.assertEqual(rows[0].free_qty, 0)
        self.assertEqual(rows[0].unit_price, 77.79)
        self.assertEqual(rows[0].amount, 1568.25)
        self.assertEqual(rows[1].paid_qty, 0)
        self.assertEqual(rows[1].free_qty, 8)
        self.assertEqual(rows[1].amount, 0)
        with self.app.test_client() as client:
            self.login(client, "admin", "admin-test-pass")
            self.assertEqual(client.get("/").location, "/v3/admin")
            self.assertEqual(client.get("/maker").status_code, 302)

    def test_invoice_number_never_accepts_contact_number(self):
        from dosocr.invoice_meta import extract_invoice_header

        header = extract_invoice_header(
            [{"text": "INVOICE NO: 9876543210  PHONE: 9876543210"}],
            1,
        )
        self.assertIsNone(header["invoice_number"])
        header = extract_invoice_header(
            [{"text": "Invoice No: AA-26/1042\nPhone: 9876543210"}],
            1,
        )
        self.assertEqual(header["invoice_number"], "AA-26/1042")

    def test_maker_can_resolve_review_and_remove_one_pdf(self):
        maker = self.db.authenticate("maker", "maker-test-pass")
        source = Path(self.temp.name) / "resolvable.pdf"
        source.write_bytes(b"%PDF-1.4 resolvable")
        batch = self.db.create_batch(
            maker["id"],
            [
                {"filename": "keep.pdf", "storage_path": str(source), "sha256": "keep"},
                {"filename": "remove.pdf", "storage_path": str(source), "sha256": "remove"},
            ],
        )
        row = {
            "page_no": 1, "raw_item_text": "UNCERTAIN", "canonical_name": "UNCERTAIN",
            "paid_qty": 1, "unit_price": 10, "amount": 10, "catalog_status": "fuzzy",
            "catalog_exact_match": 0, "match_confidence": 70, "needs_review": 1,
        }
        for identifier in batch["document_ids"]:
            self.db.complete_document(identifier, {"vendor_name": "V", "invoice_date": "2026-08-13", "grand_total": 10}, [row], [], 1)
        public_id = batch["public_id"]
        first = self.db.batch(public_id)["documents"][0]["lines"][0]
        self.db.resolve_line(first["id"], {"catalog_status": "maker_confirmed"}, maker["id"], action="line_manually_confirmed")
        self.assertEqual(self.db.batch(public_id)["review_count"], 1)
        self.db.remove_document(batch["document_ids"][1], maker["id"])
        review = self.db.batch(public_id)
        self.assertEqual(len(review["documents"]), 1)
        self.assertEqual(review["review_count"], 0)

    def test_v1_review_features_are_available_per_pdf(self):
        maker = self.db.authenticate("maker", "maker-test-pass")
        source = Path(self.temp.name) / "review-features.pdf"
        source.write_bytes(b"%PDF-1.4 review")
        batch = self.db.create_batch(
            maker["id"],
            [
                {
                    "filename": "review-features.pdf",
                    "storage_path": str(source),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            ],
        )
        document_id = batch["document_ids"][0]
        self.db.complete_document(
            document_id,
            {
                "vendor_name": "Review Vendor",
                "invoice_number": "R-101",
                "invoice_date": "2026-08-12",
                "grand_total": 50,
            },
            [
                {
                    "page_no": 1,
                    "raw_item_text": "REVIEWMED 5MG",
                    "canonical_name": "REVIEWMED",
                    "strength": "5MG",
                    "paid_qty": 1,
                    "unit_price": 50,
                    "amount": 50,
                    "catalog_status": "unmatched",
                    "catalog_exact_match": 0,
                    "match_confidence": 63.2,
                    "catalog_match_score": 63.2,
                    "needs_review": 1,
                    "expiry": "12/27",
                    "expiry_status": "valid",
                    "days_to_expiry": 500,
                    "source_bbox": [10, 20, 100, 40],
                }
            ],
            [],
            1,
        )
        public_id = batch["public_id"]
        with self.app.test_client() as client:
            headers = self.login(client, "maker", "maker-test-pass")
            page = client.get(f"/maker/batch/{public_id}")
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            for feature in (
                "medicine-review-table",
                "Exact matches",
                "Possible scan errors",
                "Expiry attention",
                "sourcePanel",
                "catalogPanel",
                "data-duplicate-line",
                "ondblclick",
            ):
                self.assertIn(feature, html)
            before = self.db.batch(public_id)
            line_id = before["documents"][0]["lines"][0]["id"]
            duplicated = client.post(
                f"/api/batches/{public_id}/documents/{document_id}/lines/{line_id}/duplicate",
                headers=headers,
            )
            self.assertEqual(duplicated.status_code, 200, duplicated.get_json())
            self.assertEqual(
                len(duplicated.get_json()["batch"]["documents"][0]["lines"]),
                2,
            )


if __name__ == "__main__":
    unittest.main()
