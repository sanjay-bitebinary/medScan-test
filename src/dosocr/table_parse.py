"""Convert dots.ocr layout HTML into trustworthy invoice line items."""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any


SUPPLIER_HINTS = [
    "TRICHUR HOSPITAL SUPPLIES",
    "SRI ENTERPRISES",
    "ALLIED AGENCIES",
    "MEENAKSHY MEDICAL",
    "LEO PHARMA",
    "SASTHA ENTERPRISES",
    "LORD PHARMA",
    "KALPANA PHARMACEUTICALS",
    "KALPANA ENTERPRISES",
    "PANIKULAM AGENCIES",
    "UNITED MEDICAL AGENCIES",
    "SMART LIFE CARE",
    "SUBHA MEDICAL AGENCIES",
    "RAGAM DISTRIBUTORS",
    "PRESAGE PHARMA",
    "BINU AGENCIES",
    "KALYAN PHARMA",
    "MALABAR LAB SOLUTIONS",
    "INTERNATIONAL AGENCIES",
    "AP PHARMA",
    "KBS ENTERPRISES",
    "LAKSHMI SURGICALS",
    "MEDI ASSIST",
    "ASWINI AGENCIES",
    "UNIVERSAL AGENCIES",
    "DURGA PHARMACEUTICALS & SURGICALS",
    "E J PHARMA",
    "J K PHARMA",
    "MEENAKSHY EXPORT AGENCIES",
    "ORIOL HEALTHCARE",
    "MAHAVEER PHARMA",
    "OMEGA ENTERPRISES",
    "NAVANI MEDITEX PRIVATE LIMITED",
    "MEDI BRIGHT CARE",
    "PLASMA MEDICAL SPECIALITIES",
    "POOJA PHARMA",
    "ROYAL ASSOCIATES",
    "ORTHO PLUS SURGICALS",
    "MANU PHARMA",
    "MIDAS MEDITECH PRIVATE LIMITED",
    "TRIGLOBE MEDICARE",
]


@dataclass
class ParsedLine:
    page_no: int
    product_text: str
    hsn: str | None
    qty: float | int | None
    amount: float | None
    expiry: str | None
    pack: str | None
    batch: str | None
    ocr_confidence: float
    mapping_note: str
    source_bbox: list[int | float] | None = None
    free_qty: float | int | None = None
    paid_qty: float | int | None = None
    unit_price: float | None = None


class _TableCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[tuple[list[str], list[bool]]]] = []
        self._table: list[tuple[list[str], list[bool]]] | None = None
        self._row: list[str] | None = None
        self._header_flags: list[bool] = []
        self._cell: list[str] | None = None
        self._cell_is_header = False
        self._cell_colspan = 1
        self._depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif self._depth == 1 and tag == "tr":
            self._row = []
            self._header_flags = []
        elif self._depth == 1 and tag in {"td", "th"} and self._row is not None:
            self._cell = []
            self._cell_is_header = tag == "th"
            attributes = dict(attrs)
            try:
                self._cell_colspan = max(1, int(attributes.get("colspan", "1")))
            except (TypeError, ValueError):
                self._cell_colspan = 1
        elif self._cell is not None and tag == "br":
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._depth == 1 and tag in {"td", "th"} and self._cell is not None:
            value = re.sub(r"\s+", " ", unescape("".join(self._cell))).strip()
            if self._row is not None:
                self._row.append(value)
                self._header_flags.append(self._cell_is_header)
                for _ in range(self._cell_colspan - 1):
                    self._row.append("")
                    self._header_flags.append(self._cell_is_header)
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            if any(cell for cell in self._row) and self._table is not None:
                self._table.append((self._row, self._header_flags))
            self._row = None
        elif tag == "table" and self._depth:
            if self._depth == 1 and self._table:
                self.tables.append(self._table)
                self._table = None
            self._depth -= 1


def _tables_from_html(html: str) -> list[list[tuple[list[str], list[bool]]]]:
    parser = _TableCollector()
    parser.feed(html)
    parser.close()
    return parser.tables


def _norm_header(value: str | None) -> str:
    return re.sub(
        r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    ).strip()


def _find_header(headers: list[str], aliases: set[str]) -> int | None:
    normalized = [_norm_header(header) for header in headers]
    for i, header in enumerate(normalized):
        if header in aliases:
            return i
    for i, header in enumerate(normalized):
        if any(alias in header for alias in aliases if len(alias) >= 4):
            return i
    return None


