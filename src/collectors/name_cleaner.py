"""Strip Ukrainian legal-entity boilerplate from company names.

Used before generating search queries for ProZorro companies — the verbose
legal prefixes drown out actual brand tokens and tank DDG hit rate. Handles
both Cyrillic and Latin transliterations, quote-mark variants, soft hyphens,
and common organizational descriptors like "design bureau".
"""
import re
import unicodedata

# Quote-mark variants we strip from the outside of cleaned names.
_QUOTES = "«»\"“”‘’«»`'"

# Soft hyphen and zero-width chars that show up inside scraped Cyrillic text.
_INVISIBLES = "­​‌‍﻿"

# Legal-form prefixes — order matters: longest first so "tovarystvo z..." is
# stripped before a short "tov" match could fire on the same name.
# Each entry is matched case-insensitively as a leading token sequence with
# optional surrounding quotes/punctuation.
_LEGAL_FORMS_LATIN = [
    # Limited liability
    r"tovarystvo\s+z\s+obmezhenoyu\s+vidpovidalnistyu",
    r"tovarystvo\s+z\s+dodatkovoyu\s+vidpovidalnistyu",
    # Joint stock
    r"publichne\s+aktsionerne\s+tovarystvo",
    r"pryvatne\s+aktsionerne\s+tovarystvo",
    r"vidkryte\s+aktsionerne\s+tovarystvo",
    r"zakryte\s+aktsionerne\s+tovarystvo",
    r"aktsionerne\s+tovarystvo",
    # Subsidiary / state
    r"dochirnye\s+pidpryyemstvo",
    r"dochirne\s+pidpryyemstvo",
    r"derzhavne\s+pidpryyemstvo",
    r"komunalne\s+pidpryyemstvo",
    r"naukovo[-\s]vyrobnyche\s+obyednannya",
    r"naukovo[-\s]vyrobnyche\s+pidpryyemstvo",
    r"vyrobnycho[-\s]tekhnichne\s+pidpryyemstvo",
    r"vyrobnyche\s+pidpryyemstvo",
    # Sole proprietor
    r"fizychna\s+osoba[-\s]pidpryyemets",
    r"fizychna\s+osoba\s+pidpryyemets",
    # Common descriptor noise
    r"konstruktorske\s+byuro",
    r"naukovo[-\s]doslidnyy\s+instytut",
    # Acronyms — must come AFTER long forms to avoid premature match.
    r"tov", r"pat", r"prat", r"vat", r"zat", r"at",
    r"dp", r"kp", r"nvo", r"nvp", r"vtp", r"nvk",
    r"fop", r"pp", r"sp",
    r"kb", r"ndi",
]

_LEGAL_FORMS_CYRILLIC = [
    r"товариство\s+з\s+обмеженою\s+відповідальністю",
    r"товариство\s+з\s+додатковою\s+відповідальністю",
    r"публічне\s+акціонерне\s+товариство",
    r"приватне\s+акціонерне\s+товариство",
    r"відкрите\s+акціонерне\s+товариство",
    r"закрите\s+акціонерне\s+товариство",
    r"акціонерне\s+товариство",
    r"дочірнє\s+підприємство",
    r"державне\s+підприємство",
    r"комунальне\s+підприємство",
    r"науково[-\s]виробниче\s+об['’']єднання",
    r"науково[-\s]виробниче\s+підприємство",
    r"виробничо[-\s]технічне\s+підприємство",
    r"виробниче\s+підприємство",
    r"фізична\s+особа[-\s]підприємець",
    r"фізична\s+особа\s+підприємець",
    r"конструкторське\s+бюро",
    r"науково[-\s]дослідний\s+інститут",
    r"тов", r"пат", r"прат", r"ват", r"зат", r"ат",
    r"дп", r"кп", r"нво", r"нвп", r"втп", r"нвк",
    r"фоп", r"пп", r"сп",
    r"кб", r"нді",
]

# Pre-compile a single alternation, longest-first (sort by length desc).
_ALL_FORMS = sorted(
    _LEGAL_FORMS_LATIN + _LEGAL_FORMS_CYRILLIC,
    key=lambda p: -len(p),
)
_LEADING_FORM_RE = re.compile(
    r"^[\s" + re.escape(_QUOTES) + r"]*"
    r"(?:" + "|".join(_ALL_FORMS) + r")"
    r"[\s" + re.escape(_QUOTES) + r"\.,]*",
    re.IGNORECASE,
)


def _strip_invisibles(s: str) -> str:
    return "".join(ch for ch in s if ch not in _INVISIBLES)


def _strip_outer_quotes(s: str) -> str:
    s = s.strip()
    while s and (s[0] in _QUOTES or s[-1] in _QUOTES):
        if s[0] in _QUOTES:
            s = s[1:].strip()
        if s and s[-1] in _QUOTES:
            s = s[:-1].strip()
    return s


def strip_legal_boilerplate(name: str) -> str:
    """Remove Ukrainian legal-entity prefixes, quotes, and invisibles.

    Strips iteratively — multi-token prefixes (e.g. "TOV" + "Konstruktorske
    Byuro") are both removed because the loop re-matches the leading position
    until no further legal form is found.

    Returns the cleaned name with normalized whitespace. If stripping would
    leave an empty string, the original name is returned unchanged.
    """
    if not name:
        return name

    cleaned = unicodedata.normalize("NFC", name)
    cleaned = _strip_invisibles(cleaned)
    cleaned = _strip_outer_quotes(cleaned)

    for _ in range(6):  # bounded; legal stacks rarely exceed 2-3 layers
        before = cleaned
        cleaned = _LEADING_FORM_RE.sub("", cleaned, count=1)
        cleaned = _strip_outer_quotes(cleaned)
        if cleaned == before:
            break

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else name


if __name__ == "__main__":
    samples = [
        "ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ «СЕКУР ІНТЕГРАЦІЯ»",
        "ТОВ \"Ромсат Україна\"",
        "tovarystvo z obmezhenoyu vidpovidalnistyu konstruktorske byuro lohika",
        "TOV NVK Yu-Forse",
        "ФОП Іваненко Іван Іванович",
        "ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДА­ЛЬНІСТЮ «ВИРОБНИЧО-ТЕХНІЧНЕ ПІДПРИЄМСТВО «МЕТАЛОКОНСТРУКЦІЯ»",
    ]
    for s in samples:
        print(f"BEFORE: {s}")
        print(f"AFTER : {strip_legal_boilerplate(s)}")
        print()
