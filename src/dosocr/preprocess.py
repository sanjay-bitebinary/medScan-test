"""PDF -> cleaned page images: rasterize, auto-rotate, deskew, denoise."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np


def _executable(name: str, env_var: str, candidates: list[str]) -> str:
    """Resolve native tools reliably in detached Windows processes."""
    configured = os.environ.get(env_var)
    if configured:
        return configured
    found = shutil.which(name)
    if found and (os.name != "nt" or found.lower().endswith(".exe")):
        return found
    for candidate in candidates:
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path
    return name


PDFTOPPM = _executable(
    "pdftoppm",
    "PDFTOPPM_CMD",
    [
        r"%LOCALAPPDATA%\Programs\poppler\Library\bin\pdftoppm.exe",
        r"%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime"
        r"\dependencies\native\poppler\Library\bin\pdftoppm.exe",
    ],
)
@dataclass
class PageImage:
    page_no: int
    path: str
    rotation_applied: int  # degrees CW applied to correct orientation


def pdf_to_images(pdf_path: str, out_dir: str, dpi: int = 300) -> list[str]:
    """Rasterize a PDF to PNG pages using poppler (pdftoppm)."""
    os.makedirs(out_dir, exist_ok=True)
    prefix = os.path.join(out_dir, "page")
    subprocess.run(
        [PDFTOPPM, "-r", str(dpi), "-png", pdf_path, prefix],
        check=True, capture_output=True,
    )
    pages = sorted(
        (f for f in os.listdir(out_dir) if f.startswith("page") and f.endswith(".png")),
        key=lambda f: int(re.search(r"(\d+)", f).group(1)),
    )
    return [os.path.join(out_dir, f) for f in pages]


def detect_rotation(image_path: str) -> int:
    """Detect sideways text using pixel projections, without another OCR engine.

    Pharmacy scans in this workflow are frequently stored as portrait images
    whose text baselines run vertically. Horizontal text produces stronger row
    projection peaks; vertical text produces stronger column projection peaks.
    A conservative threshold avoids rotating ordinary portrait documents.
    """
    image = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return 0
    height, width = image.shape[:2]
    scale = min(1.0, 800.0 / max(height, width))
    if scale < 1.0:
        image = cv2.resize(
            image,
            None,
            fx=scale,
            fy=scale,
            interpolation=cv2.INTER_AREA,
        )
    binary = cv2.threshold(
        image,
        0,
        1,
        cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU,
    )[1]
    row_projection = binary.sum(axis=1) / max(1, binary.shape[1])
    col_projection = binary.sum(axis=0) / max(1, binary.shape[0])
    row_variance = float(np.var(row_projection))
    col_variance = float(np.var(col_projection))
    if col_variance > row_variance * 1.02:
        return 270
    return 0


def rotate_image(img: np.ndarray, degrees_cw: int) -> np.ndarray:
    rot = {90: cv2.ROTATE_90_CLOCKWISE,
           180: cv2.ROTATE_180,
           270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(degrees_cw % 360)
    return cv2.rotate(img, rot) if rot is not None else img


def alternate_orientation(image_path: str, out_path: str) -> str:
    """Write a 180-degree alternate for pages whose auto-orientation is ambiguous."""
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Unable to read rasterized page: {image_path}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if not cv2.imwrite(out_path, rotate_image(image, 180)):
        raise ValueError(f"Unable to write alternate page: {out_path}")
    return out_path


def deskew(img: np.ndarray, max_angle: float = 10.0) -> np.ndarray:
    """Correct small residual skew using the minimum-area-rect of text pixels."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(inv > 0))
    if len(coords) < 100:
        return img
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.05 or abs(angle) > max_angle:
        return img
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


def enhance(img: np.ndarray) -> np.ndarray:
    """Grayscale + adaptive contrast + light denoise for OCR."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    gray = cv2.medianBlur(gray, 3)  # fast salt-and-pepper removal
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def preprocess_pdf(
    pdf_path: str,
    work_dir: str | None = None,
    dpi: int = 300,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[PageImage]:
    """Rasterize, orient, and deskew pages while preserving source detail.

    dots.ocr is a vision-language model and generally reads faint invoices
    better from the oriented color raster. Grayscale enhancement remains an
    opt-in fallback through DOSOCR_ENHANCE=1.
    """
    work_dir = work_dir or tempfile.mkdtemp(prefix="poocr_")
    raw_dir = os.path.join(work_dir, "raw")
    clean_dir = os.path.join(work_dir, "clean")
    os.makedirs(clean_dir, exist_ok=True)

    report = progress or (lambda _value, _ceiling, _message: None)
    report(3, 12, "Rasterizing PDF pages at 300 DPI…")
    raw_pages = pdf_to_images(pdf_path, raw_dir, dpi)
    total_pages = len(raw_pages)
    report(12, 14, f"PDF rasterized — {total_pages} page(s) found")

    results: list[PageImage] = []
    for i, raw_path in enumerate(raw_pages, start=1):
        start = 14 + int(10 * (i - 1) / max(1, total_pages))
        end = 14 + int(10 * i / max(1, total_pages))
        report(start, max(start + 1, end), f"Preparing page {i}/{total_pages}…")
        rot = detect_rotation(raw_path)
        img = cv2.imread(raw_path)
        img = rotate_image(img, rot)
        img = deskew(img)
        if os.environ.get("DOSOCR_ENHANCE", "").strip() == "1":
            img = enhance(img)
        out_path = os.path.join(clean_dir, f"page-{i}.png")
        cv2.imwrite(out_path, img)
        results.append(PageImage(page_no=i, path=out_path, rotation_applied=rot))
        report(
            max(start + 1, end),
            min(24, max(start + 1, end + 1)),
            f"Prepared page {i}/{total_pages}",
        )
    return results
