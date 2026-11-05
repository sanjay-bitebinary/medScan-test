"""OpenAI-compatible client for the remote dots.ocr vLLM service."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

DEFAULT_BASE_URL = "http://172.17.193.250:8003/v1"
DEFAULT_MODEL = "dots-ocr"

LAYOUT_PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""


class DotsOcrError(RuntimeError):
    """Raised when the remote OCR service cannot produce a usable response."""


def _base_url() -> str:
    return os.environ.get("DOTS_OCR_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("DOTS_OCR_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def service_health(timeout: float = 3.0) -> dict[str, Any]:
    """Return a small, UI-safe health summary for the remote model."""
    url = f"{_base_url()}/models"
    try:
        response = requests.get(url, headers=_headers(), timeout=timeout)
        response.raise_for_status()
        models = response.json().get("data", [])
        names = [model.get("id") for model in models if model.get("id")]
        expected = os.environ.get("DOTS_OCR_MODEL", DEFAULT_MODEL)
        return {
            "ok": expected in names,
            "endpoint": _base_url(),
            "model": expected,
            "available_models": names,
        }
    except Exception as exc:
        return {
            "ok": False,
            "endpoint": _base_url(),
            "model": os.environ.get("DOTS_OCR_MODEL", DEFAULT_MODEL),
            "error": str(exc),
        }


def _data_url(image_path: str) -> str:
    path = Path(image_path)
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _strip_fence(text: str) -> str:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_layout(text: str) -> list[dict[str, Any]]:
    value = _strip_fence(text)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        # Some pages occasionally contain one malformed JSON delimiter even
        # though their HTML tables are complete. Preserve only complete table
        # fragments; HTMLParser will ignore a final incomplete row.
        fragments = re.findall(
            r"<table\b.*?(?:</table>|$)",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        recovered: list[dict[str, Any]] = []
        for fragment in fragments:
            html = (
                fragment
                .replace(r"\"", '"')
                .replace(r"\n", "\n")
                .replace(r"\t", "\t")
                .replace(r"\\", "\\")
            )
            if "</table>" not in html.lower():
                html += "</table>"
            recovered.append({
                "category": "Table",
                "text": html,
                "recovered_from_invalid_json": True,
            })
        if recovered:
            return recovered
        raise DotsOcrError(
            f"dots.ocr returned invalid JSON at character {exc.pos}"
        ) from exc

    if isinstance(parsed, dict):
        for key in ("elements", "layout", "result", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
        else:
            parsed = [parsed]
    if not isinstance(parsed, list):
        raise DotsOcrError("dots.ocr returned an unexpected layout structure")
    return [element for element in parsed if isinstance(element, dict)]


def ocr_page(image_path: str, retries: int = 2) -> dict[str, Any]:
    """Extract one page and return layout elements plus token usage."""
    model = os.environ.get("DOTS_OCR_MODEL", DEFAULT_MODEL)
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": _data_url(image_path)}},
                {
                    "type": "text",
                    "text": "<|img|><|imgpad|><|endofimg|>" + LAYOUT_PROMPT,
                },
            ],
        }],
        "temperature": 0.0,
        "top_p": 0.9,
        "seed": 0,
        "max_completion_tokens": 6000,
    }
    timeout = float(os.environ.get("DOTS_OCR_TIMEOUT", "600"))
    url = f"{_base_url()}/chat/completions"
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = requests.post(
                url,
                headers=_headers(),
                json=payload,
                timeout=(10, timeout),
            )
            if response.status_code >= 400:
                detail = response.text.strip().replace("\n", " ")[:500]
                raise DotsOcrError(
                    f"dots.ocr HTTP {response.status_code}: {detail}"
                )
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            return {
                "elements": _parse_layout(content),
                "raw_content": content,
                "usage": result.get("usage", {}),
                "model": result.get("model", model),
            }
        except (requests.RequestException, KeyError, ValueError, DotsOcrError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** attempt)

    raise DotsOcrError(
        f"Remote dots.ocr request failed after {retries + 1} attempts: {last_error}"
    )
