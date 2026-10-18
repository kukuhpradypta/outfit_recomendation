"""Parse user-entered money strings into float rupiah values.

Supports Indonesian formatting where "." is a thousands separator and ","
is the decimal separator, as well as plain numbers.

Examples:
    "100.000"      -> 100000.0
    "Rp100.000"    -> 100000.0
    "1.500.000"    -> 1500000.0
    "100000"       -> 100000.0
    "100000,50"    -> 100000.5
    "100,5"        -> 100.5
    100000 (num)   -> 100000.0
"""
import re


class MoneyParseError(ValueError):
    pass


def parse_rupiah(value: str | int | float) -> float:
    """Convert a money input to a float. Raises MoneyParseError if invalid."""
    if isinstance(value, (int, float)):
        return float(value)

    if value is None:
        raise MoneyParseError("Nilai harga/budget kosong.")

    s = str(value).strip()
    if not s:
        raise MoneyParseError("Nilai harga/budget kosong.")

    # Remove currency prefix/symbols and spaces.
    s = re.sub(r"(?i)\brp\b", "", s)
    s = s.replace(" ", "").replace("\u00a0", "")

    # Keep only digits, dot, comma, and leading minus.
    cleaned = re.sub(r"[^0-9.,-]", "", s)
    if cleaned in ("", "-", ".", ","):
        raise MoneyParseError(f"Format harga/budget tidak valid: '{value}'")

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        # Both present: the last separator is the decimal, the other is thousands.
        if cleaned.rfind(",") > cleaned.rfind("."):
            # Indonesian style: "1.500.000,50"
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # US style: "1,500,000.50"
            cleaned = cleaned.replace(",", "")
    elif has_comma:
        # Comma only -> decimal separator.
        cleaned = cleaned.replace(",", ".")
    elif has_dot:
        # Dot only: decide thousands vs decimal.
        parts = cleaned.split(".")
        # Treat as thousands separator when every group after the first has
        # exactly 3 digits (e.g. "100.000", "1.500.000"). Otherwise decimal.
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]) and len(parts[0]) >= 1:
            cleaned = cleaned.replace(".", "")
        # else: leave as a normal decimal like "100.5"

    try:
        result = float(cleaned)
    except ValueError:
        raise MoneyParseError(f"Format harga/budget tidak valid: '{value}'")

    if result < 0:
        raise MoneyParseError("Harga/budget tidak boleh negatif.")
    return result
