from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from dateutil import parser as date_parser
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
)
from werkzeug.utils import secure_filename

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, "src"))
sys.path.insert(0, PROJECT_DIR)

from dosocr.pipeline import process_pdf  # noqa: E402
from dosocr.intelligence import expiry_intelligence  # noqa: E402
from dosocr.remote_ocr import service_health  # noqa: E402
from dosocr.standardize import DrugDictionary, standardize_item  # noqa: E402
from v3.db import Database  # noqa: E402

BASE_PATH = os.environ.get("V3_BASE_PATH", "/v3").rstrip("/")
V3_DATA_DIR = os.environ.get(
    "V3_DATA_DIR", os.path.join(PROJECT_DIR, "runtime", "v3-data")
)
MASTER_DATA_DIR = os.environ.get(
    "MASTER_DATA_DIR", os.path.join(PROJECT_DIR, "runtime", "v3-master")
)
UPLOAD_DIR = os.path.join(V3_DATA_DIR, "uploads")
JOB_DIR = os.path.join(V3_DATA_DIR, "jobs")
for directory in (V3_DATA_DIR, MASTER_DATA_DIR, UPLOAD_DIR, JOB_DIR):
    os.makedirs(directory, exist_ok=True)


def _secret_key() -> str:
    path = os.path.join(V3_DATA_DIR, "session_secret")
    if not os.path.isfile(path):
        Path(path).write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return Path(path).read_text(encoding="utf-8").strip()


app = Flask(
    __name__,
    template_folder=os.path.join(PROJECT_DIR, "v3", "templates"),
)
app.secret_key = _secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=200 * 1024 * 1024,
    SESSION_COOKIE_NAME="medscan_v3_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("V3_SECURE_COOKIE", "1") != "0",
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_PATH=f"{BASE_PATH}/",
)


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    return response

DB = Database(
    os.path.join(V3_DATA_DIR, "medscan_v3.db"),
    os.path.join(V3_DATA_DIR, "bootstrap_credentials.json"),
)
OCR_LOCK = threading.Lock()
CATALOG_LOCK = threading.Lock()
CATALOG = DrugDictionary(MASTER_DATA_DIR)


def _backfill_v1_review_metadata() -> None:
    """Populate V1 review details for batches scanned before this UI upgrade."""
    for line in DB.lines_missing_review_metadata():
        standardized = standardize_item(
            line.get("raw_item_text") or line.get("canonical_name") or "",
            CATALOG,
            pack_hint=line.get("pack"),
        )
        expiry_status, days_to_expiry = expiry_intelligence(line.get("expiry"))
        ocr_confidence = float(line.get("ocr_confidence") or 100)
        DB.backfill_line_review_metadata(
            int(line["id"]),
            {
                "expiry_status": expiry_status,
                "days_to_expiry": days_to_expiry,
                "match_confidence": standardized.match_confidence,
                "catalog_name": standardized.catalog_name,
                "catalog_match_score": standardized.catalog_match_score,
                "catalog_exact_match": int(standardized.catalog_exact_match),
                "ocr_confidence": ocr_confidence,
                "catalog_status": standardized.catalog_status,
                "needs_review": int(
                    not standardized.catalog_exact_match
                    or standardized.match_confidence < 70
                    or ocr_confidence < 80
                ),
            },
        )


_backfill_v1_review_metadata()


def current_user() -> dict[str, Any] | None:
    user_id = session.get("user_id")
    return DB.user(int(user_id)) if user_id else None


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf_token"] = token
    return str(token)


@app.before_request
def protect_mutations():
    if request.method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return None
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not supplied or not secrets.compare_digest(str(supplied), csrf_token()):
        if request.path.startswith("/api/"):
            return jsonify(error="Security token expired. Refresh and try again."), 403
        abort(403)
    return None


def login_required(function: Callable):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify(error="Please sign in."), 401
            return redirect(f"{BASE_PATH}/login")
        return function(*args, **kwargs)

    return wrapped


