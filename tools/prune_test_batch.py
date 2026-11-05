"""Remove an unapproved acceptance-test batch by its exact public ID."""
from __future__ import annotations

import argparse
import sqlite3


parser = argparse.ArgumentParser()
parser.add_argument("database")
parser.add_argument("public_id")
parser.add_argument("--confirm", action="store_true")
args = parser.parse_args()

if not args.confirm:
    raise SystemExit("Refusing to mutate without --confirm")

with sqlite3.connect(args.database, timeout=15) as db:
    db.execute("PRAGMA foreign_keys=ON")
    row = db.execute(
        "SELECT id,status FROM import_batches WHERE public_id=?",
        (args.public_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"Batch {args.public_id} does not exist")
    batch_id, status = row
    if status in {"submitted", "approved"}:
        raise SystemExit(f"Refusing to remove {status} batch {args.public_id}")
    print(f"Removing test batch {args.public_id} with status {status}")
    db.execute("DELETE FROM audit_log WHERE batch_id=?", (batch_id,))
    db.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
