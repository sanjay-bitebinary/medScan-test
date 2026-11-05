#!/bin/sh
set -eu

mkdir -p "${V3_DATA_DIR:-/app/runtime/v3-data}" \
         "${MASTER_DATA_DIR:-/app/runtime/v3-master}" \
         "${DOTS_OCR_CACHE_DIR:-/app/runtime/v3-cache}"

if [ ! -f "${MASTER_DATA_DIR:-/app/runtime/v3-master}/drug_names.xls" ]; then
  cp /app/data/drug_names.xls "${MASTER_DATA_DIR:-/app/runtime/v3-master}/drug_names.xls"
fi

BIND_ADDRESS="${V3_BIND:-0.0.0.0}"

exec gunicorn \
  --bind "${BIND_ADDRESS}:5003" \
  --workers 1 \
  --threads 6 \
  --timeout 900 \
  --access-logfile - \
  --error-logfile - \
  v3.app:app
