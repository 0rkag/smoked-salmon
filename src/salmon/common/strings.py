import re
import unicodedata

from salmon.common.regexes import re_strip
from salmon.constants import GENRE_LIST
from salmon.errors import GenreNotInWhitelist

# Roman numeral -> Arabic digit mapping for normalization. Covers the
# most common release-title cases (I-X). Intentionally limited to avoid
# false positives on real words like "II" (actually ambiguous -- but in
# album-title contexts, "II" is nearly always a number).
ROMAN_NUMERALS = {
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
ABBREVIATIONS = [
    (re.compile(r"\bpt\.?\b", re.IGNORECASE), "part"),
    (re.compile(r"\bvol\.?\b", re.IGNORECASE), "volume"),
    (re.compile(r"\bno\.?\b", re.IGNORECASE), "number"),
    (re.compile(r"\bft\.?\b", re.IGNORECASE), "featuring"),
    (re.compile(r"\bfeat\.?\b", re.IGNORECASE), "featuring"),
    (re.compile(r"\bep\b", re.IGNORECASE), ""),  # drop "EP" marker
    (re.compile(r"\s+&\s+"), " and "),  # explicit ampersand with spaces
    (re.compile(r"&"), " and "),  # any other ampersand
]

# Only match multi-char romans (II-X). Single-char "I"/"V"/"X" are far
# more often parts of real titles or abbreviations ("V.A." for various
# artists, the pronoun "I", placeholder "X") than actual roman numerals.
ROMAN_PATTERN = re.compile(
    r"\b(?:" + "|".join(sorted((r for r in ROMAN_NUMERALS if len(r) >= 2), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Stopwords stripped by `strip_stopwords` so "The Wall" and "Wall" compare
# as identical. Kept deliberately small — aggressive stopword lists cause
# real titles like "A Love Supreme" to lose load-bearing content.
STOPWORDS = frozenset({"the", "a", "an"})


def normalize_abbreviations(s: str) -> str:
    """Expand common abbreviations so fuzzy matching sees canonical forms.

    "Pt. 2" -> "part 2"
    "Vol. 1" -> "volume 1"
    "Jay-Z & Kanye" -> "Jay-Z and Kanye"
    """
    for pattern, replacement in ABBREVIATIONS:
        s = pattern.sub(replacement, s)
    return s


def normalize_romans(s: str) -> str:
    """Replace standalone roman numerals I-X with their Arabic equivalents.

    Only matches word-boundary tokens to avoid mangling real words like
    "in", "it", "vim" that happen to contain roman numeral characters.
    """
    def _replace(match: re.Match[str]) -> str:
        return ROMAN_NUMERALS[match.group(0).lower()]
    return ROMAN_PATTERN.sub(_replace, s)


def strip_stopwords(s: str) -> str:
    """Drop leading articles from a lowercased, tokenized string."""
    tokens = [t for t in s.split() if t not in STOPWORDS]
    return " ".join(tokens) if tokens else s  # preserve the original if all tokens stripped


def normalize_searchstr(s: str) -> str:
    """Normalize a free-text search string for provider search APIs.

    Applies the same lightweight transformations as the scoring fuzzy
    matcher — abbreviation expansion, roman numeral normalization, stopword
    removal, and whitespace collapsing — so that query drift ("Pt. 2" vs
    "Part II", "The Wall" vs "Wall", double spaces from tag typos) doesn't
    poison free-text provider queries.

    Intentionally LIGHTER than scoring._normalize: keeps punctuation like
    hyphens (needed for "Jay-Z" type names) and doesn't NFKD-normalize
    (Japanese/Chinese titles should survive as-is — providers handle
    unicode themselves).
    """
    if not s:
        return ""
    # Expand abbreviations and roman numerals BEFORE casefold so word-
    # boundary regexes work on the original casing.
    s = normalize_abbreviations(s)
    s = normalize_romans(s)
    # Lowercase for stopword matching.
    s = s.lower()
    # Collapse whitespace (this fixes the double-space typo case).
    s = re.sub(r"\s+", " ", s).strip()
    # Strip leading articles.
    s = strip_stopwords(s)
    return s


def make_searchstrs(artists, album, normalize=False) -> list[str]:
    """Generate search strings from artists and album name.

    Args:
        artists: List of (artist_name, importance) tuples.
        album: Album name.
        normalize: Whether to normalize accents.

    Returns:
        List of search strings.
    """
    main_artists = [a for a, i in artists if i == "main"]
    album = album or ""
    album = re.sub(r" ?(- )? (EP|Single)", "", album)
    album = re.sub(r"\(?[Ff]eat(\.|uring)? [^\)]+\)?", "", album)

    search: str | list[str]
    if len(main_artists) > 3 or (main_artists and any("Various" in a for a in main_artists)) or len(main_artists) == 0:
        search = re_strip(album, filter_nonscrape=False)
    elif len(main_artists) == 1:
        search = re_strip(main_artists[0], album, filter_nonscrape=False)
    else:
        # 2 or 3 main artists
        search = [re_strip(art, album, filter_nonscrape=False) for art in main_artists]

    if normalize:
        normalized = normalize_accents(search) if isinstance(search, str) else normalize_accents(*search)
        search = normalized if normalized else search

    # Always apply searchstr normalization (abbreviations, romans, stopwords,
    # whitespace collapse) so free-text provider queries are stable against
    # cosmetic tag drift.
    if isinstance(search, str):
        return [normalize_searchstr(search)]
    return [normalize_searchstr(s) for s in search]


def normalize_accents(*strs: str) -> str | list[str]:
    """Normalize accents in strings using NFKD form.

    Args:
        *strs: Variable number of strings to normalize.

    Returns:
        Single normalized string if one input, list if multiple, empty string if none.
    """
    normalized = ["".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)) for s in strs]
    if not normalized:
        return ""
    return normalized if len(normalized) > 1 else normalized[0]


def less_uppers(one, two):
    """Return the string with less uppercase letters."""
    one_count = sum(1 for c in one if c.islower())
    two_count = sum(1 for c in two if c.islower())
    return one if one_count >= two_count else two


def strip_template_keys(template, key):
    """Strip all unused brackets from the folder name."""
    folder = re.sub(r" *[\[{\(]*{" + key + r"}[\]}\)]* *", " ", template).strip()
    return re.sub(r" *- *$", "", folder)


def fetch_genre(genre: str) -> set[str]:
    """Fetch standardized genre from whitelist.

    Args:
        genre: The genre string to look up.

    Returns:
        Set of standardized genre strings.

    Raises:
        GenreNotInWhitelist: If genre is not in whitelist.
    """
    normalized = normalize_accents(genre)
    if isinstance(normalized, list):
        normalized = normalized[0] if normalized else ""
    key_search = re.sub(r"[^a-z]", "", normalized.lower().replace("&", "and"))
    try:
        return GENRE_LIST[key_search]
    except KeyError:
        raise GenreNotInWhitelist from None


def truncate(string, length):
    if len(string) < length:
        return string
    return f"{string[: length - 3]}..."
