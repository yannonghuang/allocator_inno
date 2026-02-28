"""SKU pattern helpers for raw material detection (1xx-xxxx, 2xx-xxxx, 3xx-xxxx)."""
import re

# Raw material patterns: first digit 1/2/3, then two chars, dash, four chars (e.g. 100-0018, 280-0845)
RAW_MATERIAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("1xx-xxxx", re.compile(r"^1..-....")),
    ("2xx-xxxx", re.compile(r"^2..-....")),
    ("3xx-xxxx", re.compile(r"^3..-....")),
]


def raw_material_pattern(product_id: str) -> str | None:
    """Return pattern name (e.g. '1xx-xxxx') if product_id is a raw material SKU, else None."""
    if not product_id:
        return None
    for name, pat in RAW_MATERIAL_PATTERNS:
        if pat.search(product_id):
            return name
    return None