def _number(value: str) -> float | int | None:
    cleaned = value.strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    number = float(match.group())
    return int(number) if number.is_integer() else number


def _money(value: str) -> float | None:
    number = _number(value)
    return float(number) if number is not None else None


def _hsn(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    return digits if 4 <= len(digits) <= 10 else None


def _expiry(value: str) -> str | None:
    spaced = re.sub(r"\s+", " ", value).strip()
    separated = re.fullmatch(r"(0?[1-9]|1[0-2])\s+(\d{2}|\d{4})", spaced)
    if separated:
        return f"{separated.group(1)}/{separated.group(2)}"
    cleaned = re.sub(r"\s+", "", value)
    match = re.search(r"(?:0?[1-9]|1[0-2])[/\-.](?:\d{2}|\d{4})", cleaned)
    return match.group() if match else (cleaned or None)


PRODUCT_HEADERS = {
    "product", "product name", "description", "product description",
    "item", "item name", "item description", "particulars", "medicine",
    "medicine name", "drug", "drug name", "name of item", "itemname",
    "productname", "commodity", "commodity item", "commodity name",
    "description item", "item particulars", "material", "material name",
}
HSN_HEADERS = {
    "hsn", "hsn code", "hsncode", "hsn sac", "hsn sac code", "hsn asc",
}
QTY_HEADERS = {
    "qty", "quantity", "bill qty", "billed qty", "sale qty",
    "sales qty", "order qty", "ordered qty",
}
FREE_QTY_HEADERS = {
    "free", "free qty", "free quantity", "scheme qty", "offer qty",
}
RATE_HEADERS = {
    "rate", "unit price", "unit rate", "ptr", "purchase rate", "price",
}
EXPIRY_HEADERS = {
    "exp", "expiry", "exp date", "exp dt", "expiry date", "exp dt mfr",
}
PACK_HEADERS = {"pack", "packing", "pack size", "unit pack"}
BATCH_HEADERS = {
    "batch", "batch no", "batch number", "batchno", "lot", "lot no",
    "lot number",
}
AMOUNT_HEADERS = {
    "amount", "amt", "gross amount", "gross amt", "line amount",
    "total amount", "net amount", "value", "net value", "total value",
    "taxable value", "taxable amount",
}


def _header_row_index(
    table: list[tuple[list[str], list[bool]]]
) -> int | None:
    # Native PDF table extraction often places several invoice header rows
    # (supplier, invoice metadata, description) before the actual column row.
    # Score candidate rows so a lone "Description" label cannot win over the
    # later row containing Product + HSN + Qty + Batch/Expiry columns.
    best: tuple[int, int] | None = None
    for i, (cells, flags) in enumerate(table[:12]):
        normalized = {_norm_header(cell) for cell in cells}
        has_product = any(
            header in PRODUCT_HEADERS
            or any(alias in header for alias in PRODUCT_HEADERS if len(alias) >= 5)
            for header in normalized
        )
        if not has_product or not (any(flags) or len(cells) >= 3):
            continue
        score = 1
        for aliases in (
            HSN_HEADERS, QTY_HEADERS, EXPIRY_HEADERS, BATCH_HEADERS,
            PACK_HEADERS, AMOUNT_HEADERS,
        ):
            if _find_header(cells, aliases) is not None:
                score += 1
        candidate = (score, i)
        if best is None or candidate[0] > best[0] or (
            candidate[0] == best[0] and candidate[1] > best[1]
        ):
            best = candidate
    return best[1] if best else None


def _amount_index(headers: list[str], row_length: int) -> tuple[int | None, str]:
    if row_length > len(headers):
        return row_length - 1, "unlabelled final total column"
    if row_length < len(headers):
        return None, "column count mismatch; amount withheld"

    normalized = [_norm_header(header) for header in headers]
    # Prefer the final/net line value when a table exposes both taxable and
    # net columns. Fall back to taxable value only when no total is available.
    for target in (
        "net value", "total value", "total amount", "net amount",
        "gross amount", "gross amt", "line amount", "amount", "amt",
        "taxable value", "taxable amount", "value",
    ):
        for i, header in enumerate(normalized):
            if header == target:
                return i, f"mapped from {headers[i]}"
    return None, "amount column unavailable"


def _expand_multiline_row(
    cells: list[str | None], product_index: int | None = None
) -> list[list[str]]:
    """Expand PDF table rows whose columns contain one newline per item.

    Some selectable-text invoice PDFs are extracted as a single table row with
    six product names in one cell, six quantities in another, and so on.  Keep
    the column alignment by splitting each cell into lines and emitting one
    logical row per line.  Single-valued cells are repeated for each emitted
    row, which is useful for shared metadata while leaving blank cells blank.
    """
    parts: list[list[str]] = []
    max_parts = 1
    for value in cells:
        values = [part.strip() for part in re.split(r"\r?\n+", str(value or ""))]
        values = values or [""]
        parts.append(values)
        max_parts = max(max_parts, len(values))
    # Manufacturer annotations and tax-summary labels are sometimes embedded
    # as extra lines in the product cell.  Use the other data columns to infer
    # the real item count, then remove those non-item lines before aligning.
    target_parts = max_parts
    if product_index is not None and 0 <= product_index < len(parts):
        other_counts = [
            len(values) for index, values in enumerate(parts)
            if index != product_index
        ]
        if other_counts:
            target_parts = max(other_counts)
        product_parts = parts[product_index]
        if target_parts <= 1 and len(product_parts) > 1:
            # A single item may contain embedded Batch/Exp lines. Keep them
            # together so metadata can be extracted from the item text later.
            parts[product_index] = [" ".join(product_parts)]
        elif len(product_parts) > target_parts:
            filtered = [
                part for part in product_parts
                if not re.match(
                    r"^\(?\s*(?:mfgr|manufacturer|cgst|sgst|round\s*off|total)\b",
                    part,
                    flags=re.IGNORECASE,
                )
            ]
            if filtered:
                parts[product_index] = filtered
            target_parts = max(target_parts, len(parts[product_index]))
        max_parts = target_parts
    if max_parts == 1:
        return [[str(value or "") for value in cells]]
    expanded: list[list[str]] = []
    for index in range(max_parts):
        row: list[str] = []
        for values in parts:
            if len(values) == 1:
                row.append(values[0])
            else:
                row.append(values[index] if index < len(values) else "")
        expanded.append(row)
    return expanded


def _embedded_product_metadata(value: str) -> tuple[str, str | None, str | None]:
    """Separate Batch/Exp annotations that appear inside a product cell."""
    text = re.sub(r"\s+", " ", value or "").strip()
    batch_match = re.search(
        r"\b(?:batch|lot)\s*(?:no\.?|number)?\s*:\s*([A-Za-z0-9][A-Za-z0-9./-]*)",
        text,
        flags=re.IGNORECASE,
    )
    expiry_match = re.search(
        r"\bexp(?:iry)?\.?\s*(?:dt|date)?\s*:\s*([A-Za-z0-9][A-Za-z0-9./-]*)",
        text,
        flags=re.IGNORECASE,
    )
    batch = batch_match.group(1) if batch_match else None
    expiry = _expiry(expiry_match.group(1)) if expiry_match else None
    if batch_match:
        text = text.replace(batch_match.group(0), " ")
    if expiry_match:
        text = text.replace(expiry_match.group(0), " ")
    # OCR may put a bare expiry prefix in the product cell when the Exp/Mfr
    # column shifts left (for example, "03/28 AUBI FEPANIL...").
    text = re.sub(
        r"^(?:0?[1-9]|1[0-2])[/\-.]\d{2,4}\s+", "", text
    )
    return re.sub(r"\s+", " ", text).strip(" -"), batch, expiry


def _parse_product_table(
    table: list[tuple[list[str], list[bool]]], page_no: int
) -> list[ParsedLine]:
    header_index = _header_row_index(table)
    if header_index is None:
        return []

    headers = table[header_index][0]
    product_index = _find_header(headers, PRODUCT_HEADERS)
    if product_index is None:
        return []
    hsn_index = _find_header(headers, HSN_HEADERS)
    qty_index = _find_header(headers, QTY_HEADERS)
    free_qty_index = _find_header(headers, FREE_QTY_HEADERS)
    rate_index = _find_header(headers, RATE_HEADERS)
    expiry_index = _find_header(headers, EXPIRY_HEADERS)
    batch_index = _find_header(headers, BATCH_HEADERS)
    pack_index = next(
        (
            index for index, header in enumerate(headers)
            if _norm_header(header) in PACK_HEADERS
        ),
        None,
    )

    lines: list[ParsedLine] = []
    for raw_cells, _flags in table[header_index + 1:]:
      for cells in _expand_multiline_row(raw_cells, product_index):
        if product_index >= len(cells):
            continue
        product_source = str(cells[product_index] or "").strip()
        # OCR can place the description in the adjacent Exp/Mfr column while
        # leaving the declared product cell empty. Use the richest textual
        # non-numeric cell as a conservative fallback.
        if not product_source:
            excluded = {
                index for index in (hsn_index, qty_index, batch_index, pack_index)
                if index is not None
            }
            candidates = [
                str(value or "").strip()
                for index, value in enumerate(cells)
                if index not in excluded and re.search(r"[A-Za-z]{4}", str(value or ""))
            ]
            product_source = max(candidates, key=len, default="")
        product, embedded_batch, embedded_expiry = _embedded_product_metadata(
            product_source
        )
        product_label = product.upper()
        if (
            len(product) < 3
            or len(re.findall(r"[A-Za-z]", product)) < 4
            or product_label.startswith("(")
            or product_label in {
                "CREDIT", "DEBIT", "EXP", "GST", "TAXABLE", "TOTAL",
                "GRAND TOTAL",
            }
        ):
            continue

        hsn = (
            _hsn(str(cells[hsn_index] or ""))
            if hsn_index is not None and hsn_index < len(cells)
            else None
        )
        qty = (
            _number(str(cells[qty_index] or ""))
            if qty_index is not None and qty_index < len(cells)
            else None
        )
        free_qty = (
            _number(str(cells[free_qty_index] or ""))
            if free_qty_index is not None and free_qty_index < len(cells)
            else None
        )
        unit_price = (
            _money(str(cells[rate_index] or ""))
            if rate_index is not None and rate_index < len(cells)
            else None
        )
        if not hsn and qty is None:
            continue

        amount_index, amount_note = _amount_index(headers, len(cells))
        amount = (
            _money(str(cells[amount_index] or ""))
            if amount_index is not None and amount_index < len(cells)
            else None
        )
        expiry = (
            _expiry(str(cells[expiry_index] or ""))
            if expiry_index is not None and expiry_index < len(cells)
            else embedded_expiry
        )
        pack = (
            str(cells[pack_index] or "").strip() or None
            if pack_index is not None and pack_index < len(cells)
            else None
        )
        batch = (
            str(cells[batch_index] or "").strip() or None
            if batch_index is not None and batch_index < len(cells)
            else embedded_batch
        )
        mapped_fields = sum(
            value is not None
            for value in (
                hsn_index, qty_index, expiry_index, pack_index, batch_index
            )
        )
        confidence = 97.0 if mapped_fields >= 3 else 90.0
        lines.append(ParsedLine(
                page_no=page_no,
                product_text=product,
                hsn=hsn,
                qty=qty,
                amount=amount,
                expiry=expiry,
                pack=pack,
                batch=batch,
                ocr_confidence=confidence,
                mapping_note=amount_note,
                free_qty=free_qty,
                paid_qty=qty,
                unit_price=unit_price,
            ))
    return lines


def parse_layout(elements: list[dict[str, Any]], page_no: int) -> list[ParsedLine]:
    """Parse medicine rows from every table layout element on a page."""
    lines: list[ParsedLine] = []
    for element in elements:
        if str(element.get("category", "")).lower() != "table":
            continue
        html = str(element.get("text", ""))
        for table in _tables_from_html(html):
            parsed = _parse_product_table(table, page_no)
            bbox = element.get("bbox")
            if isinstance(bbox, list) and len(bbox) == 4:
                for line in parsed:
                    line.source_bbox = bbox
            lines.extend(parsed)
    return lines


def detect_supplier(elements: list[dict[str, Any]]) -> str | None:
    text = " ".join(str(element.get("text", "")) for element in elements).upper()
    normalized = re.sub(r"[^A-Z0-9]+", " ", text)
    for supplier in SUPPLIER_HINTS:
        if supplier in normalized:
            return supplier
    return None
