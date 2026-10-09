"""
Tiered fallback for thin enrichment results.

When the per-company Tier 1 extraction in enrich.run() returns no founders
AND no contacts but the company has a real website, this module provides:

  Tier 2 — tier2_path_walk(homepage_url, claude_client, company_name, *, fetch_text)
           Walks /about, /team, /leadership, etc. off the discovered domain,
           Claude-extracts people + contacts from each, stops at the first hit.

  Tier 3 — tier3_linkedin_search(company_name, claude_client, *, ddg_search, fetch_text)
           Off-by-default. Two LinkedIn-restricted DDG queries → top 1-2
           results → Jina fetch → Claude extract.

Both helpers return the same dict shape as enrich._extract_via_claude's
founder/contact records, plus a private '_path' key naming which subpath
(or 'linkedin') produced the hit. None on no-data.

Design notes:

* `fetch_text` and `ddg_search` are injected callables so this module
  doesn't import enrich.py (which would be a circular dependency at the
  package level). enrich.run() passes the existing `_fetch_text` and
  `_ddg_search_with_backoff`.

* The Tier 2 tool schema mirrors the founder/contact record shape from
  enrich._EXTRACT_TOOL exactly — same field names, same types, same
  confidence semantics — so the existing `_store_founders` /
  `_store_contacts` paths in enrich.py consume the merged result without
  modification.

* Circuit-breaker integration: only DDG calls participate
  (via the injected `ddg_search` which is enrich._ddg_search_with_backoff,
  itself wired to `_record_failure`). Per-path Jina failures and Claude
  failures inside fallbacks DO NOT touch the breaker — they're per-page
  data-shape concerns, not systemic outages, and the per-company body
  already records the breaker-relevant failures during Tier 1.

* Wall-clock budget: TIER2_TIMEOUT_BUDGET_S caps a single Tier 2 walk so
  pathological domains can't blow up the per-company time budget.

Run the self-test:
    python -m src.enrich_fallback --self-test
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Callable
from urllib.parse import urlparse, urlunparse

import anthropic

log = logging.getLogger("enrichment")

# ── Tier 2 constants ──────────────────────────────────────────────────────────

# Path-walk candidates in priority order. /about and /team are the highest-yield
# paths on small-business websites. Order matters — we stop at the first hit.
TIER2_PATHS: tuple[str, ...] = (
    "/about", "/about-us", "/team", "/leadership",
    "/our-team", "/people", "/company",
)

# Total wall-clock ceiling for one Tier 2 walk (Jina fetches + Claude calls).
TIER2_TIMEOUT_BUDGET_S: float = 90.0

# Below this character count, Jina is almost certainly returning the host's
# 404-chrome (probe data: 161-232 chars on dead paths vs 5,384 on a real
# /team page). Skip Claude in that case.
TIER2_MIN_PAGE_CHARS: int = 600

_LINKEDIN_QUERIES = (
    '"{name}" "founder" site:linkedin.com',
    '"{name}" "CEO" site:linkedin.com',
)

# Per-fetch URL count for Tier 3.
TIER3_MAX_LINKEDIN_FETCHES: int = 2


# ── Claude tool schema (mirrors enrich._EXTRACT_TOOL's people/contact shapes) ─

_TIER2_TOOL = {
    "name": "extract_people_and_contacts",
    "description": (
        "Extract founders, key personnel, and contact info from a single "
        "company page (typically /about, /team, or /leadership)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "founders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":          {"type": "string"},
                        "role":          {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "linkedin_url":  {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "bio":           {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "background":    {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "confidence":    {"type": "number"},
                    },
                    "required": ["name", "confidence"],
                },
                "description": (
                    "Founders / co-founders / executives explicitly named on the page. "
                    "Only include people whose names appear verbatim. Do NOT fabricate. "
                    "Confidence: 1.0 if explicitly described as founder/co-founder, "
                    "0.7 if inferred from CEO/leadership role with bio, "
                    "0.3 if speculative."
                ),
            },
            "contacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["general_email", "sales_email", "phone",
                                     "twitter", "linkedin"],
                        },
                        "value":      {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["type", "value", "confidence"],
                },
                "description": (
                    "Contact info that appears VERBATIM in the source. "
                    "Do NOT fabricate or guess emails from domain names. "
                    "Confidence: 1.0 if directly listed, 0.5 if inferred."
                ),
            },
            "linkedin_url": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": (
                    "Company LinkedIn URL if present in the page text "
                    "(format: https://www.linkedin.com/company/...). Null if not found."
                ),
            },
            "hq_country": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "HQ country if mentioned on the page; null otherwise.",
            },
            "founded_year": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Four-digit founding year if mentioned; null otherwise.",
            },
        },
        "required": ["founders", "contacts", "linkedin_url",
                     "hq_country", "founded_year"],
    },
}

_TIER2_SYSTEM = (
    "You are extracting people and contact data from a single defense-tech "
    "company page. Be strict: only return information that appears verbatim "
    "in the source text. Never fabricate names, emails, or phone numbers. "
    "If the page is empty, a 404, or unrelated, return empty arrays / nulls. "
    "Confidence scores follow the schema convention: 1.0 = directly stated, "
    "0.7 = clearly inferred from context, 0.3 = speculative."
)


# ── Merge / dedup helpers ─────────────────────────────────────────────────────


def _normalize_person_name(name: str | None) -> str:
    """Normalise a person's name for dedup.

    Whitespace-normalised then lowercased. So `"John  Smith "` and
    `"john smith"` collide. Per-PR scope: handles the common case;
    title variants ("John A. Smith" vs "John Smith") aren't deduped.
    """
    if not name:
        return ""
    return " ".join(str(name).split()).lower()


def _confidence_of(record: dict) -> float:
    try:
        return float(record.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def merge_founders(*lists: list[dict] | None) -> list[dict]:
    """Concatenate + dedup founder records by normalized name.

    On a duplicate, the higher-confidence record wins. Ordering of inputs
    matters only as a tie-break: earlier-list wins on equal confidence.
    """
    by_key: dict[str, dict] = {}
    for fl in lists:
        for f in (fl or []):
            n = (f.get("name") or "").strip()
            key = _normalize_person_name(n)
            if not key:
                continue
            existing = by_key.get(key)
            if existing is None or _confidence_of(f) > _confidence_of(existing):
                by_key[key] = f
    return list(by_key.values())


def merge_contacts(*lists: list[dict] | None) -> list[dict]:
    """Concatenate + dedup contact records by (type, lowercased value)."""
    by_key: dict[tuple[str, str], dict] = {}
    for cl in lists:
        for c in (cl or []):
            ctype = (c.get("type") or "").strip()
            cval  = (c.get("value") or "").strip().lower()
            if not ctype or not cval:
                continue
            key = (ctype, cval)
            existing = by_key.get(key)
            if existing is None or _confidence_of(c) > _confidence_of(existing):
                by_key[key] = c
    return list(by_key.values())


# ── Claude call ───────────────────────────────────────────────────────────────


def _extract_people_via_claude(
    client: anthropic.Anthropic,
    company_name: str,
    page_url: str,
    page_text: str,
) -> dict | None:
    """Call Claude with the focused Tier 2 tool. Returns dict or None on error."""
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _TIER2_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_TIER2_TOOL],
            tool_choice={"type": "tool", "name": "extract_people_and_contacts"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Company name: {company_name}\n"
                        f"Page URL: {page_url}\n\n"
                        f"Page text:\n{page_text}"
                    ),
                }
            ],
        )
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        return None
    except anthropic.APIError as exc:
        log.warning("Tier-fallback Claude error on %s: %s", page_url, exc)
        return None


def _has_useful_data(payload: dict | None) -> bool:
    """A Tier 2/3 hit is 'useful' if it produced at least one founder or contact."""
    if not payload:
        return False
    return bool(payload.get("founders")) or bool(payload.get("contacts"))


# ── Tier 2 ────────────────────────────────────────────────────────────────────


def _domain_root(homepage_url: str) -> str | None:
    """Strip path/query from `homepage_url` to get scheme://netloc."""
    try:
        parsed = urlparse(homepage_url)
        if not parsed.scheme or not parsed.netloc:
            return None
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    except Exception:
        return None


