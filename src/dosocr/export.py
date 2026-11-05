"""Export standardized line items to CSV, Excel, JSON, SQLite and .sql."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile

import pandas as pd

COLUMNS = [
    "page_no", "supplier_hint", "raw_item_text", "canonical_name",
    "generic_name", "brand_name", "strength", "form", "pack",
    "batch", "hsn", "qty", "amount", "expiry", "expiry_status",
    "days_to_expiry", "match_confidence",
    "catalog_name", "catalog_match_score", "catalog_exact_match",
    "catalog_status", "ocr_confidence", "needs_review",
]

DDL = """
CREATE TABLE IF NOT EXISTS po_line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_no INTEGER,
    supplier_hint TEXT,
    raw_item_text TEXT NOT NULL,
    canonical_name TEXT,
    generic_name TEXT,
    brand_name TEXT,
    strength TEXT,
    form TEXT,
    pack TEXT,
    batch TEXT,
    hsn TEXT,
    qty REAL,
    amount REAL,
    expiry TEXT,
    expiry_status TEXT,
    days_to_expiry INTEGER,
    match_confidence REAL,
    catalog_name TEXT,
    catalog_match_score REAL,
    catalog_exact_match INTEGER DEFAULT 0,
    catalog_status TEXT,
    ocr_confidence REAL,
    needs_review INTEGER DEFAULT 0,
    source_file TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def to_dataframe(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[COLUMNS]


def export_all(records: list[dict], out_dir: str, basename: str = "po_extract",
               source_file: str = "") -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    df = to_dataframe(records)
    paths = {}

    csv_path = os.path.join(out_dir, f"{basename}.csv")
    df.to_csv(csv_path, index=False)
    paths["csv"] = csv_path

    xlsx_path = os.path.join(out_dir, f"{basename}.xlsx")
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Line Items")
        review = df[df["needs_review"] == 1]
        review.to_excel(xw, index=False, sheet_name="Needs Review")
    paths["xlsx"] = xlsx_path

    json_path = os.path.join(out_dir, f"{basename}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"source_file": source_file, "line_items": records},
                  f, indent=2, ensure_ascii=False)
    paths["json"] = json_path

    # SQLite database ready to query / integrate
    # (built in a local temp dir first: SQLite locking fails on some network mounts)
    tmp_db = os.path.join(tempfile.mkdtemp(prefix="poocr_db_"), f"{basename}.db")
    db_path = os.path.join(out_dir, f"{basename}.db")
    con = sqlite3.connect(tmp_db)
    con.executescript(DDL)
    df2 = df.copy()
    df2["source_file"] = source_file
    df2.to_sql("po_line_items", con, if_exists="append", index=False)
    con.commit()
    con.close()
    shutil.copyfile(tmp_db, db_path)
    paths["sqlite"] = db_path

    # Plain .sql insert script for their existing DB (edit table name to match)
    sql_path = os.path.join(out_dir, f"{basename}_inserts.sql")
    with open(sql_path, "w", encoding="utf-8") as f:
        f.write("-- Generated PO line-item inserts. Adjust table/column names to your schema.\n")
        f.write(DDL + "\n")
        for _, r in df.iterrows():
            vals = []
            for col in COLUMNS:
                v = r[col]
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    vals.append("NULL")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                else:
                    vals.append("'" + str(v).replace("'", "''") + "'")
            f.write(f"INSERT INTO po_line_items ({', '.join(COLUMNS)}) VALUES ({', '.join(vals)});\n")
    paths["sql"] = sql_path
    return paths
