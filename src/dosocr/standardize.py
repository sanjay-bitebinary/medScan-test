"""Catalog-authoritative drug-name standardization.

``drug_names.xls`` is the only medicine identity source. Exact catalog matches
may supply the catalog spelling and variant attributes. Fuzzy matches are
review suggestions only and never silently replace the OCR text.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
import xlrd

try:
    import xlwt
except ImportError:  # pragma: no cover - installed in deployed app image
    xlwt = None

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")

FORM_SYNONYMS = {
    "TABLET": ["TAB", "TABS", "TABLET", "TABLETS", "TB", "DT", "CR1", "CN1"],
    "CAPSULE": ["CAP", "CAPS", "CAPSULE", "CAPSULES", "ROTACAPS", "ROTACAP"],
    "INJECTION": [
        "INJ", "INJ.", "NJ", "INJECTION", "IV", "AMP", "AMPOULE", "VIAL",
    ],
    "SYRUP": ["SYP", "SYR", "SYRUP", "SUSP", "SUSPENSION"],
    "SOLUTION": ["SOLUTION", "SOLN"],
    "CREAM": ["CREAM", "CRE", "CRM", "OINTMENT", "OINT", "GEL"],
    "DROPS": ["DROPS", "DROP", "EYEDROPS", "E/D", "EYE"],
    "INHALER": [
        "INHALER", "INHALER#", "INHAL", "MDI", "CFC", "RESPU", "RESPULES",
        "TRANSPULES", "TRANSCAPS",
    ],
    "SPRAY": ["SPRAY", "NASAL"],
    "TUBE": ["TUBE"],
    "SACHET": ["SACHET", "SACHETS"],
    "LOTION": ["LOTION"],
    "POWDER": ["POWDER", "PWD"],
    "SUPPOSITORY": ["SUPP", "SUPPO", "SUPPOSITORY", "SUPPOSITORIES"],
}
_FORM_LOOKUP = {
    synonym: canonical
    for canonical, synonyms in FORM_SYNONYMS.items()
    for synonym in synonyms
}

_UNIT_ALIASES = {
    "MG": "MG", "MGS": "MG", "MG.": "MG",
    "MCG": "MCG", "UG": "MCG",
    "G": "G", "GM": "G", "GMS": "G", "GRAM": "G", "GRAMS": "G", "GRM": "G",
    "ML": "ML", "ML.": "ML",
    "IU": "IU", "%": "%", "W/V": "%", "W/W": "%",
}
_UNIT_RE = r"(?:MG|MGS|MCG|UG|GMS?|G|GRAMS?|GRM|ML|IU|%)"

STRENGTH_RE = re.compile(
    rf"\b(\d+(?:[.,]\d+)?)\s*({_UNIT_RE})(?![A-Z0-9])"
    rf"(?:\s*/\s*(\d+(?:[.,]\d+)?)?\s*({_UNIT_RE})(?![A-Z0-9]))?",
    re.IGNORECASE,
)
PACK_RE = re.compile(
    r"\b(?:\d+\s*X\s*\d+(?:\s*\w+)?|\d+\s*'?(?:S|T)\b|1X\d+\w*)\b",
    re.IGNORECASE,
)
NOISE_RE = re.compile(r"[^A-Z0-9%./+\- ]")

STOPWORDS = {
    "WITH", "AND", "FOR", "THE", "NEW", "PLUS", "FORTE", "KID", "JUNIOR",
    "FREE", "CFC", "CFC-FREE", "ORAL", "NASAL", "EYE", "EAR", "ADULT",
    "MR", "SR", "XR", "CR", "ER", "OD", "DS", "LS", "LB", "HS",
}
_CATALOG_NOISE = {
    "CAP", "CAPSULE", "CREAM", "DROP", "DROPS", "GENERAL", "INJ",
    "INJECTION", "TABLET", "TAB", "SYRUP", "SYP", "VIAL",
}


def _catalog_key(value: str) -> str:
    """Normalize catalog/OCR text without correcting or guessing characters."""
    key = re.sub(
        r"\s+", " ", re.sub(r"[^A-Z0-9]+", " ", value.upper())
    ).strip()
    # Common dotted initial-style product names: K.Z -> KZ.
    return re.sub(r"\b([A-Z])\s+([A-Z])\b", r"\1\2", key)


def _usable_catalog_key(value: str) -> bool:
    return (
        len(value) >= 4
        and value not in _CATALOG_NOISE
        and any(character.isalpha() for character in value)
    )


def _loadable_catalog_key(value: str) -> bool:
    """Allow short legitimate master names for exact matching, never fuzzy."""
    return (
        len(value) >= 2
        and value not in _CATALOG_NOISE
        and any(character.isalpha() for character in value)
    )


def _cell_text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm_unit(unit: str) -> str:
    return _UNIT_ALIASES.get(unit.upper().strip(), unit.upper())


def _clean(text: str) -> str:
    cleaned = text.upper()
    cleaned = cleaned.replace("’", "'").replace("`", "'")
    cleaned = re.sub(r"\bPERCENT(?:AGE)?\b", "%", cleaned)
    cleaned = NOISE_RE.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_strengths(text: str) -> list[str]:
    strengths: list[str] = []
    for match in STRENGTH_RE.finditer(text):
        value = match.group(1).replace(",", ".")
        strength = f"{value}{_norm_unit(match.group(2))}"
        if match.group(4):
            second_value = match.group(3) or ""
            strength = (
                f"{value}{_norm_unit(match.group(2))}/"
                f"{second_value}{_norm_unit(match.group(4))}"
            )
        strengths.append(strength)
    return strengths


def _catalog_strength(value: object, unit: object) -> str | None:
    number = _cell_text(value)
    unit_text = _cell_text(unit).upper()
    if number in {"", "."} or unit_text in {"", "."}:
        return None
    return f"{number}{_norm_unit(unit_text)}"


def _catalog_form(value: object) -> str | None:
    text = _catalog_key(_cell_text(value))
    if not text or text == "GENERAL":
        return None
    for token in text.split():
        form = _FORM_LOOKUP.get(token.strip(".#"))
        if form:
            return form
    return None


def _forms_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    liquid_oral = {"DROPS", "SOLUTION", "SYRUP"}
    return left in liquid_oral and right in liquid_oral


def _is_package_measure(measure: str, form: str | None) -> bool:
    """Classify total fill/volume as pack quantity, not active strength."""
    compact = measure.upper().replace(" ", "")
    if "/" in compact or compact.endswith("%"):
        return False
    unit_match = re.search(r"([A-Z%]+)$", compact)
    unit = unit_match.group(1) if unit_match else ""
    if unit in {"NOS", "MD", "MDI", "ACTUATION", "ACTUATIONS"}:
        return True
    if unit == "ML" and form in {
        "INJECTION", "SYRUP", "SOLUTION", "DROPS", "SPRAY", "INHALER",
    }:
        return True
    if unit == "G" and form in {"CREAM", "LOTION", "SPRAY"}:
        return True
    return False


def _pack_key(value: str | None) -> str:
    key = re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())
    return re.sub(r"MDI?$", "MD", key)


def _pack_unit(value: str | None) -> str:
    key = _pack_key(value)
    match = re.search(r"([A-Z]+)$", key)
    return match.group(1) if match else ""


def _usable_pack(value: str | None) -> str | None:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    return None if cleaned.upper() in {"", "0", ".", "-", "N/A", "NA"} else cleaned


def _merge_pack(current: str | None, addition: str | None) -> str | None:
    current = _usable_pack(current)
    addition = _usable_pack(addition)
    if not current:
        return addition
    if not addition:
        return current
    current_key = _pack_key(current)
    addition_key = _pack_key(addition)
    if current_key == addition_key or addition_key in current_key:
        return current
    if current_key in addition_key:
        return addition
    return f"{current} · {addition}"


def _catalog_base_key(raw_name: str) -> str:
    """Strip master strength/form annotations to get its medicine identity."""
    text = STRENGTH_RE.sub(" ", _clean(raw_name))
    text = PACK_RE.sub(" ", text)
    tokens = [
        token
        for token in text.split()
        if not _FORM_LOOKUP.get(token.strip(".#"))
    ]
    return _catalog_key(" ".join(tokens))


def _numeric_signature(value: str | None) -> str | None:
    """Return only the numeric strength components for unitless OCR matching."""
    if not value:
        return None
    match = re.match(
        r"^(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)*)",
        value.replace(",", ".").replace(" ", ""),
    )
    return match.group(1) if match else None


def _unitless_numeric_hints(text: str) -> list[str]:
    """Find raw numeric variants after explicit strengths and packs are removed."""
    stripped = PACK_RE.sub(" ", STRENGTH_RE.sub(" ", text))
    # OCR commonly joins a variant to the preceding word: CHRON500.
    stripped = _split_joined_tokens(stripped)
    hints = re.findall(
        r"(?<![A-Z0-9])\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)*(?![A-Z0-9])",
        stripped,
    )
    return list(dict.fromkeys(hint.replace(",", ".") for hint in hints))


def _split_joined_tokens(text: str) -> str:
    """Separate OCR-joined form/strength tokens without splitting PIT0CIN."""
    text = re.sub(
        r"\b(INJ|TABS?|CAPS?|SYP|SYR)[.#]?(?=\d)",
        r"\1 ",
        text,
    )
    text = re.sub(r"(?<=\d)(?=(?:TABS?|CAPS?)\b)", " ", text)
    return re.sub(
        r"(?<=[A-Z])(?=\d+(?:\.\d+)?(?:\s|$))",
        " ",
        text,
    )


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    base_name: str
    key: str
    strength: str | None
    form: str | None
    pack: str | None


@dataclass(frozen=True)
class CatalogMatch:
    name: str | None
    score: float
    exact: bool
    status: str
    entry: CatalogEntry | None = None


class DrugDictionary:
    """Authoritative hospital product catalog loaded only from drug_names.xls."""

    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self.catalog_by_key: dict[str, list[CatalogEntry]] = {}
        catalog_path = os.path.join(data_dir, "drug_names.xls")
        if os.path.exists(catalog_path):
            book = xlrd.open_workbook(catalog_path, on_demand=True)
            sheet = book.sheet_by_index(0)
            for row_no in range(sheet.nrows):
                raw_name = str(sheet.cell_value(row_no, 0)).strip()
                raw_key = _catalog_key(raw_name)
                key = _catalog_base_key(raw_name) or raw_key
                if not raw_name or not _loadable_catalog_key(key):
                    continue
                form = _catalog_form(
                    sheet.cell_value(row_no, 3) if sheet.ncols > 3 else ""
                )
                name_measures = _extract_strengths(_clean(raw_name))
                column_measure = _catalog_strength(
                    sheet.cell_value(row_no, 1) if sheet.ncols > 1 else "",
                    sheet.cell_value(row_no, 2) if sheet.ncols > 2 else "",
                )
                strength = next(
                    (
                        measure for measure in name_measures
                        if not _is_package_measure(measure, form)
                    ),
                    None,
                )
                pack = next(
                    (
                        measure for measure in name_measures
                        if _is_package_measure(measure, form)
                    ),
                    None,
                )
                if column_measure:
                    if _is_package_measure(column_measure, form):
                        pack = pack or column_measure
                    else:
                        strength = strength or column_measure
                entry = CatalogEntry(
                    name=raw_name,
                    base_name=key,
                    key=key,
                    strength=strength,
                    form=form,
                    pack=pack,
                )
                entries = self.catalog_by_key.setdefault(key, [])
                if entry not in entries:
                    entries.append(entry)
            book.release_resources()

        self._catalog_keys = [
            key for key in self.catalog_by_key if _usable_catalog_key(key)
        ]
        self._keys_by_first_token: dict[str, list[str]] = {}
        for key in self.catalog_by_key:
            self._keys_by_first_token.setdefault(key.split()[0], []).append(key)

    def add_entry(
        self,
        name: str,
        strength: str | None = None,
        form: str | None = None,
        pack: str | None = None,
    ) -> dict[str, object]:
        """Append a confirmed medicine to the authoritative XLS catalog."""
        clean_name = re.sub(r"\s+", " ", str(name or "")).strip()
        if not clean_name or not _usable_catalog_key(_catalog_key(clean_name)):
            raise ValueError("Enter a valid medicine name before adding it.")
        key = _catalog_base_key(clean_name) or _catalog_key(clean_name)
        existing = self.catalog_by_key.get(key, [])
        if existing:
            return {
                "added": False,
                "name": existing[0].name,
                "message": "This medicine is already in the medicine list.",
            }
        if xlwt is None:
            raise RuntimeError("The XLS writer dependency is unavailable.")

        catalog_path = os.path.join(self.data_dir, "drug_names.xls")
        if not os.path.isfile(catalog_path):
            raise FileNotFoundError("The medicine list file is unavailable.")
        source = xlrd.open_workbook(catalog_path, on_demand=False)
        source_sheet = source.sheet_by_index(0)
        workbook = xlwt.Workbook()
        target_sheet = workbook.add_sheet(source_sheet.name[:31] or "Sheet1")
        for row_no in range(source_sheet.nrows):
            for col_no in range(source_sheet.ncols):
                value = source_sheet.cell_value(row_no, col_no)
                if value != "":
                    target_sheet.write(row_no, col_no, value)

        strength_text = str(strength or "").strip().upper()
        strength_number: object = "."
        strength_unit = "."
        strength_match = re.match(
            rf"^(\d+(?:\.\d+)?)(\s*{_UNIT_RE}(?:\s*/\s*\d+(?:\.\d+)?\s*{_UNIT_RE})?)$",
            strength_text,
            flags=re.IGNORECASE,
        )
        if strength_match:
            strength_number = float(strength_match.group(1))
            if float(strength_match.group(1)).is_integer():
                strength_number = int(float(strength_match.group(1)))
            strength_unit = re.sub(r"\s+", "", strength_match.group(2)).upper()
        elif strength_text:
            strength_number = strength_text
        row_no = source_sheet.nrows
        target_sheet.write(row_no, 0, clean_name)
        target_sheet.write(row_no, 1, strength_number)
        target_sheet.write(row_no, 2, strength_unit)
        target_sheet.write(row_no, 3, str(form or ".").strip().upper() or ".")
        target_sheet.write(
            row_no,
            4,
            f"{clean_name} - {str(form or 'GENERAL').strip().upper() or 'GENERAL'}",
        )

        os.makedirs(self.data_dir, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="drug_names_", suffix=".xls", dir=self.data_dir, delete=False
        )
        temp_path = handle.name
        handle.close()
        try:
            workbook.save(temp_path)
            os.replace(temp_path, catalog_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        self.__init__(self.data_dir)
        return {
            "added": True,
            "name": clean_name,
            "strength": strength_text or None,
            "form": str(form or "").strip().upper() or None,
            "pack": str(pack or "").strip() or None,
            "message": "Medicine added to the medicine list.",
        }

    @staticmethod
    def _select_variant(
        entries: list[CatalogEntry],
        strength: str | None,
        form: str | None,
        numeric_hints: list[str] | None = None,
        pack_hint: str | None = None,
    ) -> tuple[CatalogEntry | None, bool]:
        """Return a unique catalog variant and whether supplied details conflict."""
        candidates = entries
        mismatch = False

        if strength:
            known = [entry for entry in candidates if entry.strength]
            matching = [entry for entry in known if entry.strength == strength]
            if matching:
                candidates = matching
            elif known:
                mismatch = True

        if not strength and numeric_hints:
            wanted = set(numeric_hints)
            matching = [
                entry for entry in candidates
                if _numeric_signature(entry.strength) in wanted
            ]
            if matching:
                candidates = matching

        if form and not mismatch:
            known = [entry for entry in candidates if entry.form]
            matching = [
                entry for entry in known
                if _forms_compatible(entry.form, form)
            ]
            if matching:
                candidates = matching
            elif known:
                mismatch = True

        if pack_hint and not mismatch:
            wanted_pack = _pack_key(pack_hint)
            known = [entry for entry in candidates if entry.pack]
            matching = [
                entry for entry in known
                if (
                    entry.pack
                    and (
                        _pack_key(entry.pack) == wanted_pack
                        or _pack_key(entry.pack) in wanted_pack
                    )
                )
            ]
            if matching:
                candidates = matching
            elif any(
                _pack_unit(entry.pack) == _pack_unit(pack_hint)
                for entry in known
            ):
                mismatch = True

        if mismatch:
            return entries[0] if entries else None, True

        unique = {
            (entry.base_name, entry.strength, entry.form, entry.pack): entry
            for entry in candidates
        }
        if len(unique) == 1:
            return next(iter(unique.values())), False
        return None, False

    def _exact_name_key(self, raw_text: str) -> str | None:
        """Find the earliest complete catalog-name token sequence in OCR text."""
        tokens = _catalog_key(raw_text).split()
        matches: list[tuple[int, int, str]] = []
        for start, token in enumerate(tokens):
            for key in self._keys_by_first_token.get(token, []):
                key_tokens = key.split()
                if tokens[start:start + len(key_tokens)] == key_tokens:
                    matches.append((start, -len(key_tokens), key))
        return min(matches)[2] if matches else None

    def match_catalog(
        self,
        raw_text: str,
        name_tokens: list[str],
        strength: str | None = None,
        form: str | None = None,
        numeric_hints: list[str] | None = None,
        pack_hint: str | None = None,
    ) -> CatalogMatch:
        """Match against the XLS catalog; fuzzy results are suggestions only."""
        if not self._catalog_keys:
            return CatalogMatch(None, 0.0, False, "catalog_unavailable")

        exact_key = self._exact_name_key(raw_text)
        suppressed_exact_key: str | None = None
        if exact_key and name_tokens:
            probe = _catalog_key(" ".join(name_tokens[:4]))
            more_specific = [
                key for key in self.catalog_by_key
                if key.startswith(exact_key + " ")
                and fuzz.ratio(probe, key) >= 90
            ]
            # Do not let a shorter exact family name hide an OCR error in a
            # more-specific product name (ENCORATE CHRON -> CHRONO).
            if more_specific:
                suppressed_exact_key = exact_key
                exact_key = None
        if exact_key:
            entries = self.catalog_by_key[exact_key]
            entry, attribute_mismatch = self._select_variant(
                entries, strength, form, numeric_hints, pack_hint
            )
            if attribute_mismatch:
                display_name = (
                    entry.base_name if entry else entries[0].base_name
                )
                return CatalogMatch(
                    display_name,
                    100.0,
                    False,
                    "attribute_mismatch",
                    entry,
                )
            display_name = (
                entry.base_name if entry else entries[0].base_name
            )
            return CatalogMatch(display_name, 100.0, True, "exact", entry)

        cleaned_tokens = [
            _catalog_key(token)
            for token in name_tokens
            if _usable_catalog_key(_catalog_key(token))
        ][:8]
        probes: set[str] = set()
        for width in range(1, min(4, len(cleaned_tokens)) + 1):
            for start in range(len(cleaned_tokens) - width + 1):
                probes.add(" ".join(cleaned_tokens[start:start + width]))
        if not probes:
            probes.add(_catalog_key(raw_text))

        best_name: str | None = None
        best_score = 0.0
        best_probe = ""
        best_key = ""
        nearest_score = 0.0
        for probe in probes:
            for match in process.extract(
                probe, self._catalog_keys, scorer=fuzz.ratio, limit=5
            ):
                candidate_key = match[0]
                if candidate_key == suppressed_exact_key:
                    continue
                candidate_score = float(match[1])
                nearest_score = max(nearest_score, candidate_score)
                length_ratio = (
                    min(len(probe), len(candidate_key))
                    / max(len(probe), len(candidate_key))
                )
                plausible = (
                    candidate_score >= 85
                    and length_ratio >= 0.70
                    and (
                        probe[:1] == candidate_key[:1]
                        or candidate_score >= 95
                    )
                )
                if plausible and candidate_score > best_score:
                    best_name = self.catalog_by_key[candidate_key][0].base_name
                    best_score = candidate_score
                    best_probe = probe
                    best_key = candidate_key

        if best_key:
            entry, _mismatch = self._select_variant(
                self.catalog_by_key[best_key],
                strength,
                form,
                numeric_hints,
                pack_hint,
            )
            return CatalogMatch(
                best_name,
                round(best_score, 1),
                False,
                "fuzzy",
                entry,
            )

        return CatalogMatch(None, round(nearest_score, 1), False, "unmatched")

    @staticmethod
    def _entry_canonical(entry: CatalogEntry) -> str:
        return entry.base_name

    def suggest(self, query: str, limit: int = 8) -> list[dict]:
        """Return catalog-backed autocomplete suggestions."""
        query_key = _catalog_key(query)
        if len(query_key) < 2:
            return []
        ranked_keys = [
            key for key in self.catalog_by_key
            if key.startswith(query_key)
        ]
        ranked_keys.extend(
            key for key in self.catalog_by_key
            if query_key in key and key not in ranked_keys
        )
        if len(ranked_keys) < limit:
            for match in process.extract(
                query_key,
                list(self.catalog_by_key),
                scorer=fuzz.WRatio,
                limit=max(limit * 2, 12),
                score_cutoff=72,
            ):
                if match[0] not in ranked_keys:
                    ranked_keys.append(match[0])

        suggestions: list[dict] = []
        seen: set[str] = set()
        for key in ranked_keys:
            for entry in self.catalog_by_key[key]:
                canonical = self._entry_canonical(entry)
                canonical_key = _catalog_key(canonical)
                if canonical_key in seen:
                    continue
                seen.add(canonical_key)
                suggestions.append({
                    "canonical_name": canonical,
                    "catalog_name": entry.base_name,
                    "strength": entry.strength,
                    "form": entry.form,
                })
                if len(suggestions) >= limit:
                    return suggestions
        return suggestions

    def entries(self, query: str = "", limit: int = 300) -> list[dict]:
        """Expose the XLS master for the role-aware master-data screen."""
        needle = _catalog_key(query)
        result: list[dict] = []
        seen: set[tuple[str, str | None, str | None, str | None]] = set()
        for key in sorted(self.catalog_by_key):
            if needle and needle not in key:
                continue
            for entry in self.catalog_by_key[key]:
                signature = (entry.base_name, entry.strength, entry.form, entry.pack)
                if signature in seen:
                    continue
                seen.add(signature)
                result.append({
                    "catalog_name": entry.name,
                    "canonical_name": entry.base_name,
                    "strength": entry.strength,
                    "form": entry.form,
                    "pack": entry.pack,
                })
                if len(result) >= max(1, min(1000, int(limit))):
                    return result
        return result


@dataclass
class StandardizedItem:
    raw_text: str
    generic_name: str | None = None
    brand_name: str | None = None
    strength: str | None = None
    form: str | None = None
    pack: str | None = None
    canonical_name: str = ""
    match_confidence: float = 0.0
    catalog_name: str | None = None
    catalog_match_score: float = 0.0
    catalog_exact_match: bool = False
    catalog_status: str = "unmatched"
    residual_tokens: list[str] = field(default_factory=list)


def standardize_item(
    raw: str,
    dictionary: DrugDictionary,
    pack_hint: str | None = None,
) -> StandardizedItem:
    item = StandardizedItem(raw_text=raw)
    text = _split_joined_tokens(_clean(raw))

    item.pack = _usable_pack(pack_hint)
    packs = PACK_RE.findall(text)
    if packs:
        item.pack = _merge_pack(
            item.pack,
            re.sub(r"\s+", " ", packs[0].upper()).strip(),
        )
        text = PACK_RE.sub(" ", text)

    # Detect the form before classifying measurements: 30GM is a package fill
    # for a cream, while 4.5GM is a strength for an injection.
    form_tokens = re.sub(r"(?<=\d)(?=(?:TABS?|CAPS?)\b)", " ", text).split()
    for token in form_tokens:
        canonical_form = _FORM_LOOKUP.get(token.strip(".#"))
        if canonical_form:
            item.form = canonical_form
            break

    measurements = _extract_strengths(text)
    for measurement in measurements:
        if _is_package_measure(measurement, item.form):
            item.pack = _merge_pack(item.pack, measurement)
        elif not item.strength:
            item.strength = measurement
    text = STRENGTH_RE.sub(" ", text)
    text = re.sub(r"(?<=\d)(?=(?:TABS?|CAPS?)\b)", " ", text)

    tokens = [token for token in re.sub(r"\s+", " ", text).split() if token]
    kept: list[str] = []
    for token in tokens:
        canonical_form = _FORM_LOOKUP.get(token.strip(".#"))
        if canonical_form and not item.form:
            item.form = canonical_form
        elif not canonical_form:
            kept.append(token)

    name_tokens = [
        token
        for token in kept
        if (
            token not in STOPWORDS
            and not re.fullmatch(
                r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)*",
                token,
            )
            and len(token) > 1
        )
    ]
    item.residual_tokens = name_tokens

    catalog_match = dictionary.match_catalog(
        raw,
        name_tokens,
        strength=item.strength,
        form=item.form,
        numeric_hints=_unitless_numeric_hints(_split_joined_tokens(_clean(raw))),
        pack_hint=item.pack,
    )
    item.catalog_name = catalog_match.name
    item.catalog_match_score = catalog_match.score
    item.catalog_exact_match = catalog_match.exact
    item.catalog_status = catalog_match.status
    item.match_confidence = catalog_match.score

    # Only an exact master match may replace OCR-derived identity or attributes.
    # The standardized name deliberately remains the product identity only;
    # strength, dosage form, and pack belong exclusively in their own fields.
    if catalog_match.exact:
        base = catalog_match.name or " ".join(name_tokens) or text
        if catalog_match.entry:
            item.strength = catalog_match.entry.strength or item.strength
            item.form = catalog_match.entry.form or item.form
            item.pack = _merge_pack(item.pack, catalog_match.entry.pack)
    elif catalog_match.status == "attribute_mismatch" and catalog_match.name:
        base = catalog_match.name
    else:
        base = " ".join(name_tokens) or text
        if catalog_match.status == "fuzzy" and catalog_match.entry:
            entry_signature = _numeric_signature(catalog_match.entry.strength)
            numeric_hints = set(_unitless_numeric_hints(_clean(raw)))
            if entry_signature and entry_signature in numeric_hints:
                item.strength = catalog_match.entry.strength
            item.form = item.form or catalog_match.entry.form
            item.pack = _merge_pack(item.pack, catalog_match.entry.pack)

    item.canonical_name = base.strip()
    return item
