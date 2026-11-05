# MedScan V3

MedScan V3 is the unified Bite Binary product flow:

1. A Maker signs in, uploads one or more PDF invoices, reviews grouped extraction results, corrects fields inline, and selects **Save & submit to Checker**.
2. A Checker signs in, opens the complete batch, compares each PDF's OCR invoice total with its paid quantity × rate subtotal, and confirms approval with their password.
3. Only an approved batch enters central reporting. An Admin signs in to see the completed import, its actors, files, line count, value, and status.

## Isolation

This repository deploys only `app-v3`. It joins the existing Docker network and reuses the existing `dots-ocr` GPU service. It does not create another OCR model, GPU container, V1/V2 application, or shared writable database.

Runtime data is independent under:

- `runtime/v3-data`
- `runtime/v3-master`
- `runtime/v3-cache`

## Start

Copy `.env.example` to `.env`, set the three passwords, then:

```sh
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:5003/health
```

The production reverse proxy publishes the application at `https://ocr.tryaisoft.com/v3/`.

## Important totals

The computed subtotal uses **paid quantity × unit rate**. Free goods increase received quantity but do not increase the computed cost. The OCR-fetched invoice total may differ because of GST, discounts, freight, or rounding; MedScan shows the variance and missing-rate count explicitly.
