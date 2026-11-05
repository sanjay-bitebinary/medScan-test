"""Extract invoice-level metadata from OCR layout elements."""
from __future__ import annotations

import re
from html import unescape

from .table_parse import SUPPLIER_HINTS, detect_supplier


def _plain_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _fragments(value: str) -> list[str]:
    value = re.sub(
        r"(?i)<br\s*/?>|</(?:td|th|tr|table)>",
        "\n",
        value,
    )
    value = re.sub(r"<[^>]+>", " ", value)
    return [
        re.sub(r"\s+", " ", part).strip()
        for part in unescape(value).splitlines()
        if re.sub(r"\s+", " ", part).strip()
    ]


def _first(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" .:-")
    return None


def _money(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\d[\d,]*(?:\.\d{1,2})?", value)
    if not match:
        return None
    return float(match.group().replace(",", ""))


def _invoice_candidate(value: str | None) -> str | None:
    """Reject labels, contact numbers and other non-invoice values.

    OCR often puts ``Invoice No`` and ``Phone`` in one visual line.  The
    earlier broad compact-text expression could therefore select PHONE or a
    10-digit contact number as the invoice number.
    """
    candidate = re.sub(r"\s+", " ", str(value or "")).strip(" .:-#")
    if not candidate:
        return None
    upper = candidate.upper()
    if upper in {"CREDIT", "CASH", "ORIGINAL", "RECIPIENT", "TAX", "PHONE", "MOBILE", "TEL"}:
        return None
    if re.search(r"\b(?:PHONE|MOBILE|CONTACT|TEL(?:EPHONE)?|WHATSAPP)\b", upper):
        return None
    digits = re.sub(r"\D", "", candidate)
    # Indian contact numbers are commonly emitted as a bare 10-digit value;
    # invoice IDs normally have a short sequence or a meaningful prefix.
    if len(digits) in {10, 11, 12} and not re.search(r"[A-Z]", upper) and not re.search(r"[/\-]", candidate):
        return None
    if not re.search(r"\d", candidate):
        return None
    return candidate


def _invoice_number(texts: list[str], compact: str) -> str | None:
    patterns = [
        r"\b(?:invoice|inv)\s*(?:no|number|#)?\s*[:\-]?\s*\*?([A-Z0-9][A-Z0-9/\-]{2,})",
        r"\bbill\s*(?:no|number|#)\s*[:\-]?\s*\*?([A-Z0-9][A-Z0-9/\-]{2,})",
    ]
    # Same-line extraction has precedence; it preserves visual field
    # boundaries rather than allowing a nearby phone field to bleed in.
    for value in texts:
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                candidate = _invoice_candidate(match.group(1))
                if candidate:
                    return candidate
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            candidate = _invoice_candidate(match.group(1))
            if candidate:
                return candidate
    return None


def extract_invoice_header(
    elements: list[dict],
    page_no: int,
    supplier: str | None = None,
) -> dict:
    texts = [
        fragment
        for element in elements
        if element.get("text")
        for fragment in _fragments(str(element.get("text", "")))
    ]
    text = "\n".join(value for value in texts if value)
    compact = " ".join(texts)
    compact_upper = compact.upper()
    known_hits = [
        (compact_upper.find(name), name)
        for name in SUPPLIER_HINTS
        if compact_upper.find(name) >= 0
    ]
    candidates = [
        value for value in texts[:16]
        if 4 <= len(value) <= 90
        and re.search(r"[A-Z]{3}", value.upper())
        and not re.search(
            r"\b(?:TAX|GST|INVOICE|ORIGINAL|RECIPIENT|GSTIN|DATE|STATE|CODE|"
            r"CUSTOMER|BILLING|SHIPPING|ADDRESS|SINCE|PHONE|EMAIL|BANK)\b",
            value,
            re.IGNORECASE,
        )
        and not re.search(
            r"\b(?:ROAD|LANE|THRISSUR|KERALA|\d{6}|IRN|PREPARED|COPY|"
            r"AMOUNT CHARGEABLE|JUBILEE MISSION)\b",
            value,
            re.IGNORECASE,
        )
    ]
    if known_hits:
        supplier_name = min(known_hits)[1]
    elif candidates:
        supplier_name = candidates[0]
    elif supplier and all(word in compact_upper for word in supplier.split()[:2]):
        supplier_name = supplier
    else:
        supplier_name = None

    invoice_number = _invoice_number(texts, compact)
    invoice_date = _first([
        r"\b(?:invoice|inv|bill)\s*(?:date|dt)\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})",
        r"\b(?:invoice|inv|bill)\s*(?:date|dt)\s*[:\-]?\s*(\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*[- ]\d{2,4})",
        r"\bdated\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})",
        r"\bdated\s*[:\-]?\s*(\d{1,2}[- ](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*[- ]\d{2,4})",
        r"\bdate\s*[:\-]\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})",
    ], compact)
    gstin = _first([
        r"\bGSTIN(?:/UIN)?\s*[:\-]?\s*(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9])",
    ], compact.upper())
    po_number = _first([
        r"\b(?:PO|P\.O\.|PURCHASE ORDER)\s*(?:NO|NUMBER|#)?\s*[:\-]\s*([A-Z0-9][A-Z0-9/\-]+)",
    ], compact)
    gr_number = _first([
        r"\b(?:GRN|GR|GOODS RECEIPT)\s*(?:NO|NUMBER|#)?\s*[:\-]\s*([A-Z0-9][A-Z0-9/\-]+)",
    ], compact)
    grand_total_text = _first([
        r"\b(?:GRAND TOTAL|NET PAYABLE|NET AMOUNT|INVOICE TOTAL|"
        r"INVOICE AMOUNT|BILL AMOUNT|TOTAL INVOICE VALUE)\s*[:₹]?\s*"
        r"([\d,]+(?:\.\d{1,3})?)",
    ], compact)
    return {
        "page_no": page_no,
        "supplier": supplier_name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "gstin": gstin,
        "po_number": po_number,
        "gr_number": gr_number,
        "grand_total": _money(grand_total_text),
    }


def meaningful_header(header: dict) -> bool:
    return any(
        header.get(key)
        for key in ("supplier", "invoice_number", "invoice_date", "gstin", "po_number", "gr_number")
    )
