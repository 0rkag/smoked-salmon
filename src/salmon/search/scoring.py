"""Weighted scoring for metadata search results.

Pure, dependency-free scoring used by `run_metasearch` to rank search
results against known tag metadata. See `score_result` for the scoring
philosophy and weight semantics.
"""
from __future__ import annotations

import re
import unicodedata
from enum import IntEnum
from typing import TYPE_CHECKING

import msgspec
from rapidfuzz import fuzz as _fuzz

if TYPE_CHECKING:
    from salmon.search.base import IdentData


SENTINEL_ARTISTS = frozenset({
    "",
    "unknown artist",
    "unknown",
    "various",
    "various artists",
    "v.a.",
    "va",
    "anonymous",
    "no artist",
})


def is_sentinel_artist(artist: str | None) -> bool:
    """Return True if `artist` is a placeholder that doesn't identify a
    specific artist (e.g. "Unknown Artist", "Various", "VA").

    Used by provider fallback chains to decide whether to emit structured
    queries that filter by artist. Sentinel values as a filter return zero
    results from most provider APIs because metadata sources don't index
    anonymous releases under a literal "Unknown Artist" string.
    """
    if not artist:
        return True
    return artist.strip().lower() in SENTINEL_ARTISTS


class FallbackLevel(IntEnum):
    """How closely a search result matched the structured query.

    Lower values = more structured match. Currently informational only;
    kept as a sort-tiebreaker hook for future use.
    """

    STRUCTURED = 0  # Full structured query matched directly
    PARTIAL_STRUCTURED = 1  # Dropped some structured params
    FREE_TEXT = 2  # Fell back to plain searchstr
    LOOSE = 3  # Free text with extra normalization (e.g. accent-stripped)


class TagData(msgspec.Struct, frozen=True):
    """Tag data to score search results against.

    Only populated fields participate in scoring. Unset fields don't affect
    the denominator (they're neutral). See `score_result` for the full
    scoring philosophy.
    """

    artist: str | None = None
    album: str | None = None
    year: int | str | None = None
    track_count: int | None = None
    source: str | None = None
    label: str | None = None
    catno: str | None = None
    is_va: bool = False


def score_result(result: IdentData, tag: TagData) -> float:
    """Score a search result against tag metadata.

    Returns a score from 0-100. Higher is better.

    Scoring philosophy:
        - Each field has a weight (see `_get_weights`). Weights sum to 100.
        - A field only contributes to the denominator if the tag side has a
          value for it. Fields the user didn't provide don't dilute the score.
        - BUT: if the tag side has a value and the result side is missing it,
          the weight is still counted — the result loses those points.
          This deliberately penalizes providers that return sparse metadata,
          on the principle that a provider returning `None` for a known-good
          label is less trustworthy than one that returns the matching label.
        - If no tag fields are populated at all, returns a neutral 50.0.
        - Cross-field credit: when the standard artist comparison would score
          very low (< 0.5), the result's artist field is checked against the
          tag's label to detect "label-as-artist" releases (common for
          anonymous techno/dub on small labels). If the result's artist
          fuzzy-matches the tag's label with score > 0.7, the artist field is
          credited at 60% of the label-match score instead of zero.
    """
    weights = _get_weights(tag.is_va)

    # Compute the artist match score with cross-field "label-as-artist" credit.
    # Some metadata sources put the label in the artist field for anonymous
    # releases (common in underground techno/dub). When the standard artist
    # comparison fails but the result's artist looks like the tag's label,
    # give partial credit instead of treating it as a hard mismatch.
    artist_match_score = 0.0
    if tag.artist and result.artist:
        artist_match_score = _fuzzy_artist(str(tag.artist), str(result.artist))
        if artist_match_score < 0.5 and tag.label:
            label_as_artist = _fuzzy_artist(str(tag.label), str(result.artist))
            if label_as_artist > 0.7:
                artist_match_score = max(artist_match_score, 0.6 * label_as_artist)

    total_weight = 0.0
    weighted_score = 0.0

    checks = [
        ("album", tag.album, result.album, weights["album"]),
        ("artist", tag.artist, result.artist, weights["artist"]),
        ("year", tag.year, result.year, weights["year"]),
        ("label", tag.label, result.label, weights["label"]),
        ("catno", tag.catno, result.catno, weights["catno"]),
        ("track_count", tag.track_count, result.track_count, weights["track_count"]),
        ("source", tag.source, result.source, weights["source"]),
    ]

    for field, tag_val, result_val, weight in checks:
        if tag_val is None or tag_val == "":
            continue
        total_weight += weight
        if result_val is None or result_val == "":
            continue
        if field == "album":
            weighted_score += weight * _fuzzy_album(str(tag_val), str(result_val))
        elif field == "artist":
            weighted_score += weight * artist_match_score
        elif field == "year":
            weighted_score += weight * _match_year(tag_val, result_val)
        elif field == "label":
            weighted_score += weight * _fuzzy_normalize(str(tag_val), str(result_val))
        elif field == "catno":
            weighted_score += weight * _match_catno(str(tag_val), str(result_val))
        elif field == "track_count":
            weighted_score += weight * _match_track_count(int(tag_val), result_val)
        elif field == "source":
            weighted_score += weight * (1.0 if str(tag_val).upper() == str(result_val).upper() else 0.0)

    if total_weight == 0:
        return 50.0

    score = (weighted_score / total_weight) * 100.0
    return round(score, 1)


