"""Probabilistic noise pipeline for the benchmark harness.

Each noise function takes ``(TagData, random.Random)`` and returns either a
modified TagData or None if the noise is not applicable (e.g. transliterate
on a Latin-only title). Skipped noises don't count against the denominator.

Noises are applied deterministically per entry by seeding a fresh Random with
``seed + stable_hash(slug)``, so adding a new entry doesn't renumber the
random rolls for existing entries.
"""

from __future__ import annotations

import hashlib
import random
import re
import sys
import tomllib
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

from msgspec.structs import replace
from unidecode import unidecode

_BENCH_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BENCH_DIR.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from salmon.search.scoring import TagData  # noqa: E402

NoiseFn: TypeAlias = Callable[[TagData, random.Random], TagData | None]

# -----------------------------------------------------------------------
# Bucket 1 — field dropping
# -----------------------------------------------------------------------


def _drop_label(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.label is None:
        return None
    return replace(tag, label=None)


def _drop_catno(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.catno is None:
        return None
    return replace(tag, catno=None)


def _drop_year(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.year is None:
        return None
    return replace(tag, year=None)


def _drop_track_count(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.track_count is None:
        return None
    return replace(tag, track_count=None)


def _drop_source(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.source is None:
        return None
    return replace(tag, source=None)


# -----------------------------------------------------------------------
# Bucket 2 — numeric drift
# -----------------------------------------------------------------------


def _year_shift(tag: TagData, rng: random.Random, delta: int) -> TagData | None:
    if tag.year is None:
        return None
    try:
        base = int(str(tag.year)[:4])
    except (TypeError, ValueError):
        return None
    sign = rng.choice([-1, 1])
    return replace(tag, year=base + delta * sign)


def _year_off_by_1(tag: TagData, rng: random.Random) -> TagData | None:
    return _year_shift(tag, rng, 1)


def _year_off_by_5(tag: TagData, rng: random.Random) -> TagData | None:
    return _year_shift(tag, rng, 5)


def _year_off_by_10(tag: TagData, rng: random.Random) -> TagData | None:
    return _year_shift(tag, rng, 10)


def _track_count_off_by_1(tag: TagData, rng: random.Random) -> TagData | None:
    if tag.track_count is None:
        return None
    sign = rng.choice([-1, 1])
    new_count = max(1, tag.track_count + sign)
    return replace(tag, track_count=new_count)


# -----------------------------------------------------------------------
# Bucket 3 — character-level errors
# -----------------------------------------------------------------------


def _album_swap_adjacent_chars(tag: TagData, rng: random.Random) -> TagData | None:
    if tag.album is None or len(tag.album) < 3:
        return None
    pos = rng.randint(0, len(tag.album) - 2)
    chars = list(tag.album)
    chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]
    return replace(tag, album="".join(chars))


def _album_drop_char(tag: TagData, rng: random.Random) -> TagData | None:
    if tag.album is None or len(tag.album) < 4:
        return None
    pos = rng.randint(1, len(tag.album) - 2)
    return replace(tag, album=tag.album[:pos] + tag.album[pos + 1 :])


def _album_case_flip(tag: TagData, rng: random.Random) -> TagData | None:
    if not tag.album:
        return None
    mode = rng.choice(["upper", "lower"])
    new_album = tag.album.upper() if mode == "upper" else tag.album.lower()
    return replace(tag, album=new_album)


def _album_double_space(tag: TagData, rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    space_positions = [i for i, c in enumerate(tag.album) if c == " "]
    if not space_positions:
        return None
    pos = rng.choice(space_positions)
    return replace(tag, album=tag.album[:pos] + "  " + tag.album[pos + 1 :])


def _artist_trailing_whitespace(tag: TagData, _rng: random.Random) -> TagData | None:
    if not tag.artist:
        return None
    return replace(tag, artist=tag.artist + "   ")


# -----------------------------------------------------------------------
# Bucket 4 — unicode/encoding drift
# -----------------------------------------------------------------------


def _album_strip_accents(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    decomposed = unicodedata.normalize("NFKD", tag.album)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    if stripped == tag.album:
        return None
    return replace(tag, album=stripped)


def _album_transliterate(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    result = unidecode(tag.album)
    if result == tag.album or not result.strip():
        return None
    return replace(tag, album=result)


def _album_fullwidth_to_halfwidth(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    out: list[str] = []
    converted = False
    for ch in tag.album:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
            converted = True
        elif code == 0x3000:
            out.append(" ")
            converted = True
        else:
            out.append(ch)
    if not converted:
        return None
    return replace(tag, album="".join(out))


def _album_mojibake(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    try:
        result = tag.album.encode("utf-8").decode("latin-1")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if result == tag.album:
        return None
    return replace(tag, album=result)


# -----------------------------------------------------------------------
# Bucket 5 — semantic/formatting drift
# -----------------------------------------------------------------------


_EDITION_MARKERS = [
    "(Deluxe Edition)",
    "(Remastered)",
    "(Anniversary Edition)",
    "(Expanded)",
]

_EDITION_STRIP_RE = re.compile(
    r"\s*\(?(?:Remastered|Deluxe|Expanded|Anniversary|Limited|Special|Bonus|Collector'?s)"
    r"(?:\s+Edition)?\)?",
    re.IGNORECASE,
)

_VOLUME_SUBS: list[tuple[str, str]] = [
    (r"\bVolume\b", "Vol."),
    (r"\bVol\.", "Volume"),
    (r"\bVol\b", "Volume"),
    (r"\bPart\b", "Pt."),
    (r"\bPt\.", "Part"),
    (r"\bPt\b", "Part"),
    (r"\bNumber\b", "No."),
    (r"\bNo\.", "Number"),
]

_ROMAN_PAIRS: list[tuple[str, str]] = [
    ("VIII", "8"),
    ("VII", "7"),
    ("III", "3"),
    ("IX", "9"),
    ("IV", "4"),
    ("II", "2"),
    ("X", "10"),
    ("8", "VIII"),
    ("7", "VII"),
    ("3", "III"),
    ("9", "IX"),
    ("4", "IV"),
    ("2", "II"),
    ("10", "X"),
]


def _album_feat_drop(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    result = re.sub(r"\s*\([Ff]eat(?:\.|uring)?\s[^)]*\)", "", tag.album)
    result = re.sub(r"\s*[Ff]eat(?:\.|uring)?\s.*$", "", result)
    result = result.strip()
    if result == tag.album or not result:
        return None
    return replace(tag, album=result)


def _album_feat_add(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    if "feat" in tag.album.lower():
        return None
    return replace(tag, album=tag.album + " (feat. Synthetic Artist)")


def _album_edition_add(tag: TagData, rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    lowered = tag.album.lower()
    if any(m.lower() in lowered for m in _EDITION_MARKERS):
        return None
    marker = rng.choice(_EDITION_MARKERS)
    return replace(tag, album=tag.album + " " + marker)


def _album_edition_strip(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    result = _EDITION_STRIP_RE.sub("", tag.album).strip()
    if result == tag.album or not result:
        return None
    return replace(tag, album=result)


def _album_volume_abbrev_drift(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    for pattern, replacement in _VOLUME_SUBS:
        new, n = re.subn(pattern, replacement, tag.album, count=1, flags=re.IGNORECASE)
        if n > 0:
            return replace(tag, album=new)
    return None


def _album_roman_numeral_drift(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.album is None:
        return None
    for src, dst in _ROMAN_PAIRS:
        pattern = r"\b" + re.escape(src) + r"\b"
        new, n = re.subn(pattern, dst, tag.album, count=1)
        if n > 0:
            return replace(tag, album=new)
    return None


def _label_as_artist(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.artist is None or tag.label is None:
        return None
    return replace(tag, artist=tag.label, label=tag.artist)


def _catno_as_catno_no_label(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.label is None:
        return None
    return replace(tag, label=None)


def _swap_artist_album(tag: TagData, _rng: random.Random) -> TagData | None:
    if tag.artist is None or tag.album is None:
        return None
    return replace(tag, artist=tag.album, album=tag.artist)


# -----------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------

NOISE_FUNCTIONS: dict[str, NoiseFn] = {
    # Bucket 1 — field dropping
    "drop_label": _drop_label,
    "drop_catno": _drop_catno,
    "drop_year": _drop_year,
    "drop_track_count": _drop_track_count,
    "drop_source": _drop_source,
    # Bucket 2 — numeric drift
    "year_off_by_1": _year_off_by_1,
    "year_off_by_5": _year_off_by_5,
    "year_off_by_10": _year_off_by_10,
    "track_count_off_by_1": _track_count_off_by_1,
    # Bucket 3 — character-level errors
    "album_swap_adjacent_chars": _album_swap_adjacent_chars,
    "album_drop_char": _album_drop_char,
    "album_case_flip": _album_case_flip,
    "album_double_space": _album_double_space,
    "artist_trailing_whitespace": _artist_trailing_whitespace,
    # Bucket 4 — unicode/encoding drift
    "album_strip_accents": _album_strip_accents,
    "album_transliterate": _album_transliterate,
    "album_fullwidth_to_halfwidth": _album_fullwidth_to_halfwidth,
    "album_mojibake": _album_mojibake,
    # Bucket 5 — semantic/formatting drift
    "album_feat_drop": _album_feat_drop,
    "album_feat_add": _album_feat_add,
    "album_edition_add": _album_edition_add,
    "album_edition_strip": _album_edition_strip,
    "album_volume_abbrev_drift": _album_volume_abbrev_drift,
    "album_roman_numeral_drift": _album_roman_numeral_drift,
    "label_as_artist": _label_as_artist,
    "catno_as_catno_no_label": _catno_as_catno_no_label,
    "swap_artist_album": _swap_artist_album,
}


# -----------------------------------------------------------------------
# Config loading
# -----------------------------------------------------------------------


class NoiseConfig:
    """Parsed noise configuration.

    Attributes:
        seed: int seed for deterministic RNG
        noises: dict mapping noise name -> probability (0.0-1.0)
    """

    __slots__ = ("seed", "noises")

    def __init__(self, seed: int, noises: dict[str, float]):
        self.seed = int(seed)
        # Validate + sort by key for deterministic iteration
        for name, prob in noises.items():
            if name not in NOISE_FUNCTIONS:
                raise ValueError(
                    f"unknown noise '{name}' in profile; "
                    f"known: {sorted(NOISE_FUNCTIONS)}"
                )
            if not 0.0 <= float(prob) <= 1.0:
                raise ValueError(f"noise '{name}' probability out of [0,1]: {prob}")
        self.noises = {k: float(v) for k, v in sorted(noises.items())}

    @classmethod
    def from_toml(cls, path: Path) -> NoiseConfig:
        with path.open("rb") as f:
            data = tomllib.load(f)
        seed = data.get("seed", 42)
        noises = data.get("noises", {})
        return cls(seed=seed, noises=noises)

    def canonical_string(self) -> str:
        """Stable string form for cache-key hashing.

        Noises are already sorted by key (see __init__). Probabilities are
        formatted to 6 decimal places to avoid float-hashing drift across
        Python versions.
        """
        return ",".join(f"{k}={v:.6f}" for k, v in self.noises.items())

    def is_noop(self) -> bool:
        """True if this config applies no noise (all probabilities are 0)."""
        return not self.noises or all(p == 0.0 for p in self.noises.values())


# -----------------------------------------------------------------------
# Application
# -----------------------------------------------------------------------


def _slug_seed(base_seed: int, slug: str) -> int:
    """Derive a per-slug RNG seed. Uses sha256 to be stable across Py versions."""
    h = hashlib.sha256(f"{base_seed}|{slug}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def apply_noise(tag: TagData, slug: str, config: NoiseConfig) -> TagData:
    """Apply the configured noise to a TagData deterministically.

    The RNG is seeded per-slug so adding new entries doesn't shift
    the random decisions for existing entries.
    """
    if config.is_noop():
        return tag
    rng = random.Random(_slug_seed(config.seed, slug))
    for name, prob in config.noises.items():
        if rng.random() < prob:
            fn = NOISE_FUNCTIONS[name]
            new_tag = fn(tag, rng)
            if new_tag is not None:
                tag = new_tag
    return tag


# -----------------------------------------------------------------------
# Smoke tests
# -----------------------------------------------------------------------

if __name__ == "__main__":
    # 1. from_toml loads a valid file
    pristine_path = _BENCH_DIR / "noise-profiles" / "pristine.toml"
    if pristine_path.exists():
        cfg = NoiseConfig.from_toml(pristine_path)
        assert cfg.is_noop(), "pristine profile should be a no-op"
        print(f"OK: loaded {pristine_path.name} (seed={cfg.seed})")

    # 2. canonical_string is stable
    cfg_a = NoiseConfig(seed=42, noises={"drop_label": 0.5, "drop_catno": 0.25})
    cfg_b = NoiseConfig(seed=42, noises={"drop_catno": 0.25, "drop_label": 0.5})
    assert cfg_a.canonical_string() == cfg_b.canonical_string()
    print(f"OK: canonical_string stable: {cfg_a.canonical_string()}")

    # 3. is_noop returns input unchanged
    noop = NoiseConfig(seed=42, noises={})
    tag = TagData(artist="X", album="Y", year=2020, label="L", catno="C1")
    assert apply_noise(tag, "slug", noop) is tag
    print("OK: is_noop short-circuits")

    # 4. determinism with same seed
    cfg = NoiseConfig(seed=42, noises={"drop_label": 1.0, "year_off_by_1": 1.0})
    r1 = apply_noise(tag, "some-slug", cfg)
    r2 = apply_noise(tag, "some-slug", cfg)
    assert r1 == r2, f"not deterministic: {r1} vs {r2}"
    print(f"OK: deterministic, result={r1}")

    # 5. _drop_label with None label returns None
    bare = TagData(artist="X", album="Y")
    assert _drop_label(bare, random.Random(0)) is None
    print("OK: drop_label returns None when label absent")

    # 6. Each new noise function returns TagData or None on a sample tag
    sample = TagData(
        artist="Some Artist",
        album="Some Album (feat. Foo) Volume II",
        year=2020,
        track_count=10,
        source="CD",
        label="Some Label",
        catno="ABC-123",
    )
    new_fns = [
        _album_swap_adjacent_chars,
        _album_drop_char,
        _album_case_flip,
        _album_double_space,
        _artist_trailing_whitespace,
        _album_strip_accents,
        _album_transliterate,
        _album_fullwidth_to_halfwidth,
        _album_mojibake,
        _album_feat_drop,
        _album_feat_add,
        _album_edition_add,
        _album_edition_strip,
        _album_volume_abbrev_drift,
        _album_roman_numeral_drift,
        _label_as_artist,
        _catno_as_catno_no_label,
        _swap_artist_album,
    ]
    for fn in new_fns:
        out = fn(sample, random.Random(0))
        assert out is None or isinstance(out, TagData), f"{fn.__name__} bad return"
    print(f"OK: {len(new_fns)} new noise functions return TagData|None")

    # 7. Specific behavioral tests
    rng = random.Random(0)

    cafe = TagData(album="Café")
    out = _album_strip_accents(cafe, rng)
    assert out is not None and out.album == "Cafe", out
    print(f"OK: strip_accents Café -> {out.album}")

    jp = TagData(album="アンと私")
    out = _album_transliterate(jp, rng)
    assert out is not None and out.album.isascii() and out.album.strip(), out
    print(f"OK: transliterate アンと私 -> {out.album}")

    out = _album_mojibake(TagData(album="Café"), rng)
    assert out is not None and "Ã" in out.album, out
    print(f"OK: mojibake Café -> {out.album}")

    out = _album_mojibake(TagData(album="Hello"), rng)
    assert out is None, out
    print("OK: mojibake Hello -> None")

    out = _label_as_artist(TagData(artist="Various", label="Hostom"), rng)
    assert out is not None and out.artist == "Hostom" and out.label == "Various"
    print(f"OK: label_as_artist -> artist={out.artist} label={out.label}")

    # roman numeral drift — at least one of these should flip
    titles = ["Symphony No. II", "Volume III", "Chapter IV", "Part VII"]
    flipped = []
    for t in titles:
        r = _album_roman_numeral_drift(TagData(album=t), rng)
        if r is not None and r.album != t:
            flipped.append((t, r.album))
    assert flipped, f"none of {titles} produced a roman drift"
    print(f"OK: roman_numeral_drift flipped {len(flipped)}/{len(titles)} titles: {flipped[0]}")

    out = _album_edition_strip(TagData(album="Classic Album (Remastered)"), rng)
    assert out is not None and out.album == "Classic Album", out
    print(f"OK: edition_strip -> {out.album}")

    # 8. Determinism end-to-end with all new noises enabled
    cfg_all = NoiseConfig(
        seed=42,
        noises={name: 1.0 for name in NOISE_FUNCTIONS},
    )
    r1 = apply_noise(sample, "deterministic-slug", cfg_all)
    r2 = apply_noise(sample, "deterministic-slug", cfg_all)
    assert r1 == r2, f"determinism failed: {r1} vs {r2}"
    print(f"OK: end-to-end deterministic with all {len(NOISE_FUNCTIONS)} noises")

    print(f"\nAll smoke tests passed. {len(NOISE_FUNCTIONS)} noises registered.")