def tier2_path_walk(
    homepage_url: str,
    claude_client: anthropic.Anthropic,
    company_name: str,
    *,
    fetch_text: Callable[[str], str | None],
    timeout_budget_s: float = TIER2_TIMEOUT_BUDGET_S,
) -> dict | None:
    """Walk well-known about/team paths off `homepage_url` until we find people.

    Returns a payload of the same shape as Tier 1's
    `_extract_via_claude` for {founders, contacts, linkedin_url,
    hq_country, founded_year}, with an extra `_path` key indicating which
    subpath produced the hit. Returns None if no path yielded useful data.

    Stops at the first founder-or-contact match. Honours `timeout_budget_s`
    total wall-clock; aborts the remaining walk on overrun.
    """
    root = _domain_root(homepage_url)
    if not root:
        log.debug("tier2: skipped — could not parse %r", homepage_url)
        return None

    start = time.perf_counter()
    paths_tried: list[str] = []
    for path in TIER2_PATHS:
        elapsed = time.perf_counter() - start
        if elapsed > timeout_budget_s:
            log.warning(
                "tier2: budget exhausted at %.1fs after %d/%d paths (%s)",
                elapsed, len(paths_tried), len(TIER2_PATHS), root,
            )
            break
        url = f"{root}{path}"
        paths_tried.append(path)

        text = fetch_text(url)
        if not text or len(text) < TIER2_MIN_PAGE_CHARS:
            log.debug("tier2: %s skipped (len=%d, threshold=%d)",
                      url, len(text or ""), TIER2_MIN_PAGE_CHARS)
            continue

        payload = _extract_people_via_claude(claude_client, company_name, url, text)
        if _has_useful_data(payload):
            payload["_path"] = path
            log.info("tier2: hit on %s (founders=%d contacts=%d)",
                     url,
                     len(payload.get("founders") or []),
                     len(payload.get("contacts") or []))
            return payload

    log.info("tier2: walked %d path(s), no people found (root=%s)",
             len(paths_tried), root)
    return None