def _get_weights(is_va: bool) -> dict[str, float]:
    if is_va:
        return {
            "album": 25,
            "artist": 5,
            "year": 10,
            "label": 15,
            "catno": 15,
            "track_count": 20,
            "source": 10,
        }
    return {
        "album": 25,
        "artist": 20,
        "year": 10,
        "label": 10,
        "catno": 10,
        "track_count": 15,
        "source": 10,
    }


def strip_album_noise(s: str) -> str:
    s = re.sub(r"\(?[Ff]eat(\.|uring)? [^\)]+\)?", "", s)
    s = re.sub(
        r"\s*\(?(Remastered|Deluxe|Expanded|Anniversary|Limited|Special|Bonus|Collector'?s)\s*(Edition)?\)?",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip()


# Roman numeral -> Arabic digit mapping for normalization. Covers the
# most common release-title cases (I-X). Intentionally limited to avoid
# false positives on real words like "II" (actually ambiguous -- but in
# album-title contexts, "II" is nearly always a number).
_ROMAN_NUMERALS = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}

# Common abbreviations in release titles. Pattern: match as a whole word
# (word-boundary), normalize to canonical form. These run BEFORE
# punctuation stripping so trailing dots in "Pt." / "Vol." are captured.
_ABBREVIATIONS = [
    (re.compile(r"\bpt\.?\b", re.IGNORECASE), "part"),
    (re.compile(r"\bvol\.?\b", re.IGNORECASE), "volume"),
    (re.compile(r"\bno\.?\b", re.IGNORECASE), "number"),
    (re.compile(r"\bft\.?\b", re.IGNORECASE), "featuring"),
    (re.compile(r"\bfeat\.?\b", re.IGNORECASE), "featuring"),
    (re.compile(r"\bep\b", re.IGNORECASE), ""),  # drop "EP" marker
    (re.compile(r"\s+&\s+"), " and "),  # explicit ampersand with spaces
    (re.compile(r"&"), " and "),  # any other ampersand
]


def _normalize_abbreviations(s: str) -> str:
    """Expand common abbreviations so fuzzy matching sees canonical forms.

    "Pt. 2" -> "part 2"
    "Vol. 1" -> "volume 1"
    "Jay-Z & Kanye" -> "Jay-Z and Kanye"
    """
    for pattern, replacement in _ABBREVIATIONS:
        s = pattern.sub(replacement, s)
    return s


_ROMAN_PATTERN = re.compile(
    r"\b(?:" + "|".join(sorted(_ROMAN_NUMERALS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _normalize_romans(s: str) -> str:
    """Replace standalone roman numerals I-X with their Arabic equivalents.

    Only matches word-boundary tokens to avoid mangling real words like
    "in", "it", "vim" that happen to contain roman numeral characters.
    """
    def _replace(match: re.Match[str]) -> str:
        return _ROMAN_NUMERALS[match.group(0).lower()]
    return _ROMAN_PATTERN.sub(_replace, s)


def _normalize(s: str) -> str:
    """Lowercase, strip diacritics, expand abbreviations, normalize romans."""
    if not s:
        return ""
    # Strip diacritics first so downstream regexes see ASCII.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Expand abbreviations BEFORE stripping punctuation so "Pt." and
    # "Vol." patterns can still match their trailing dots, and so "&"
    # is still present when the ampersand rules run.
    s = _normalize_abbreviations(s)
    s = _normalize_romans(s)
    s = s.lower()
    # Strip remaining punctuation (keep word chars + whitespace).
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fuzzy_album(a: str, b: str) -> float:
    """Compute album title similarity 0.0-1.0.

    Uses rapidfuzz token_set_ratio after strip_album_noise + _normalize.
    token_set_ratio handles stopwords ("The Wall" vs "Wall"), word
    reordering, and is insensitive to duplicate tokens.
    """
    a_n = _normalize(strip_album_noise(a))
    b_n = _normalize(strip_album_noise(b))
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    return _fuzz.token_set_ratio(a_n, b_n) / 100.0


def _fuzzy_artist(a: str, b: str) -> float:
    """Compute artist name similarity 0.0-1.0.

    Uses rapidfuzz WRatio which combines token_sort_ratio,
    partial_ratio, and token_set_ratio -- designed for short,
    reorder-tolerant identifier strings.
    """
    a_n = _normalize(a)
    b_n = _normalize(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    return _fuzz.WRatio(a_n, b_n) / 100.0


def _fuzzy_normalize(a: str, b: str) -> float:
    """Generic fuzzy match for labels / catalogue numbers / etc.

    Uses partial_ratio to handle substring cases like "Sub Pop" vs
    "Sub Pop Records".
    """
    a_n = _normalize(a)
    b_n = _normalize(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    return _fuzz.partial_ratio(a_n, b_n) / 100.0


def _match_year(a, b) -> float:
    try:
        ya, yb = int(str(a)[:4]), int(str(b)[:4])
    except (ValueError, TypeError):
        return 0.0
    if ya == yb:
        return 1.0
    if abs(ya - yb) == 1:
        return 0.5
    return 0.0


def _match_catno(a: str, b: str) -> float:
    a = re.sub(r"[\s\-]", "", a).upper()
    b = re.sub(r"[\s\-]", "", b).upper()
    return 1.0 if a == b else 0.0


def _match_track_count(a: int, b: int | None) -> float:
    if b is None:
        return 0.0
    if a == b:
        return 1.0
    if abs(a - b) == 1:
        return 0.5
    return 0.0
