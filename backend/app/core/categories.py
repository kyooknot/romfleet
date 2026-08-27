"""Canonical game-type (category) labels.

Different sources label the same concept differently — RA tags a patched ROM `~Hack~`
("Hack") while LaunchBox's Game Type calls it "ROM Hack". Collapse those synonyms to one
canonical bucket so the type filter and per-category completeness rules stay consistent.
Unknown values pass through unchanged (only known synonyms are folded).
"""
from __future__ import annotations

# lowercased source value -> canonical label
_CANON = {
    "hack": "Hack", "rom hack": "Hack", "romhack": "Hack", "rom-hack": "Hack",
    "homebrew": "Homebrew",
    "unlicensed": "Unlicensed", "unl": "Unlicensed", "aftermarket": "Unlicensed",
    "prototype": "Prototype", "proto": "Prototype",
    "demo": "Demo",
}


def normalize_category(cat: str | None) -> str | None:
    if not cat:
        return cat
    s = str(cat).strip()
    return _CANON.get(s.lower(), s)
