"""Direct extraction for PDFs that contain a usable text layer.

Selectable invoices should not be rasterized and sent through a vision model
when their table coordinates are already available in the PDF.  This module
uses pdfplumber's positioned text/table extraction and falls back to a small
fixed-width parser for DOSPrinter-style invoices.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
import re
from typing import Any

import pdfplumber

from .table_parse import ParsedLine, _parse_product_table


@dataclass
class NativePage:
    page_no: int
    text: str
    elements: list[dict[str, Any]]
    lines: list[ParsedLine]
    width: float
    height: float


def _html_table(rows: list[list[str | None]]) -> str:
    cells: list[str] = ["<table>"]
    for row in rows:
        cells.append("<tr>")
        for value in row:
            cells.append(f"<td>{escape(str(value or ''))}</td>")
        cells.append("</tr>")
    cells.append("</table>")
    return "".join(cells)


def _parse_tables(
    tables: list[list[list[str | None]]], page_no: int
) -> list[ParsedLine]:
    lines: list[ParsedLine] = []
    for rows in tables:
        if not rows:
            continue
        parsed = _parse_product_table(
            [(row, [False] * len(row)) for row in rows], page_no
        )
        for line in parsed:
            line.ocr_confidence = 100.0
        lines.extend(parsed)
    return lines


_FIXED_ROW = re.compile(
    r"^\s*(?P<mrp>\d+(?:\.\d+)?)\s+"
    r"(?P<batch>\S+)\s+"
    r"(?P<expiry>(?:0?[1-9]|1[0-2])[/\-.]\d{2,4})\s+"
    r"(?P<before_hsn>.*?)\s+"
    r"(?P<hsn>\d{4,10})\s+"
    r"(?P<after_hsn>.*)$"
)

_LORD_ROW = re.compile(
    r"^\s*\d+\s+(?P<hsn>\d{4,10})\s+(?P<body>.+)$"
)
_LORD_CONTINUATION = re.compile(
    r"^\s*\S+\s+(?P<batch>\S+)\s+(?P<expiry>\d{1,2}[/\-.]\d{2,4})\s*$"
)


def _parse_lord_item_rows(text: str, page_no: int) -> list[ParsedLine]:
    """Parse LORD PHARMA's two-line, fixed-width invoice rows."""
    lines: list[ParsedLine] = []
    in_table = False
    for raw in text.splitlines():
        row = raw.rstrip()
        upper = row.upper()
        if "ITEM NAME" in upper and "HSN" in upper and "QTY" in upper:
            in_table = True
            continue
        if not in_table:
            continue
        if set(row.strip()) <= {"-", "=", "_"}:
            if lines:
                break
            continue
        match = _LORD_ROW.match(row)
        if match:
            body = match.group("body")
            # Product descriptions can contain numbers (e.g. FG-8). Anchor on
            # decimal rate + integer quantity + decimal gross to avoid cutting
            # the name at those embedded numbers.
            fields = re.match(
                r"(?P<product>.+?)\s+"
                r"(?P<rate>\d+\.\d+)\s+"
                r"(?P<qty>\d+(?:\.\d+)?)\s+"
                r"(?P<gross>\d+\.\d+)\s+(?P<rest>.*)$",
                body,
            )
            if not fields:
                continue
            product = re.sub(r"\s+", " ", fields.group("product")).strip(" -")
            if len(re.findall(r"[A-Za-z]", product)) < 3:
                continue
            numbers = [float(fields.group("gross"))]
            numbers.extend(
                float(value.replace(",", ""))
                for value in re.findall(r"\d+(?:\.\d+)?", fields.group("rest"))
            )
            amount = numbers[-2] if len(numbers) >= 2 else numbers[0]
            lines.append(ParsedLine(
                page_no=page_no,
                product_text=product,
                hsn=match.group("hsn"),
                qty=float(fields.group("qty")),
                amount=amount,
                expiry=None,
                pack=None,
                batch=None,
                ocr_confidence=100.0,
                mapping_note="native LORD fixed-width PDF text",
            ))
            continue
        continuation = _LORD_CONTINUATION.match(row)
        if continuation and lines:
            lines[-1].batch = continuation.group("batch")
            lines[-1].expiry = continuation.group("expiry")
    return lines


def _parse_fixed_width(text: str, page_no: int) -> list[ParsedLine]:
    """Parse DOSPrinter-like rows that have no drawn table borders."""
    lines: list[ParsedLine] = []
    in_table = False
    for raw in text.splitlines():
        row = raw.rstrip()
        upper = row.upper()
        if "ITEM NAME" in upper and "HSN" in upper and "QTY" in upper:
            in_table = True
            continue
        if not in_table:
            continue
        if set(row.strip()) <= {"-", "=", "_"}:
            if lines:
                break
            continue
        match = _FIXED_ROW.match(row)
        if not match:
            continue
        product = re.sub(r"\s+", " ", match.group("before_hsn")).strip()
        if len(re.findall(r"[A-Za-z]", product)) < 3:
            continue
        after = match.group("after_hsn").strip()
        qty_match = re.match(r"(?P<qty>\d+(?:\.\d+)?)\s*(?P<rest>.*)", after)
        qty = float(qty_match.group("qty")) if qty_match else None
        if qty is not None and qty.is_integer():
            qty = int(qty)
        remainder = qty_match.group("rest").strip() if qty_match else ""
        is_free = bool(re.search(r"\*\s*FREE\s*\*", remainder, re.IGNORECASE))
        numbers = [
            float(value.replace(",", ""))
            for value in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", remainder)
        ]
        # DOSPrinter invoices put RATE immediately after QTY on paid rows and
        # the final numeric column is the line's net amount. Free rows repeat
        # the product identity and quantity but explicitly contain * FREE *.
        unit_price = None if is_free or not numbers else numbers[0]
        amount = 0.0 if is_free else (numbers[-1] if numbers else None)
        paid_qty = 0 if is_free else qty
        free_qty = qty if is_free else 0
        # The direct text path intentionally keeps the whole product/packing
        # description; the catalogue standardizer separates strength/form/pack.
        lines.append(ParsedLine(
            page_no=page_no,
            product_text=product,
            hsn=match.group("hsn"),
            qty=qty,
            amount=amount,
            expiry=match.group("expiry"),
            pack=None,
            batch=match.group("batch"),
            ocr_confidence=100.0,
            mapping_note=(
                "native DOSPrinter free-goods row"
                if is_free
                else "native fixed-width PDF text with rate and net amount"
            ),
            free_qty=free_qty,
            paid_qty=paid_qty,
            unit_price=unit_price,
        ))
    return lines or _parse_lord_item_rows(text, page_no)


def extract_native_pages(pdf_path: str) -> dict[int, NativePage]:
    """Return directly parsed pages; empty lines means use dots.ocr."""
    native: dict[int, NativePage] = {}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                try:
                    text = page.extract_text(layout=True) or page.extract_text() or ""
                    tables = page.extract_tables()
                    lines = _parse_tables(tables, page_no)
                    if not lines:
                        lines = _parse_fixed_width(text, page_no)
                    if not lines:
                        continue
                    elements = [{
                        "category": "Text",
                        "text": text,
                        "bbox": [0, 0, float(page.width), float(page.height)],
                    }]
                    native[page_no] = NativePage(
                        page_no=page_no,
                        text=text,
                        elements=elements,
                        lines=lines,
                        width=float(page.width),
                        height=float(page.height),
                    )
                except Exception:
                    # A malformed page should fall back to raster + dots.ocr,
                    # not make the entire upload fail.
                    continue
    except Exception:
        return {}
    return native