# ── Tier 3 ────────────────────────────────────────────────────────────────────


def tier3_linkedin_search(
    company_name: str,
    claude_client: anthropic.Anthropic,
    *,
    ddg_search: Callable[[str, int], list[dict]],
    fetch_text: Callable[[str], str | None],
    max_fetches: int = TIER3_MAX_LINKEDIN_FETCHES,
) -> dict | None:
    """LinkedIn fallback — off by default in enrich.run().

    Two queries:
      "<firm-name>" "founder" site:linkedin.com
      "<firm-name>" "CEO"     site:linkedin.com

    Take top LinkedIn URLs, Jina-fetch, Claude-extract.

    `ddg_search` MUST be the existing `_ddg_search_with_backoff` so the
    circuit breaker fires correctly on rate-limit cascades.
    """
    seen: list[str] = []
    for q_template in _LINKEDIN_QUERIES:
        if len(seen) >= max_fetches:
            break
        q = q_template.format(name=company_name)
        try:
            results = ddg_search(q, 5)
        except TypeError:
            # Some implementations use kwarg max_results=
            results = ddg_search(q, max_results=5)  # type: ignore[call-arg]
        for r in results or []:
            url = (r.get("href") or "").strip()
            if not url or "linkedin.com" not in url.lower():
                continue
            if url in seen:
                continue
            seen.append(url)
            if len(seen) >= max_fetches:
                break
        time.sleep(0.5)

    if not seen:
        log.info("tier3: no LinkedIn URLs from DDG for %r", company_name)
        return None

    aggregate_payload: dict = {
        "founders":     [],
        "contacts":     [],
        "linkedin_url": None,
        "hq_country":   None,
        "founded_year": None,
    }
    for url in seen:
        text = fetch_text(url)
        if not text or len(text) < TIER2_MIN_PAGE_CHARS:
            log.debug("tier3: %s skipped (len=%d)", url, len(text or ""))
            continue
        payload = _extract_people_via_claude(claude_client, company_name, url, text)
        if not payload:
            continue
        aggregate_payload["founders"] = merge_founders(
            aggregate_payload.get("founders"), payload.get("founders")
        )
        aggregate_payload["contacts"] = merge_contacts(
            aggregate_payload.get("contacts"), payload.get("contacts")
        )
        for k in ("linkedin_url", "hq_country", "founded_year"):
            if not aggregate_payload.get(k) and payload.get(k):
                aggregate_payload[k] = payload[k]

    if not _has_useful_data(aggregate_payload):
        log.info("tier3: %d LinkedIn fetch(es), no people found for %r",
                 len(seen), company_name)
        return None

    aggregate_payload["_path"] = "linkedin"
    log.info("tier3: hit (founders=%d contacts=%d) for %r",
             len(aggregate_payload["founders"]),
             len(aggregate_payload["contacts"]),
             company_name)
    return aggregate_payload


