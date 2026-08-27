"""
Hash prioritizer.

Parses RA hash names like:
  "Sonic The Hedgehog (USA, Europe) (Rev 2).md"
  "Final Fantasy (Japan) (En translation v1.1) [rapatches].md"

And assigns a priority_score so the preferred hash can be selected
deterministically. Lower score = higher preference.

Priority order:
  Region:       USA > World > Europe > Australia > Japan > Korea > Brazil > China > Other
  Revision:     Higher revision wins
  Translation:  English translation > no translation; higher version wins
                NON-English translation (Ru/Fr/Pt/…) ranks LAST — below every
                native release — so it only wins when it is a game's only hash.
  Label:        nointro preferred over rapatches

Translation language matters. RA tracks fan translations as ordinary hashes of
a game, and their names carry the *base* region, e.g.
  "1943 - The Battle of Midway (USA) (Ru) (v0.1.2) (Artemon).nes"
Reading only the "(USA)" makes a Russian translation look like the US release
(same score), which let it (a) tie/beat the genuine USA ROM in dedup and (b)
be auto-applied by apply_patches as if it were the wanted game. We therefore
detect the *translation language* and rank non-English translations last.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field


# ── Region ranking ─────────────────────────────────────────────────────────────
# USA and World are a deliberately tight pair (gap 1): the MSU-1 penalty
# (MSU_PENALTY_RANK below) is larger than that gap but smaller than every
# other region gap (10), so both plain USA and plain World rank above an
# MSU-1 USA, while an MSU-1 of any region still ranks below that same region's
# plain release and above the next region down. See _compute_score.
REGION_RANK: dict[str, int] = {
    "USA":       0,
    "World":     1,
    "Europe":    11,
    "Australia": 21,
    "Japan":     41,
    "Korea":     51,
    "Brazil":    61,
    "China":     71,
}
DEFAULT_REGION_RANK = 81  # anything not in the list

# Non-English fan translations rank below EVERY native release (Other = 81) and
# below the English-translation tier (31) and MSU. They stay selectable as a
# last resort (select_preferred still picks them when they are a game's only
# hash) but never win a dedup against a native or English-translated copy.
NONENGLISH_TRANSLATION_RANK = 99

# Tokens in filenames that map to regions
REGION_ALIASES: dict[str, str] = {
    "USA":         "USA",
    "US":          "USA",
    "World":       "World",
    "Europe":      "Europe",
    "EUR":         "Europe",
    "EU":          "Europe",
    "Australia":   "Australia",
    "AUS":         "Australia",
    "Japan":       "Japan",
    "JPN":         "Japan",
    "JP":          "Japan",
    "Korea":       "Korea",
    "KOR":         "Korea",
    "Brazil":      "Brazil",
    "BRA":         "Brazil",
    "China":       "China",
    "CHN":         "China",
    "UK":          "Europe",   # United Kingdom — English release, NOT Ukrainian
}

# Lowercased region tokens — these are REGIONS, never languages.
_REGION_ALIAS_LC = {k.lower() for k in REGION_ALIASES}

# Language tokens used by No-Intro / RAPatches to tag a TRANSLATION. A bare
# language code in parentheses is the fan-translation signature, e.g. "(Ru)".
_ENGLISH_LANG = {"en", "english"}

# Non-English language codes (ISO-639-1) and words. Region-colliding codes
# ("jp", "eu") are deliberately excluded — they are handled as regions above.
_NONENGLISH_LANG = {
    # codes
    "fr", "de", "es", "it", "nl", "pt", "sv", "no", "da", "fi", "pl", "ru",
    "cs", "sk", "hu", "el", "tr", "ar", "he", "gl", "hr", "sr",
    "sl", "ro", "bg", "lt", "lv", "et", "ga", "fa", "th", "vi",
    "zh", "ko", "ja",
    # non-standard codes seen in RAPatches/No-Intro translation names
    # (NOTE: "uk"=United Kingdom region, not Ukrainian; "ca"=Canada, "id"/"is"/
    # "ms" collide with English words/regions — all deliberately excluded.)
    "du", "tl", "eo", "tu", "la",
    # words
    "french", "german", "spanish", "italian", "dutch", "portuguese",
    "swedish", "norwegian", "danish", "finnish", "polish", "russian",
    "ukrainian", "czech", "slovak", "hungarian", "greek", "turkish",
    "arabic", "hebrew", "catalan", "galician", "croatian", "serbian",
    "slovenian", "romanian", "bulgarian", "lithuanian", "latvian",
    "estonian", "icelandic", "irish", "persian", "thai", "vietnamese",
    "indonesian", "malay", "chinese", "korean", "japanese",
}


@dataclass
class ParsedHash:
    md5: str
    raw_name: str
    label: str                  # "nointro" | "rapatches" | ""
    patch_url: str | None

    regions: list[str] = field(default_factory=list)
    revision: int = 0
    is_translation: bool = False
    is_english_translation: bool = False   # English (or English-inclusive) translation
    translation_lang: str = ""             # "en", "ru", "fr", … or "" if unknown
    translation_version: str = ""   # e.g. "1.1"
    is_msu: bool = False            # MSU-1 audio enhancement (same RA game, audio-only)
    priority_score: int = 0


def parse_and_score(md5: str, name: str, labels: list[str], patch_url: str | None) -> ParsedHash:
    label = labels[0] if labels else ""
    parsed = ParsedHash(md5=md5, raw_name=name, label=label, patch_url=patch_url)

    _extract_regions(name, parsed)
    _extract_revision(name, parsed)
    _extract_translation(name, parsed)
    _extract_msu(name, parsed)
    parsed.priority_score = _compute_score(parsed)

    return parsed


# ── Extraction helpers ─────────────────────────────────────────────────────────

def _extract_regions(name: str, parsed: ParsedHash):
    """Pull all region tokens from parenthesised groups."""
    # Find all (...) groups
    groups = re.findall(r'\(([^)]+)\)', name)
    found = []
    for group in groups:
        tokens = [t.strip() for t in group.split(',')]
        for token in tokens:
            canonical = REGION_ALIASES.get(token)
            if canonical and canonical not in found:
                found.append(canonical)
    parsed.regions = found if found else ["Other"]


def _extract_revision(name: str, parsed: ParsedHash):
    """Extract (Rev N) or (v1.1) style revision markers."""
    rev_match = re.search(r'\(Rev\s+(\d+)\)', name, re.IGNORECASE)
    if rev_match:
        parsed.revision = int(rev_match.group(1))
        return
    # Some sets use (Rev A), (Rev B) — treat as 1, 2, ...
    rev_alpha = re.search(r'\(Rev\s+([A-Z])\)', name, re.IGNORECASE)
    if rev_alpha:
        parsed.revision = ord(rev_alpha.group(1).upper()) - ord('A') + 1


def _extract_translation(name: str, parsed: ParsedHash):
    """Detect translation patches, their LANGUAGE, and their version.

    A non-English fan translation is the failure mode this guards against: its
    name carries the base region (e.g. "(USA) (Ru)"), so reading only the
    region made it indistinguishable from the native release. We classify the
    translation language so non-English translations can be ranked last.

    Official multi-language releases ("(Europe) (En,Fr,De)") are NOT
    translations and must not be flagged — they carry several language codes,
    no patch, and no translation tag, so none of the signals below fire.
    """
    # Collect language tokens from parenthesised groups (skipping regions).
    eng_tokens: list[str] = []
    noneng_tokens: list[str] = []
    for group in re.findall(r'\(([^)]+)\)', name):
        for raw in group.split(','):
            low = raw.strip().lower()
            if not low or low in _REGION_ALIAS_LC:
                continue
            if low in _ENGLISH_LANG:
                eng_tokens.append(low)
            elif low in _NONENGLISH_LANG:
                noneng_tokens.append(low)

    # No-Intro translation tag carries the language, e.g. "(T-Eng)", "[T+Rus]".
    t_tag = re.search(r'[\(\[]\s*T[-+]\s*([A-Za-z]{2,})', name)
    if t_tag:
        tlang = t_tag.group(1).lower()
        (eng_tokens if tlang.startswith("en") else noneng_tokens).append(tlang)

    has_lang   = bool(eng_tokens or noneng_tokens)
    total_lang = len(eng_tokens) + len(noneng_tokens)

    classic_english = bool(re.search(r'\(En(glish)?\s*(translation|patch)?\)', name, re.IGNORECASE))
    has_word        = bool(re.search(r'translation', name, re.IGNORECASE))
    has_t_tag       = bool(t_tag)
    # A RAPatches patch (patch_url set, or a "rapatches" label) that also names
    # a language is a translation. A patch with NO language token is a hack —
    # left alone here so hacks keep their normal scoring and auto-patching.
    patch_signal = (parsed.patch_url is not None) or ("rapatches" in (parsed.label or ""))
    # A single lone non-English code (no English, no other languages) is the
    # bare fan-translation signature even without a patch_url — catches local
    # filenames like "1943 (USA) (Ru) (Artemon).nes". Multiple codes ⇒ official
    # multi-language release, so this stays narrow.
    lone_nonenglish = (total_lang == 1 and len(noneng_tokens) == 1)

    is_trans = (
        classic_english or has_word or has_t_tag
        or (patch_signal and has_lang)
        or lone_nonenglish
    )
    if not is_trans:
        return

    parsed.is_translation = True
    if eng_tokens or not noneng_tokens:
        # English, English-inclusive multi-language, or an unknown-language
        # translation ("(Translation)" with no code) → treat as English-
        # equivalent: the desirable, playability-restoring case.
        parsed.is_english_translation = True
        parsed.translation_lang = "en"
    else:
        parsed.is_english_translation = False
        parsed.translation_lang = noneng_tokens[0]

    # Look for version: v1.1, v2, 1.02, etc.
    ver_match = re.search(r'[vV](\d+(?:\.\d+)?)', name)
    if ver_match:
        parsed.translation_version = ver_match.group(1)


def _extract_msu(name: str, parsed: ParsedHash):
    """Detect MSU-1 audio-enhancement ROMs (e.g. "(MSU-1)", "MSU1", "(MSU)").

    MSU-1 only swaps a game's audio for streamed PCM, so RA lists the MSU-1
    ROM as a supported hash of the *same* game as the plain release. When both
    are present we prefer the plain release, so MSU-1 carries a penalty.
    """
    if re.search(r'\bMSU(?:[\s-]?1)?\b', name, re.IGNORECASE):
        parsed.is_msu = True


# ── Score computation ──────────────────────────────────────────────────────────

def _version_as_int(version: str) -> int:
    """Convert "1.1" → 110, "2" → 200, "" → 0. Higher = better."""
    if not version:
        return 0
    parts = version.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        return major * 100 + minor
    except ValueError:
        return 0


def _compute_score(parsed: ParsedHash) -> int:
    score = 0

    # Region — pick the best (lowest rank) region listed
    best_region_rank = min(
        REGION_RANK.get(r, DEFAULT_REGION_RANK)
        for r in (parsed.regions or ["Other"])
    )
    score += best_region_rank * 10_000

    # Translation / RAPatches builds — English translations and patched
    # builds of non-English originals sit at a fixed tier between Australia
    # (210k) and Japan (410k): they beat non-English native releases
    # (Japan/Korea/etc) but lose to USA/World/Europe/Australia native
    # releases. Placed IN LIEU of the base region (usually Japan), not added
    # on top — so a translated/hacked ROM ranks above its plain Japanese
    # original and above the MSU-1 build of the same game.
    if parsed.is_translation and parsed.is_english_translation:
        score = min(score, 31 * 10_000)
        # Higher translation version is better (subtract)
        score -= _version_as_int(parsed.translation_version) * 10
    elif parsed.is_translation:
        # Non-English fan translation — rank LAST, regardless of the base
        # region named in the hash (the "(USA)" in "(USA) (Ru)" is the patch
        # source, not a US release). Overwrite (not min) so the region credit
        # can't keep it near the top. Still selectable as a game's only hash.
        score = NONENGLISH_TRANSLATION_RANK * 10_000
        score -= _version_as_int(parsed.translation_version) * 10

    # MSU-1 — audio-only enhancement; ranks one notch below the equivalent
    # non-MSU version of the same game (plain, translated, or hack). Applied
    # last so it always sits just under its non-MSU sibling. The penalty
    # (5 ranks) exceeds the USA->World gap (1) but is smaller than every other
    # region gap (10): USA, World, MSU-1 USA, MSU-1 World, Europe, Japan, ...
    if parsed.is_msu:
        score += 5 * 10_000

    # Revision — higher is better (subtract so higher rev = lower score)
    score -= parsed.revision * 100

    # Label — rapatches slightly below nointro within same tier
    if parsed.label == "rapatches":
        score += 1

    # Date-style revision/version strings (e.g. "v2020.05.73") explode into
    # huge ints that overflow the ES integer mapping — clamp to int32.
    return max(-2_000_000_000, min(2_000_000_000, score))


def select_preferred(hashes: list[ParsedHash]) -> ParsedHash | None:
    if not hashes:
        return None
    return min(hashes, key=lambda h: h.priority_score)


def select_patch_hash(hashes: list[ParsedHash]) -> ParsedHash | None:
    """Choose which patch to AUTO-APPLY for a missing game.

    Only hacks and English (or English-inclusive) translations are eligible —
    a non-English translation must never be fabricated into the collection in
    place of a game's native/English release. Among the eligible patch-bearing
    hashes, pick the best (lowest priority_score). Returns None when the only
    patches available are non-English translations — the caller should then
    skip the game rather than manufacture an unwanted ROM.
    """
    candidates = [
        h for h in hashes
        if h.patch_url and not (h.is_translation and not h.is_english_translation)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda h: h.priority_score)
