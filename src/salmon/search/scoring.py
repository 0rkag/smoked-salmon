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

from salmon.common.strings import (
    normalize_abbreviations,
    normalize_romans,
    strip_stopwords,
)

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

    Lower values = more structured match. Used by
    `_score_and_filter_results` as a sort tiebreaker: when two results
    tie on score, the one with the lower `FallbackLevel` ranks first
    (structured matches beat free-text matches on equal scores).
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


def _normalize(s: str) -> str:
    """Lowercase, strip diacritics, expand abbreviations, normalize romans,
    and drop stopwords."""
    if not s:
        return ""
    # Strip diacritics first so downstream regexes see ASCII.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Expand abbreviations BEFORE stripping punctuation so "Pt." and
    # "Vol." patterns can still match their trailing dots, and so "&"
    # is still present when the ampersand rules run.
    s = normalize_abbreviations(s)
    s = normalize_romans(s)
    s = s.lower()
    # Strip remaining punctuation (keep word chars + whitespace).
    s = re.sub(r"[^\w\s]", " ", s)
    # Collapse whitespace.
    s = re.sub(r"\s+", " ", s).strip()
    # Drop stopwords (last, so they don't interfere with earlier patterns).
    s = strip_stopwords(s)
    return s


def _fuzzy_album(a: str, b: str) -> float:
    """Compute album title similarity 0.0-1.0.

    Uses rapidfuzz token_sort_ratio after strip_album_noise + _normalize.
    token_sort_ratio is reorder-tolerant but — unlike token_set_ratio —
    does NOT treat a token-set subset as a perfect match. That distinction
    matters: "Chronic" should not be a 100% match for "Chronic Girl".
    Stopwords ("the", "a", "an") are dropped in `_normalize`, so
    "The Wall" and "Wall" still collapse to the same normalized form.
    """
    a_n = _normalize(strip_album_noise(a))
    b_n = _normalize(strip_album_noise(b))
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    return _fuzz.token_sort_ratio(a_n, b_n) / 100.0


def _fuzzy_artist(a: str, b: str) -> float:
    """Compute artist name similarity 0.0-1.0.

    Uses rapidfuzz token_sort_ratio after stopword-aware `_normalize`.
    Chose token_sort_ratio over WRatio because WRatio blends in
    token_set_ratio which treats "Various" as a perfect match for
    "Various Artists" (correct for that case) but also treats "Ivan"
    as a perfect match for "Ivan Gafer" (wrong — we want a penalty).
    """
    a_n = _normalize(a)
    b_n = _normalize(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    return _fuzz.token_sort_ratio(a_n, b_n) / 100.0


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
