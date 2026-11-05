"""PDF -> local preprocessing -> remote dots.ocr -> catalog validation -> export."""
from __future__ import annotations

import json
import hashlib
import os
import re
from collections.abc import Callable

from .export import export_all
from .intelligence import expiry_intelligence
from .invoice_meta import extract_invoice_header, meaningful_header
from .native_text import extract_native_pages
from .preprocess import alternate_orientation, preprocess_pdf
from .remote_ocr import ocr_page
from .standardize import DrugDictionary, standardize_item
from .table_parse import detect_supplier, parse_layout

REVIEW_MATCH_CONFIDENCE = 70.0
REVIEW_OCR_CONFIDENCE = 80.0
ProgressCallback = Callable[[int, str, int | None], None]
OCR_CACHE_VERSION = b"dosocr-dots-layout-v1\0"


def _cache_path(image_path: str, data_dir: str | None) -> str | None:
    if os.environ.get("DOTS_OCR_CACHE", "1").strip() == "0":
        return None
    cache_root = os.environ.get("DOTS_OCR_CACHE_DIR", "").strip()
    if not cache_root:
        project_root = os.path.dirname(data_dir) if data_dir else os.getcwd()
        cache_root = os.path.join(project_root, ".cache", "dots-ocr")
    digest = hashlib.sha256()
    digest.update(OCR_CACHE_VERSION)
    with open(image_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    os.makedirs(cache_root, exist_ok=True)
    return os.path.join(cache_root, f"{digest.hexdigest()}.json")


def _load_layout_result(path: str | None) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            result = json.load(handle)
        if isinstance(result.get("elements"), list):
            return result
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _save_layout_result(path: str | None, result: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)


def _record(parsed, supplier: str | None, dictionary: DrugDictionary) -> dict:
    # The pack column is separate invoice data. Appending it to the product
    # description can turn a pack such as 10ML into a false medicine strength.
    pack_hint = (
        parsed.pack
        if str(parsed.pack or "").strip().upper()
        not in {"", "0", ".", "-", "N/A", "NA"}
        else None
    )
    standardized = standardize_item(
        parsed.product_text,
        dictionary,
        pack_hint=pack_hint,
    )
    expiry_status, days_to_expiry = expiry_intelligence(parsed.expiry)
    needs_review = int(
        not standardized.catalog_exact_match
        or standardized.match_confidence < REVIEW_MATCH_CONFIDENCE
        or parsed.ocr_confidence < REVIEW_OCR_CONFIDENCE
    )
    return {
        "page_no": parsed.page_no,
        "supplier_hint": supplier,
        "raw_item_text": parsed.product_text,
        "canonical_name": standardized.canonical_name,
        "generic_name": standardized.generic_name,
        "brand_name": standardized.brand_name,
        "strength": standardized.strength,
        "form": standardized.form,
        "pack": standardized.pack or pack_hint,
        "batch": parsed.batch,
        "hsn": parsed.hsn,
        "qty": parsed.qty,
        "paid_qty": parsed.paid_qty if parsed.paid_qty is not None else parsed.qty,
        "free_qty": parsed.free_qty,
        "unit_price": parsed.unit_price,
        "amount": parsed.amount,
        "expiry": parsed.expiry,
        "expiry_status": expiry_status,
        "days_to_expiry": days_to_expiry,
        "match_confidence": standardized.match_confidence,
        "catalog_name": standardized.catalog_name,
        "catalog_match_score": standardized.catalog_match_score,
        "catalog_exact_match": int(standardized.catalog_exact_match),
        "catalog_status": standardized.catalog_status,
        "ocr_confidence": parsed.ocr_confidence,
        "needs_review": needs_review,
        "source_bbox": parsed.source_bbox,
    }


def process_pdf(
    pdf_path: str,
    out_dir: str,
    work_dir: str | None = None,
    dpi: int = 300,
    data_dir: str | None = None,
    progress: ProgressCallback | None = None,
    export_results: bool = True,
) -> dict:
    """Process a PDF while keeping all non-model work on the local machine."""
    callback = progress or (lambda _percent, _message, _ceiling=None: None)
    work_dir = work_dir or os.path.join(out_dir, "_work")
    os.makedirs(work_dir, exist_ok=True)

    callback(2, "Inspecting the uploaded PDF…", 3)
    pages = preprocess_pdf(
        pdf_path,
        work_dir=work_dir,
        dpi=dpi,
        progress=lambda value, ceiling, message: callback(
            value, message, ceiling
        ),
    )
    if not pages:
        raise ValueError("The PDF did not contain any rasterizable pages.")

    # A selectable PDF can often provide exact table cells without any model
    # inference.  Keep rasterized pages for source-linked review, but prefer
    # the native parser whenever it found at least one medicine row.
    native_pages = extract_native_pages(pdf_path)

    callback(24, "Loading the medicine reference list…", 28)
    dictionary = DrugDictionary(data_dir) if data_dir else DrugDictionary()
    callback(28, "Medicine reference list loaded", 29)
    records: list[dict] = []
    diagnostics: list[dict] = []
    invoice_headers: list[dict] = []
    layout_dir = os.path.join(work_dir, "dots-layout")
    os.makedirs(layout_dir, exist_ok=True)

    for index, page in enumerate(pages, start=1):
        page_start = 29 + int(57 * (index - 1) / len(pages))
        page_end = 29 + int(57 * index / len(pages))
        ocr_start = page_start + max(1, int((page_end - page_start) * 0.15))
        parse_start = page_start + max(2, int((page_end - page_start) * 0.82))
        callback(
            page_start,
            f"Page {index}/{len(pages)}: preparing the scan…",
            ocr_start,
        )
        layout_path = os.path.join(layout_dir, f"page-{page.page_no}.json")
        cache_path = _cache_path(page.path, data_dir)
        native = native_pages.get(page.page_no)
        result = None
        parsed_lines = []
        if native is not None:
            result = {
                "elements": native.elements,
                "model": "native-pdf-text",
                "usage": {},
            }
            parsed_lines = native.lines
            callback(
                ocr_start,
                f"Page {index}/{len(pages)}: reading selectable PDF text directly…",
                parse_start,
            )
        else:
            result = _load_layout_result(layout_path) or _load_layout_result(cache_path)
        if result is None:
            callback(
                ocr_start,
                f"Page {index}/{len(pages)}: reading the medicine table…",
                parse_start,
            )
            result = ocr_page(page.path)
        else:
            callback(
                ocr_start,
                f"Page {index}/{len(pages)}: loading the saved scan result…",
                parse_start,
            )
        _save_layout_result(layout_path, result)
        if not os.path.isfile(cache_path or ""):
            _save_layout_result(cache_path, result)

        callback(
            parse_start,
            f"Page {index}/{len(pages)}: mapping invoice columns…",
            page_end,
        )
        if not parsed_lines:
            parsed_lines = parse_layout(result["elements"], page.page_no)
        # Some image-only scans are physically upside down after the
        # projection-based orientation heuristic. Give dots.ocr one alternate
        # 180-degree view only when the first layout produced no rows. This
        # keeps the normal path fast while recovering rotated landscape bills.
        if not parsed_lines and str(result.get("model", "")).lower() == "dots-ocr":
            alternate_path = os.path.join(
                work_dir, "alternate", f"page-{page.page_no}.png"
            )
            try:
                alternate_orientation(page.path, alternate_path)
                alternate_result = ocr_page(alternate_path)
                alternate_lines = parse_layout(
                    alternate_result.get("elements", []), page.page_no
                )
                if alternate_lines:
                    result = alternate_result
                    parsed_lines = alternate_lines
                    page.rotation_applied = (page.rotation_applied + 180) % 360
            except Exception:
                # Keep the original parse/error path if the alternate attempt
                # is unavailable or the model returns no usable layout.
                pass
        supplier = detect_supplier(result["elements"])
        header = extract_invoice_header(
            result["elements"], page.page_no, supplier=supplier
        )
        header["line_total"] = round(
            sum(float(line.amount or 0) for line in parsed_lines),
            2,
        ) or None
        if meaningful_header(header):
            invoice_headers.append(header)
        records.extend(
            _record(line, supplier, dictionary) for line in parsed_lines
        )
        diagnostics.append({
            "page_no": page.page_no,
            "rotation_applied": page.rotation_applied,
            "line_items": len(parsed_lines),
            "model": result.get("model"),
            "usage": result.get("usage", {}),
        })
        callback(
            page_end,
            f"Page {index}/{len(pages)}: mapped {len(parsed_lines)} medicine rows",
            min(87, page_end + 1),
        )

    if not records:
        raise ValueError(
            "Scanning completed, but no medicine table rows were found. "
            "Check that the PDF contains a Product/Item column and a Qty or HSN column."
        )

    callback(87, "Finalizing medicine validation…", 92)
    basename = re.sub(
        r"[^\w\-]+",
        "_",
        os.path.splitext(os.path.basename(pdf_path))[0],
    )
    outputs = {}
    if export_results:
        callback(92, "Creating exports…", 99)
        outputs = export_all(
            records,
            out_dir,
            basename=basename,
            source_file=os.path.basename(pdf_path),
        )
    else:
        callback(92, "Saving scan results for review…", 99)
    callback(99, "Finalizing results…", 100)
    return {
        "records": records,
        "outputs": outputs,
        "n_pages": len(pages),
        "diagnostics": diagnostics,
        "invoice_headers": invoice_headers,
    }