# ── Self-test fixture ─────────────────────────────────────────────────────────

# Pure-function tests for the merge helpers. Tier 2/3 themselves are integration
# code (require Claude + Jina + DDG); their behaviour is exercised in the
# 5-company test cohort run, not here.

_MERGE_TESTS: list[tuple[str, callable, list, list]] = [
    (
        "founders: empty + empty -> empty",
        lambda: merge_founders([], []),
        [],
        [],
    ),
    (
        "founders: dedup by lowercased name",
        lambda: merge_founders(
            [{"name": "John Smith", "confidence": 0.7}],
            [{"name": "JOHN SMITH", "confidence": 1.0}],
        ),
        [{"name": "JOHN SMITH", "confidence": 1.0}],   # higher-confidence wins
        [],
    ),
    (
        "founders: dedup with whitespace differences",
        lambda: merge_founders(
            [{"name": "John  Smith", "confidence": 0.5}],
            [{"name": "john smith ", "confidence": 0.7}],
        ),
        [{"name": "john smith ", "confidence": 0.7}],  # whitespace-normalized key
        [],
    ),
    (
        "founders: tie on confidence -> earlier list wins",
        lambda: merge_founders(
            [{"name": "Jane Doe", "confidence": 0.7, "_src": "t1"}],
            [{"name": "jane doe", "confidence": 0.7, "_src": "t2"}],
        ),
        [{"name": "Jane Doe", "confidence": 0.7, "_src": "t1"}],
        [],
    ),
    (
        "founders: distinct names both kept",
        lambda: merge_founders(
            [{"name": "Alice", "confidence": 1.0}],
            [{"name": "Bob",   "confidence": 1.0}],
        ),
        [
            {"name": "Alice", "confidence": 1.0},
            {"name": "Bob",   "confidence": 1.0},
        ],
        [],
    ),
    (
        "founders: empty/blank names skipped",
        lambda: merge_founders(
            [{"name": "", "confidence": 1.0},
             {"name": "   ", "confidence": 1.0},
             {"name": "Real Person", "confidence": 0.7}],
            [],
        ),
        [{"name": "Real Person", "confidence": 0.7}],
        [],
    ),
    (
        "contacts: dedup by (type, lowercased value)",
        lambda: merge_contacts(
            [{"type": "general_email", "value": "Hello@Example.com", "confidence": 0.5}],
            [{"type": "general_email", "value": "hello@example.com", "confidence": 1.0}],
        ),
        [{"type": "general_email", "value": "hello@example.com", "confidence": 1.0}],
        [],
    ),
    (
        "contacts: same value different type both kept",
        lambda: merge_contacts(
            [{"type": "general_email", "value": "info@x.com", "confidence": 0.5}],
            [{"type": "sales_email",   "value": "info@x.com", "confidence": 0.5}],
        ),
        [
            {"type": "general_email", "value": "info@x.com", "confidence": 0.5},
            {"type": "sales_email",   "value": "info@x.com", "confidence": 0.5},
        ],
        [],
    ),
    (
        "contacts: empty/blank fields skipped",
        lambda: merge_contacts(
            [{"type": "", "value": "stuff", "confidence": 1.0},
             {"type": "phone", "value": "", "confidence": 1.0},
             {"type": "phone", "value": "555-1212", "confidence": 1.0}],
            [],
        ),
        [{"type": "phone", "value": "555-1212", "confidence": 1.0}],
        [],
    ),
]


def _run_self_test() -> int:
    """Run the pure-function merge tests. Returns shell exit code."""
    ok = 0
    fail = 0
    for label, fn, expected, _unused in _MERGE_TESTS:
        try:
            got = fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {label}  → raised {type(e).__name__}: {e}")
            fail += 1
            continue
        # Compare as set-of-frozenset for order-independence (dedup may reorder).
        def _key(d): return tuple(sorted((k, repr(v)) for k, v in d.items()))
        got_keys = sorted(_key(d) for d in got)
        exp_keys = sorted(_key(d) for d in expected)
        if got_keys == exp_keys:
            print(f"  ✓ {label}")
            ok += 1
        else:
            print(f"  ✗ {label}")
            print(f"      expected: {expected}")
            print(f"      got     : {got}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--self-test", action="store_true",
                   help="Run merge-helper unit tests and exit.")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.self_test:
        sys.exit(_run_self_test())
    print("(no-op without --self-test)")
