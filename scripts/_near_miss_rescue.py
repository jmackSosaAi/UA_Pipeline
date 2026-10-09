"""NEAR_MISS rescue — NULL-only country backfill + safe re-validation.

Resolves Q29(b). Operates entirely on already-fetched data — NO Apify
calls. Workflow:

  1. NULL-only country backfill (SBIR rows where hq_country is NULL get
     'United States' per D-015 / 13 CFR § 121.702). All other multi-
     country sources stay NULL.
  2. For each NEAR_MISS row where companies.linkedin_url is NULL:
     re-call the D-014 canonicalizer with the now-filled hq_country
     and returned_country=None (we don't have the original returned
     country and won't re-fetch). The canonicalizer's name-only path
     (D-008/D-014: our known + returned None → MATCH) substitutes for
     the country re-check.
  3. Apply false-positive guards as substitute for the country check
     we can't redo:
       - fuzzy_score >= 95
       - normalized name match OR returned-begins-with-our-name+sep
       - returned must NOT contain Cyrillic / CJK / Arabic / etc. when
         our_name is Latin-only (the Vermeer / 'VerMeer Салон
         Интерьера' case)
       - returned length <= 3 × our length
  4. Auto-accept passes → write companies.linkedin_url with the D-018
     uniqueness guard, mark linkedin_url_discoveries.outcome
     'RESCUED_MATCH'. Failures → stage for operator review.

Outputs:
  docs/artifacts/near_miss_rescue/country_inferences.csv
  docs/artifacts/near_miss_rescue/manual_review.csv
  docs/NEAR_MISS_RESCUE_REPORT.md
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = _HERE.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.base import DB_PATH                              # noqa: E402
from collectors.linkedin_canonicalize import (                    # noqa: E402
    classify_match_with_country,
)

ARTIFACTS_DIR = ROOT / "docs" / "artifacts" / "near_miss_rescue"
REPORT_PATH   = ROOT / "docs" / "NEAR_MISS_RESCUE_REPORT.md"

# Auto-accept halt threshold per spec — guard logic may be too strict
# if we rescue fewer than this many rows.
HALT_THRESHOLD = 50

log = logging.getLogger("near_miss_rescue")


# ── Name normalisation (for the auto-accept name-match guard) ──────────────

_LEGAL_SUFFIX_TOKENS = {
    "llc", "lllp", "lp",
    "inc", "incorporated", "corp", "corporation",
    "ltd", "limited",
    "gmbh", "ag", "se", "as", "ab", "oy", "kft",
    "sa", "spa", "srl", "s.r.l.", "sl", "sas", "bv", "nv",
    "pty", "plc", "company", "co", "co.",
    "doo",
}
# Match the suffix as a trailing token (preceded by space or punctuation,
# possibly trailed by punctuation). Case-insensitive applied to lowercase.
_SUFFIX_RE = re.compile(
    r"(?:[\s,.;]+)("
    + "|".join(re.escape(s) for s in sorted(_LEGAL_SUFFIX_TOKENS, key=len, reverse=True))
    + r")[\s,.;]*$",
    re.IGNORECASE,
)
_PUNCT_COLLAPSE_RE = re.compile(r"[^\w\s]+")
_WS_COLLAPSE_RE = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    """Lowercase, iteratively strip trailing legal suffixes, drop
    punctuation, collapse whitespace. Same intent as the discovery
    slug heuristic but kept here independently for the auto-accept
    name-match guard."""
    s = (name or "").strip().lower()
    while True:
        new = _SUFFIX_RE.sub("", s).strip()
        if new == s:
            break
        s = new
    s = _PUNCT_COLLAPSE_RE.sub(" ", s)
    s = _WS_COLLAPSE_RE.sub(" ", s).strip()
    return s


# ── Latin-script detection (Vermeer guard) ──────────────────────────────────

# Codepoints we consider "Latin-compatible": ASCII, Latin-1 supplement,
# Latin Extended A/B + IPA. Anything outside these scripts on the
# returned side when our side is Latin-only is a script-mismatch signal.
def _is_latin_only(text: str) -> bool:
    """True if every alphabetic char in `text` belongs to a Latin
    script. Punctuation, digits, whitespace pass through."""
    if not text:
        return True
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            script = unicodedata.name(ch, "")
        except ValueError:
            return False
        if not script.startswith("LATIN"):
            return False
    return True


# ── Auto-accept guard chain ─────────────────────────────────────────────────


@dataclass
class GuardResult:
    accept: bool
    reason: str  # set to a label even on accept (for audit clarity)


def auto_accept_check(
    *, our_name: str, returned_name: str, fuzzy_score: float
) -> GuardResult:
    """All-or-nothing guard chain. Returns first-fail reason; on pass,
    returns reason='passed_all_guards'."""
    if fuzzy_score < 95:
        return GuardResult(False, f"score<95 ({fuzzy_score:.1f})")

    if _is_latin_only(our_name) and not _is_latin_only(returned_name):
        return GuardResult(False, "returned_has_non_latin_script")

    # Length-ratio guard (Vermeer-class blow-up catch even when
    # non-Latin chars happen to be stripped or absent).
    our_len = max(len(our_name.strip()), 1)
    ret_len = len(returned_name.strip())
    if ret_len > 3 * our_len:
        return GuardResult(False, f"returned_length_blowup ({ret_len}>{3*our_len})")

    # Normalised name match — either exact equality after suffix-strip,
    # or the returned name begins with our_name plus a non-word separator
    # (catches 'Saker' → 'Saker Digital' as a legitimate rebrand/division).
    a = _normalize_name(our_name)
    b = _normalize_name(returned_name)
    if a and b and a == b:
        return GuardResult(True, "normalized_name_exact")
    if a and b and b.startswith(a):
        rest = b[len(a):]
        if not rest or not rest[0].isalnum():
            return GuardResult(True, "returned_starts_with_our_name")
    return GuardResult(False, f"normalized_name_mismatch (a={a!r} b={b!r})")


# ── Country backfill (step 2) ───────────────────────────────────────────────


def backfill_null_countries(
    conn: sqlite3.Connection, *, dry_run: bool, csv_writer
) -> dict[str, int]:
    """NULL-only backfill per spec.

    SBIR rows with hq_country IS NULL → 'United States' (D-015).
    All other sources stay NULL (multi-country; can't safely infer).
    Returns count by source."""
    counts: dict[str, int] = {}

    sbir_rows = conn.execute(
        "SELECT id, name FROM companies "
        "WHERE source='SBIR' AND (hq_country IS NULL OR TRIM(hq_country)='')"
    ).fetchall()
    counts["SBIR"] = len(sbir_rows)
    for row in sbir_rows:
        cid, name = row[0], row[1]
        csv_writer.writerow([cid, name, "SBIR", "(NULL)", "United States",
                             "D-015: SBIR is US-only per 13 CFR § 121.702"])
        if not dry_run:
            conn.execute(
                "UPDATE companies SET hq_country='United States' WHERE id = ?",
                (cid,),
            )
    if not dry_run:
        conn.commit()
    return counts


# ── Rescue loop (step 3) ────────────────────────────────────────────────────


@dataclass
class Rescue:
    company_id: int
    our_name: str
    source: str | None
    hq_country: str | None
    attempted_url: str
    returned_org_name: str
    fuzzy_score: float
    candidate_source: str | None
    discovery_row_id: int

    decision: str = ""           # AUTO_ACCEPT | REVIEW | DROP
    revalidate_outcome: str = "" # MATCH | NEAR_MISS | COLLISION
    reason: str = ""             # guard failure reason or accept label
    write_attempted: bool = False
    write_succeeded: bool = False
    collision_with: int | None = None


def load_rescue_candidates(conn: sqlite3.Connection) -> list[Rescue]:
    """One row per (company_id, attempted_url) — keep the highest-score
    discovery row per company. Multiple companies have 2 NEAR_MISS rows
    (Phase 2a + Phase 2d.1); deduping by best-per-company avoids
    repeated rescue attempts for the same company."""
    rows = conn.execute(
        """
        SELECT d.id, d.company_id, c.name AS our_name, c.source,
               c.hq_country, d.attempted_url, d.returned_org_name,
               d.fuzzy_score, d.candidate_source
        FROM linkedin_url_discoveries d
        JOIN companies c ON c.id = d.company_id
        WHERE d.outcome = 'NEAR_MISS'
          AND (c.linkedin_url IS NULL OR TRIM(c.linkedin_url) = '')
        """
    ).fetchall()
    by_co: dict[int, Rescue] = {}
    for r in rows:
        rid, cid, name, src, country, url, rname, score, csrc = r
        rec = Rescue(
            company_id=cid, our_name=name, source=src, hq_country=country,
            attempted_url=url or "", returned_org_name=rname or "",
            fuzzy_score=float(score or 0.0), candidate_source=csrc,
            discovery_row_id=rid,
        )
        prev = by_co.get(cid)
        if prev is None or rec.fuzzy_score > prev.fuzzy_score:
            by_co[cid] = rec
    return list(by_co.values())


def evaluate_rescue(r: Rescue) -> None:
    """Set r.decision / r.revalidate_outcome / r.reason in place."""
    cls, _score, _reason = classify_match_with_country(
        r.our_name or "", r.returned_org_name or "",
        r.hq_country, None,
    )
    r.revalidate_outcome = cls
    if cls != "MATCH":
        r.decision = "REVIEW"
        r.reason = f"revalidate_returned_{cls}" + (
            f"_{_reason}" if _reason else ""
        )
        return
    guard = auto_accept_check(
        our_name=r.our_name or "", returned_name=r.returned_org_name or "",
        fuzzy_score=r.fuzzy_score,
    )
    if guard.accept:
        r.decision = "AUTO_ACCEPT"
        r.reason = guard.reason
    else:
        r.decision = "REVIEW"
        r.reason = f"guard_failed:{guard.reason}"


# ── Write step (D-018 uniqueness guard) ─────────────────────────────────────


def write_rescue(conn: sqlite3.Connection, r: Rescue) -> None:
    """Atomic write per rescue: uniqueness check → UPDATE companies →
    UPDATE linkedin_url_discoveries. Marks r.write_attempted and
    r.write_succeeded (and r.collision_with on D-018 collision)."""
    r.write_attempted = True
    row = conn.execute(
        "SELECT id FROM companies WHERE linkedin_url = ? AND id != ?",
        (r.attempted_url, r.company_id),
    ).fetchone()
    if row is not None:
        r.collision_with = int(row[0])
        r.decision = "REVIEW"
        r.reason = f"d018_collision_with_company_{r.collision_with}"
        log.warning(
            "D-018 COLLISION: %s already assigned to company %d; "
            "skip rescue write for company %d (%s)",
            r.attempted_url, r.collision_with, r.company_id, r.our_name,
        )
        return
    conn.execute(
        "UPDATE companies SET linkedin_url = ? WHERE id = ?",
        (r.attempted_url, r.company_id),
    )
    conn.execute(
        "UPDATE linkedin_url_discoveries "
        "SET outcome='RESCUED_MATCH', attempted_at = datetime('now') "
        "WHERE id = ?",
        (r.discovery_row_id,),
    )
    r.write_succeeded = True


# ── Manual-review CSV (step 4) ──────────────────────────────────────────────


def write_manual_review(rescues: list[Rescue], path: Path) -> int:
    """Stage everything that isn't AUTO_ACCEPT-and-written. Returns count."""
    fieldnames = [
        "company_id", "our_name", "returned_org_name", "fuzzy_score",
        "attempted_url", "source", "hq_country_was", "hq_country_now",
        "candidate_source", "revalidate_outcome", "reason_flagged",
    ]
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rescues:
            if r.decision == "AUTO_ACCEPT" and r.write_succeeded:
                continue
            w.writerow({
                "company_id":          r.company_id,
                "our_name":            r.our_name,
                "returned_org_name":   r.returned_org_name,
                "fuzzy_score":         f"{r.fuzzy_score:.1f}",
                "attempted_url":       r.attempted_url,
                "source":              r.source or "",
                # We backfilled inside the same transaction; "was" and
                # "now" are recoverable by reading the inferences CSV.
                # Here we just record the row's current state.
                "hq_country_was":      r.hq_country or "",
                "hq_country_now":      r.hq_country or "",
                "candidate_source":    r.candidate_source or "",
                "revalidate_outcome":  r.revalidate_outcome,
                "reason_flagged":      r.reason,
            })
            n += 1
    return n


# ── Report writer (step 5) ──────────────────────────────────────────────────


def write_report(
    *,
    rescues: list[Rescue],
    country_counts: dict[str, int],
    review_count: int,
    halted: bool,
) -> None:
    auto = [r for r in rescues if r.decision == "AUTO_ACCEPT" and r.write_succeeded]
    coll = [r for r in rescues if r.collision_with is not None]
    review = [r for r in rescues if not (r.decision == "AUTO_ACCEPT" and r.write_succeeded)]

    reason_counts = Counter(r.reason for r in review)

    # Ukrainian subset — operationally important for Kyiv per spec.
    def is_ukr(r: Rescue) -> bool:
        return (r.hq_country or "").strip().lower() in (
            "ukraine", "ua"
        ) or "ukraine" in (r.returned_org_name or "").lower()
    ukr_auto = [r for r in auto if is_ukr(r)]

    lines: list[str] = []
    lines.append("# NEAR_MISS rescue report")
    lines.append("")
    lines.append(f"- **Date:** 2026-05-11")
    lines.append(f"- **Cohort:** 431 cumulative NEAR_MISS rows; "
                 f"{len(rescues)} rescue candidates after per-company dedup "
                 f"(companies whose linkedin_url is still NULL).")
    if halted:
        lines.append(f"- **HALTED:** auto-accept count "
                     f"{len(auto)} < {HALT_THRESHOLD}; no writes applied. "
                     f"See manual_review.csv + reason breakdown below.")
    lines.append("")
    lines.append("## TL;DR")
    lines.append("")
    if halted:
        lines.append(
            f"Guard logic produced **{len(auto)}** auto-accept candidates, "
            f"below the {HALT_THRESHOLD}-row halt threshold. **No writes "
            f"applied**; cohort returned unchanged. Manual review queue "
            f"contains all {review_count} candidates with the guard-failure "
            f"reason recorded per row."
        )
    else:
        lines.append(
            f"**{len(auto)}** companies rescued — `linkedin_url` written, "
            f"`linkedin_url_discoveries.outcome` flipped to "
            f"`RESCUED_MATCH`. **{review_count}** rows staged for manual "
            f"review at `docs/artifacts/near_miss_rescue/manual_review.csv`. "
            f"**{len(coll)}** D-018 uniqueness collisions caught and "
            f"diverted to review (would-be auto-accept rescues whose URL "
            f"was already claimed by another company)."
        )
    lines.append("")

    lines.append("## Step 2 — NULL-only country backfill")
    lines.append("")
    total_bf = sum(country_counts.values())
    lines.append(f"Total companies backfilled: **{total_bf}**.")
    if total_bf > 0:
        lines.append("")
        lines.append("| Source | Rows backfilled |")
        lines.append("|--------|----------------:|")
        for src, n in sorted(country_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {src} | {n} |")
        lines.append("")
    else:
        lines.append("")
        lines.append("Zero rows backfilled — D-015's earlier SBIR backfill "
                     "(commit `7c20352`) already filled all 2,282 SBIR rows. "
                     "No SBIR rows in the current DB have `hq_country IS "
                     "NULL`. Other sources (brave1_articles, NATO DIANA, "
                     "etc.) stay NULL per spec — multi-country, can't safely "
                     "infer.")
        lines.append("")

    lines.append("## Step 3 — Re-validation outcomes")
    lines.append("")
    out_counts = Counter(r.revalidate_outcome for r in rescues)
    lines.append("| revalidate_outcome | count |")
    lines.append("|--------------------|------:|")
    for k in ("MATCH", "NEAR_MISS", "COLLISION"):
        lines.append(f"| {k} | {out_counts.get(k, 0)} |")
    lines.append("")
    lines.append(f"The MATCH rows then flowed through the four auto-accept "
                 f"guards. Of {out_counts.get('MATCH', 0)} MATCHes:")
    lines.append("")
    n_auto = len(auto)
    n_guard_fail = sum(1 for r in rescues if r.revalidate_outcome == "MATCH"
                       and r.decision == "REVIEW" and r.collision_with is None
                       and r.reason.startswith("guard_failed:"))
    n_d018 = len(coll)
    lines.append(f"- Auto-accepted + written: **{n_auto}**")
    lines.append(f"- Failed guard chain → review: **{n_guard_fail}**")
    lines.append(f"- D-018 uniqueness collision → review: **{n_d018}**")
    lines.append("")

    lines.append("## Step 4 — Manual review queue breakdown")
    lines.append("")
    lines.append(f"`docs/artifacts/near_miss_rescue/manual_review.csv` — "
                 f"{review_count} rows.")
    lines.append("")
    lines.append("| reason_flagged | count |")
    lines.append("|----------------|------:|")
    for reason, n in reason_counts.most_common(20):
        lines.append(f"| `{reason}` | {n} |")
    lines.append("")

    if auto:
        lines.append("## Auto-accepted matches — sample of 10")
        lines.append("")
        lines.append("| company_id | our_name | returned_org_name | score | source | hq_country |")
        lines.append("|-----------:|----------|-------------------|------:|--------|------------|")
        for r in auto[:10]:
            lines.append(
                f"| {r.company_id} | {r.our_name} | {r.returned_org_name} | "
                f"{r.fuzzy_score:.0f} | {r.source or ''} | {r.hq_country or ''} |"
            )
        lines.append("")
        lines.append(f"### Ukrainian subset of auto-accepts ({len(ukr_auto)} of {len(auto)})")
        lines.append("")
        if ukr_auto:
            lines.append("| company_id | our_name | returned_org_name | source |")
            lines.append("|-----------:|----------|-------------------|--------|")
            for r in ukr_auto[:25]:
                lines.append(
                    f"| {r.company_id} | {r.our_name} | {r.returned_org_name} | "
                    f"{r.source or ''} |"
                )
            lines.append("")
        else:
            lines.append("(none — auto-accepts didn't include rows whose "
                         "`hq_country` is Ukraine or whose returned_org_name "
                         "contains 'Ukraine')")
            lines.append("")

    if coll:
        lines.append("## D-018 uniqueness collisions caught")
        lines.append("")
        lines.append("| company_id | our_name | attempted_url | collision_with |")
        lines.append("|-----------:|----------|---------------|---------------:|")
        for r in coll:
            lines.append(
                f"| {r.company_id} | {r.our_name} | {r.attempted_url} | "
                f"{r.collision_with} |"
            )
        lines.append("")

    lines.append("## Net writes to `companies.linkedin_url`")
    lines.append("")
    lines.append(f"**{n_auto}** new validated URLs written this session.")
    lines.append("")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(
        description="NEAR_MISS rescue per Q29(b). NULL-only country "
                    "backfill + canonicalizer re-validation + Latin/length "
                    "guards. Writes companies.linkedin_url for auto-accepts "
                    "under D-018; stages the rest for manual review.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Compute decisions and produce CSV+report, but "
                        "skip the country backfill UPDATE and the rescue "
                        "writes to companies + linkedin_url_discoveries.")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    inferences_path = ARTIFACTS_DIR / "country_inferences.csv"
    review_path = ARTIFACTS_DIR / "manual_review.csv"

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")

    # Step 2 — country backfill (open CSV inside transaction context).
    with inferences_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["company_id", "name", "source", "hq_country_was",
                    "hq_country_now", "reason"])
        country_counts = backfill_null_countries(conn, dry_run=args.dry_run, csv_writer=w)
    log.info("country backfill: %s", country_counts)

    # Step 3 — load rescue cohort with post-backfill hq_country.
    rescues = load_rescue_candidates(conn)
    log.info("rescue candidates: %d (post-dedup, one per company_id)",
             len(rescues))

    for r in rescues:
        evaluate_rescue(r)

    auto_candidates = [r for r in rescues if r.decision == "AUTO_ACCEPT"]
    log.info("auto-accept candidates pre-D018: %d (halt threshold: %d)",
             len(auto_candidates), HALT_THRESHOLD)

    halted = len(auto_candidates) < HALT_THRESHOLD
    if not halted and not args.dry_run:
        for r in auto_candidates:
            write_rescue(conn, r)
        conn.commit()
    else:
        if halted:
            log.warning("HALTED: auto-accept count %d below threshold %d. "
                        "No writes applied; review CSV + report still produced.",
                        len(auto_candidates), HALT_THRESHOLD)
        else:
            log.info("(dry-run: writes skipped)")

    # Step 4 — manual review CSV.
    review_count = write_manual_review(rescues, review_path)
    log.info("manual review staged: %d rows → %s", review_count, review_path)

    # Step 5 — report.
    write_report(
        rescues=rescues, country_counts=country_counts,
        review_count=review_count, halted=halted,
    )

    written = sum(1 for r in rescues if r.write_succeeded)
    coll    = sum(1 for r in rescues if r.collision_with is not None)
    print()
    print("=== NEAR_MISS rescue summary ===")
    print(f"  country backfills        : {sum(country_counts.values())}")
    print(f"  rescue candidates        : {len(rescues)}")
    print(f"  auto-accept (pre-D018)   : {len(auto_candidates)}")
    print(f"  rescued (written)        : {written}")
    print(f"  D-018 collisions caught  : {coll}")
    print(f"  manual review staged     : {review_count}")
    print(f"  halted                   : {halted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
