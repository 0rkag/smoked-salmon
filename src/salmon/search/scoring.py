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

if TYPE_CHECKING:
    from salmon.search.base import IdentData


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
    """
    weights = _get_weights(tag.is_va)
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
            weighted_score += weight * _fuzzy_artist(str(tag_val), str(result_val))
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


def _normalize(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", "", s)
    return " ".join(s.lower().split())


def strip_album_noise(s: str) -> str:
    s = re.sub(r"\(?[Ff]eat(\.|uring)? [^\)]+\)?", "", s)
    s = re.sub(
        r"\s*\(?(Remastered|Deluxe|Expanded|Anniversary|Limited|Special|Bonus|Collector'?s)\s*(Edition)?\)?",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip()


def _fuzzy_album(a: str, b: str) -> float:
    a = _normalize(strip_album_noise(a))
    b = _normalize(strip_album_noise(b))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return _token_similarity(a, b)


def _fuzzy_artist(a: str, b: str) -> float:
    a = _normalize(a)
    b = _normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    return overlap / max(len(a_tokens), len(b_tokens))


def _fuzzy_normalize(a: str, b: str) -> float:
    a = _normalize(a)
    b = _normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.8
    return _token_similarity(a, b)


def _token_similarity(a: str, b: str) -> float:
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    return overlap / max(len(a_tokens), len(b_tokens))


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