def role_required(*roles: str):
    def decorator(function: Callable):
        @wraps(function)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                if request.path.startswith("/api/"):
                    return jsonify(error="Please sign in."), 401
                return redirect(f"{BASE_PATH}/login")
            if user["role"] not in roles:
                if request.path.startswith("/api/"):
                    return jsonify(error="You do not have access to this action."), 403
                return redirect(f"{BASE_PATH}/")
            return function(*args, **kwargs)

        return wrapped

    return decorator


@app.context_processor
def template_context():
    return {
        "base_path": BASE_PATH,
        "current_user": current_user(),
        "csrf_token": csrf_token(),
    }


def _normal_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    try:
        return date_parser.parse(text, dayfirst=True, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _scheme_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("canonical_name") or "").upper(),
        str(row.get("batch") or "").upper(),
        str(row.get("hsn") or ""),
        str(row.get("expiry") or ""),
    )


def _enhance_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    summary_pattern = re.compile(
        r"\b(?:TOTAL ITEMS?|CGST PAY(?:A?BLE)?|SGST PAY(?:A?BLE)?|"
        r"GRAND TOTAL|NET PAYABLE|TAXABLE VALUE|TOTAL AMT)\b",
        re.IGNORECASE,
    )
    footer_pattern = re.compile(
        r"^(?:(?:ORDERED|PACKED|CHECKED|PREPARED|RECEIVED|DELIVERED)"
        r"\s+BY|DESPATCH(?:ED)?|AUTHORI[ZS]ED SIGNATORY|SIGNATURE)$",
        re.IGNORECASE,
    )
    for source in records:
        row = copy.deepcopy(source)
        raw = str(row.get("raw_item_text") or "")
        canonical = str(row.get("canonical_name") or "")
        if footer_pattern.fullmatch(raw.strip()) or footer_pattern.fullmatch(
            canonical.strip()
        ):
            continue
        if not row.get("batch") and not row.get("hsn") and summary_pattern.search(raw):
            continue
        signature = (
            re.sub(r"\s+", " ", raw).strip().upper(),
            str(row.get("batch") or "").strip().upper(),
            str(row.get("hsn") or "").strip(),
            _number(
                row.get("paid_qty")
                if row.get("paid_qty") is not None
                else row.get("qty")
            ),
            _number(row.get("free_qty")),
            _number(row.get("amount")),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduplicated.append(row)

    paid_keys = {
        _scheme_key(row)
        for row in deduplicated
        if _number(row.get("amount")) > 0
    }
    standalone_free: dict[tuple[str, str, str, str], float] = {}
    for row in deduplicated:
        if _number(row.get("amount")) != 0:
            continue
        paid = _number(
            row.get("paid_qty")
            if row.get("paid_qty") is not None
            else row.get("qty")
        )
        free = _number(row.get("free_qty"))
        quantity = free if free > 0 else paid
        key = _scheme_key(row)
        if quantity > 0 and key in paid_keys:
            standalone_free[key] = standalone_free.get(key, 0.0) + quantity

    merged: list[dict[str, Any]] = []
    consumed_free: set[tuple[str, str, str, str]] = set()
    for source in deduplicated:
        row = copy.deepcopy(source)
        paid = _number(
            row.get("paid_qty")
            if row.get("paid_qty") is not None
            else row.get("qty")
        )
        free = _number(row.get("free_qty"))
        amount = _number(row.get("amount"))
        if free > max(100.0, paid * 2):
            free = 0.0
            row["needs_review"] = 1
        key = _scheme_key(row)
        if amount == 0 and key in paid_keys and (paid > 0 or free > 0):
            continue
        if amount > 0 and key not in consumed_free:
            free += standalone_free.get(key, 0.0)
            consumed_free.add(key)
        row["paid_qty"] = paid
        row["free_qty"] = free
        row["qty"] = paid + free
        row["scheme_flag"] = int(free > 0)
        row["scheme_text"] = (
            f"Buy {paid:g}, get {free:g} free" if free > 0 else None
        )
        rate = row.get("unit_price")
        if rate is None and paid > 0 and amount > 0:
            rate = amount / paid
        row["unit_price"] = round(float(rate), 4) if rate is not None else None
        previous = DB.latest_price(str(row.get("canonical_name") or ""))
        row["previous_unit_price"] = previous
        row["price_difference"] = None
        row["price_change_percent"] = None
        row["price_status"] = "new"
        if rate is not None and previous is not None:
            difference = round(float(rate) - float(previous), 4)
            row["price_difference"] = difference
            row["price_change_percent"] = (
                round(difference / previous * 100, 2) if previous else None
            )
            row["price_status"] = (
                "increased"
                if difference > 0.005
                else "decreased"
                if difference < -0.005
                else "unchanged"
            )
        merged.append(row)
    return merged


def _header(result: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = list(result.get("invoice_headers") or [])
    source = candidates[0] if candidates else {}
    return {
        "vendor_name": source.get("supplier")
        or next(
            (
                row.get("supplier_hint")
                for row in records
                if row.get("supplier_hint")
            ),
            None,
        )
        or "Unknown vendor",
        "vendor_gstin": source.get("gstin"),
        "invoice_number": source.get("invoice_number"),
        "invoice_date": _normal_date(source.get("invoice_date")),
        "gr_number": source.get("gr_number"),
        "po_number": source.get("po_number"),
        "grand_total": source.get("grand_total")
        or round(sum(_number(row.get("amount")) for row in records), 2),
    }


def _process_batch(document_ids: list[int]) -> None:
    with OCR_LOCK:
        for document_id in document_ids:
            document = DB.document_for_scan(document_id)
            if not document:
                continue
            work_dir = os.path.join(JOB_DIR, str(document_id), "work")
            out_dir = os.path.join(JOB_DIR, str(document_id), "out")

            def progress(
                percent: int,
                message: str,
                _ceiling: int | None = None,
            ) -> None:
                DB.set_document_progress(document_id, percent, message)

            try:
                result = process_pdf(
                    document["storage_path"],
                    out_dir,
                    work_dir=work_dir,
                    dpi=300,
                    data_dir=MASTER_DATA_DIR,
                    progress=progress,
                    export_results=False,
                )
                records = _enhance_records(result.get("records") or [])
                header = _header(result, records)
                DB.complete_document(
                    document_id,
                    header,
                    records,
                    result.get("diagnostics") or [],
                    int(result.get("n_pages") or 0),
                )
            except Exception as exc:
                DB.fail_document(document_id, str(exc))


def _batch_access(public_id: str, *, edit: bool = False) -> dict[str, Any]:
    batch = DB.batch(public_id)
    if not batch:
        abort(404)
    user = current_user()
    if not user:
        abort(401)
    role = user["role"]
    allowed = False
    if role == "admin":
        allowed = not edit
    elif role == "maker":
        allowed = batch["created_by"] == user["id"]
        if edit:
            allowed = allowed and batch["status"] == "maker_review"
    elif role == "checker":
        allowed = not edit and batch["status"] in {
            "submitted",
            "approved",
            "rejected",
        }
    if not allowed:
        abort(403)
    return batch


def _document_in_batch(batch: dict[str, Any], document_id: int) -> dict[str, Any]:
    for document in batch["documents"]:
        if int(document["id"]) == int(document_id):
            return document
    abort(404)


def _line_in_document(document: dict[str, Any], line_id: int) -> dict[str, Any]:
    for line in document["lines"]:
        if int(line["id"]) == int(line_id):
            return line
    abort(404)


def _role_home(user: dict[str, Any]) -> str:
    return {
        "maker": f"{BASE_PATH}/maker",
        "checker": f"{BASE_PATH}/checker",
        "admin": f"{BASE_PATH}/admin",
    }[user["role"]]


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = str(request.form.get("username") or "").strip()
        password = str(request.form.get("password") or "")
        user = DB.authenticate(username, password)
        if not user:
            return render_template(
                "login.html",
                error="The username or password is incorrect.",
            ), 401
        session.clear()
        session["user_id"] = user["id"]
        session["csrf_token"] = secrets.token_urlsafe(24)
        return redirect(_role_home(user))
    user = current_user()
    if user:
        return redirect(_role_home(user))
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(f"{BASE_PATH}/login")


@app.get("/")
@login_required
def home():
    return redirect(_role_home(current_user()))


@app.get("/maker")
@role_required("maker")
def maker_page():
    return render_template("maker.html", page="maker")


@app.get("/history")
@login_required
def history_page():
    return render_template("history.html", page="history")


@app.get("/vendors")
@login_required
def vendor_page():
    return render_template("vendors.html", page="vendors")


@app.get("/alerts")
@login_required
def alerts_page():
    return render_template("alerts.html", page="alerts")


@app.get("/master")
@login_required
def master_page():
    return render_template("master.html", page="master")


@app.get("/maker/batch/<public_id>")
@role_required("maker")
def maker_batch_page(public_id: str):
    batch = _batch_access(public_id)
    return render_template("batch_review.html", page="maker", batch=batch)


@app.get("/checker")
@role_required("checker")
def checker_page():
    return render_template("checker_queue.html", page="checker")


@app.get("/checker/batch/<public_id>")
@role_required("checker")
def checker_batch_page(public_id: str):
    batch = _batch_access(public_id)
    return render_template("checker_batch.html", page="checker", batch=batch)


@app.get("/admin")
@role_required("admin")
def admin_page():
    return render_template("admin.html", page="admin")


@app.get("/health")
def health():
    try:
        with DB.connect() as db:
            db.execute("SELECT 1").fetchone()
        database_ok = True
    except Exception:
        database_ok = False
    scanner = service_health()
    payload = {
        "app": True,
        "database": database_ok,
        "scanner": bool(scanner.get("ok")),
    }
    return jsonify(payload), 200 if database_ok else 503


@app.get("/api/scanner-status")
@login_required
def scanner_status():
    scanner = service_health()
    return jsonify(ok=bool(scanner.get("ok")))


@app.post("/api/batches")
@role_required("maker")
def create_batch():
    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify(error="Choose at least one PDF."), 400
    if len(uploads) > 10:
        return jsonify(error="A batch can contain at most 10 PDFs."), 400
    upload_id = uuid.uuid4().hex
    directory = os.path.join(UPLOAD_DIR, upload_id)
    os.makedirs(directory, exist_ok=True)
    files = []
    total_size = 0
    for index, uploaded in enumerate(uploads, 1):
        display_name = os.path.basename(uploaded.filename or f"invoice-{index}.pdf")
        if not display_name.lower().endswith(".pdf"):
            return jsonify(error=f"{display_name}: only PDF files are supported."), 400
        data = uploaded.read()
        if not data.startswith(b"%PDF-"):
            return jsonify(error=f"{display_name}: file is not a valid PDF."), 400
        if len(data) > 50 * 1024 * 1024:
            return jsonify(error=f"{display_name}: exceeds the 50 MB limit."), 400
        total_size += len(data)
        if total_size > 150 * 1024 * 1024:
            return jsonify(error="The batch exceeds the 150 MB limit."), 400
        safe = secure_filename(display_name) or f"invoice-{index}.pdf"
        path = os.path.join(directory, f"{index:02d}-{safe}")
        Path(path).write_bytes(data)
        files.append(
            {
                "filename": display_name,
                "storage_path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    batch = DB.create_batch(current_user()["id"], files)
    threading.Thread(
        target=_process_batch,
        args=(batch["document_ids"],),
        daemon=True,
        name=f"scan-batch-{batch['public_id']}",
    ).start()
    return jsonify(
        ok=True,
        public_id=batch["public_id"],
        redirect=f"{BASE_PATH}/maker/batch/{batch['public_id']}",
    ), 202


@app.get("/api/batches/<public_id>")
@login_required
def batch_data(public_id: str):
    batch = _batch_access(public_id)
    return jsonify(batch=batch)


@app.patch("/api/batches/<public_id>/documents/<int:document_id>/header")
@role_required("maker")
def update_header(public_id: str, document_id: int):
    batch = _batch_access(public_id, edit=True)
    _document_in_batch(batch, document_id)
    payload = request.get_json(silent=True) or {}
    if "invoice_date" in payload:
        payload["invoice_date"] = _normal_date(payload.get("invoice_date"))
    if "ocr_grand_total" in payload:
        value = payload.get("ocr_grand_total")
        payload["ocr_grand_total"] = float(value) if value not in ("", None) else None
    try:
        DB.update_header(document_id, payload, current_user()["id"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, batch=DB.batch(public_id))


@app.patch(
    "/api/batches/<public_id>/documents/<int:document_id>/lines/<int:line_id>"
)
@role_required("maker")
def update_line(public_id: str, document_id: int, line_id: int):
    batch = _batch_access(public_id, edit=True)
    document = _document_in_batch(batch, document_id)
    _line_in_document(document, line_id)
    payload = request.get_json(silent=True) or {}
    field = str(payload.get("field") or "")
    try:
        DB.update_line(
            line_id,
            field,
            payload.get("value"),
            current_user()["id"],
        )
        if field == "expiry":
            status, days = expiry_intelligence(payload.get("value"))
            DB.update_line_review_metadata(
                line_id,
                {"expiry_status": status, "days_to_expiry": days},
                current_user()["id"],
                action="expiry_revalidated",
            )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, batch=DB.batch(public_id))


@app.post("/api/batches/<public_id>/documents/<int:document_id>/lines")
@role_required("maker")
def add_line(public_id: str, document_id: int):
    batch = _batch_access(public_id, edit=True)
    _document_in_batch(batch, document_id)
    line_id = DB.add_line(document_id, current_user()["id"])
    return jsonify(ok=True, line_id=line_id, batch=DB.batch(public_id))


@app.delete(
    "/api/batches/<public_id>/documents/<int:document_id>/lines/<int:line_id>"
)
@role_required("maker")
def delete_line(public_id: str, document_id: int, line_id: int):
    batch = _batch_access(public_id, edit=True)
    document = _document_in_batch(batch, document_id)
    _line_in_document(document, line_id)
    DB.delete_line(line_id, current_user()["id"])
    return jsonify(ok=True, batch=DB.batch(public_id))


@app.delete("/api/batches/<public_id>/documents/<int:document_id>")
@role_required("maker")
def remove_document(public_id: str, document_id: int):
    batch = _batch_access(public_id, edit=True)
    document = _document_in_batch(batch, document_id)
    path = DB.remove_document(document_id, current_user()["id"])
    # The file was only an unsubmitted upload.  Delete it after the DB change;
    # a scanner which already holds it safely completes as a no-op.
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
    return jsonify(ok=True, batch=DB.batch(public_id))


@app.post(
    "/api/batches/<public_id>/documents/<int:document_id>/lines/<int:line_id>/duplicate"
)
@role_required("maker")
def duplicate_line(public_id: str, document_id: int, line_id: int):
    batch = _batch_access(public_id, edit=True)
    document = _document_in_batch(batch, document_id)
    _line_in_document(document, line_id)
    duplicate_id = DB.duplicate_line(line_id, current_user()["id"])
    return jsonify(
        ok=True,
        line_id=duplicate_id,
        batch=DB.batch(public_id),
    )


@app.get("/api/catalog/suggest")
@login_required
def catalog_suggest():
    query = str(request.args.get("q") or "").strip()
    try:
        limit = max(1, min(20, int(request.args.get("limit", 10))))
    except ValueError:
        limit = 10
    return jsonify(suggestions=CATALOG.suggest(query, limit=limit))


@app.post(
    "/api/batches/<public_id>/documents/<int:document_id>/lines/<int:line_id>/resolve"
)
@role_required("maker")
def resolve_line(public_id: str, document_id: int, line_id: int):
    batch = _batch_access(public_id, edit=True)
    document = _document_in_batch(batch, document_id)
    line = _line_in_document(document, line_id)
    payload = request.get_json(silent=True) or {}
    resolution = str(payload.get("resolution") or "").strip().lower()
    if resolution == "map":
        name = " ".join(str(payload.get("name") or "").split())
        if not name:
            return jsonify(error="Choose a medicine from the master list."), 400
        standardized = standardize_item(
            " ".join(value for value in (name, line.get("strength"), line.get("form")) if value),
            CATALOG,
            pack_hint=line.get("pack"),
        )
        if not standardized.catalog_exact_match:
            return jsonify(error="Choose an exact medicine from the master list."), 400
        DB.resolve_line(
            line_id,
            {
                "canonical_name": standardized.canonical_name,
                "strength": standardized.strength,
                "form": standardized.form,
                "pack": standardized.pack or line.get("pack"),
                "catalog_name": standardized.catalog_name,
                "catalog_match_score": 100.0,
                "catalog_exact_match": 1,
                "catalog_status": "exact",
                "match_confidence": 100.0,
            },
            current_user()["id"],
            action="line_mapped_to_master",
        )
    elif resolution == "ok":
        DB.resolve_line(
            line_id,
            {
                "catalog_status": "maker_confirmed",
                "match_confidence": float(line.get("match_confidence") or 0),
            },
            current_user()["id"],
            action="line_manually_confirmed",
        )
    else:
        return jsonify(error="Choose how this medicine line is being resolved."), 400
    return jsonify(ok=True, batch=DB.batch(public_id))


@app.post(
    "/api/batches/<public_id>/documents/<int:document_id>/lines/<int:line_id>/catalog"
)
@role_required("maker")
def add_catalog_entry(
    public_id: str,
    document_id: int,
    line_id: int,
):
    batch = _batch_access(public_id, edit=True)
    document = _document_in_batch(batch, document_id)
    line = _line_in_document(document, line_id)
    payload = request.get_json(silent=True) or {}
    name = " ".join(
        str(
            payload.get("name")
            or line.get("canonical_name")
            or line.get("raw_item_text")
            or ""
        ).split()
    )
    strength = (
        " ".join(str(payload.get("strength") or line.get("strength") or "").split())
        or None
    )
    form = (
        " ".join(str(payload.get("form") or line.get("form") or "").split())
        or None
    )
    pack = (
        " ".join(str(payload.get("pack") or line.get("pack") or "").split())
        or None
    )
    try:
        with CATALOG_LOCK:
            addition = CATALOG.add_entry(name, strength, form, pack)
            standardized = standardize_item(
                " ".join(value for value in (name, strength, form) if value),
                CATALOG,
                pack_hint=pack,
            )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    needs_review = int(
        not standardized.catalog_exact_match
        or standardized.match_confidence < 70
        or float(line.get("ocr_confidence") or 0) < 80
    )
    DB.update_line_review_metadata(
        line_id,
        {
            "canonical_name": standardized.canonical_name,
            "strength": standardized.strength,
            "form": standardized.form,
            "pack": standardized.pack or pack,
            "catalog_name": standardized.catalog_name,
            "catalog_match_score": standardized.catalog_match_score,
            "catalog_exact_match": int(standardized.catalog_exact_match),
            "catalog_status": standardized.catalog_status,
            "match_confidence": standardized.match_confidence,
            "needs_review": needs_review,
        },
        current_user()["id"],
        action="catalog_entry_added",
    )
    return jsonify(
        ok=True,
        addition=addition,
        batch=DB.batch(public_id),
    )


@app.post("/api/batches/<public_id>/submit")
@role_required("maker")
def submit_batch(public_id: str):
    _batch_access(public_id, edit=True)
    try:
        DB.submit_batch(public_id, current_user()["id"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(
        ok=True,
        message="Batch saved and submitted to the Checker.",
        redirect=f"{BASE_PATH}/maker/batch/{public_id}",
    )


@app.get("/api/checker/queue")
@role_required("checker")
def checker_queue():
    return jsonify(items=DB.checker_queue())


@app.post("/api/checker/batches/<public_id>/decision")
@role_required("checker")
def checker_decision(public_id: str):
    batch = _batch_access(public_id)
    if batch["status"] != "submitted":
        return jsonify(error="This batch is no longer awaiting review."), 409
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password") or "")
    if not DB.verify_user_password(current_user()["id"], password):
        return jsonify(error="Password confirmation failed."), 403
    action = str(payload.get("action") or "")
    try:
        DB.review_batch(
            public_id,
            current_user()["id"],
            action,
            str(payload.get("note") or "").strip() or None,
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(
        ok=True,
        message=(
            "Batch approved and committed to the central system."
            if action == "approved"
            else "Batch rejected and removed from the approval queue."
        ),
        redirect=f"{BASE_PATH}/checker",
    )


@app.get("/api/admin/summary")
@role_required("admin")
def admin_summary():
    return jsonify(DB.admin_summary())


@app.get("/api/history")
@login_required
def history_data():
    return jsonify(items=DB.batch_history(current_user()))


@app.get("/api/vendors")
@login_required
def vendor_data():
    return jsonify(
        DB.vendor_overview(
            vendor=str(request.args.get("vendor") or "").strip() or None,
            date_from=_normal_date(request.args.get("from")),
            date_to=_normal_date(request.args.get("to")),
            include_submitted=current_user()["role"] == "checker",
        )
    )


@app.get("/api/alerts")
@login_required
def alert_data():
    return jsonify(DB.alerts())


@app.get("/api/master")
@login_required
def master_data():
    query = str(request.args.get("q") or "").strip()
    return jsonify(entries=CATALOG.entries(query, limit=300))


@app.get("/reports/vendors.csv")
@login_required
def vendor_report():
    data = DB.vendor_overview(
        vendor=str(request.args.get("vendor") or "").strip() or None,
        date_from=_normal_date(request.args.get("from")),
        date_to=_normal_date(request.args.get("to")),
        include_submitted=current_user()["role"] == "checker",
    )
    import csv
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Vendor", "Invoices", "Purchase value", "Average ticket", "Medicine volume"])
    for row in data["vendors"]:
        writer.writerow([row["vendor_name"], row["invoice_count"], row["total_value"], row["average_ticket"], row["medicine_volume"]])
    writer.writerow([])
    writer.writerow(["Filename", "Vendor", "Invoice no.", "Invoice date", "PO no.", "GR no.", "OCR total", "Medicine lines", "Free-good lines"])
    for row in data["invoices"]:
        writer.writerow([row["filename"], row["vendor_name"], row["invoice_number"], row["invoice_date"], row["po_number"], row["gr_number"], row["ocr_grand_total"], row["line_count"], row["scheme_count"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=medscan-vendor-report.csv"})


@app.get("/source/<public_id>/<int:document_id>/<int:page_no>")
@login_required
def source_page(public_id: str, document_id: int, page_no: int):
    batch = _batch_access(public_id)
    document = _document_in_batch(batch, document_id)
    if page_no < 1 or page_no > int(document.get("page_count") or 0):
        abort(404)
    path = os.path.join(
        JOB_DIR,
        str(document_id),
        "work",
        "clean",
        f"page-{page_no}.png",
    )
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/png", conditional=True)


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5003")),
        threaded=True,
    )
