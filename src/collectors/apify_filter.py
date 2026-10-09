"""Apify baseline filter — modular pass/reject rules + rejection logging.

Architecture:
  - FilterRule: a (name, check) pair. `check` is Callable[[dict], (bool, str|None)]
    where True = pass, False = reject (with a reason string).
  - BaselineFilter: an ordered list of FilterRules built from the YAML config.
    `evaluate(org)` runs every rule and returns the full list of failures
    (we don't short-circuit — we want to surface every reason an org was
    rejected so the operator can spot mis-tuned rules).

The org dict mirrors the `bucket` shape produced by
`apify_leads._group_by_org`:

    {
      "org_name":    str,
      "org_website": str,
      "org_meta":    {industry, founded_year, size, country, ...},
      "leads":       [<actor item>, ...],
    }

Rule TYPES live here (the five `_make_*` factories below). Rule VALUES
live in `config/sources/apify_baseline.yaml`. To add a new rule type,
write a new factory + register it in `_BUILTIN_RULES` + add a config key.

Self-test:
    python -m src.collectors.apify_filter --self-test
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config"
    / "sources"
    / "apify_baseline.yaml"
)

# A rule's check returns (passed, reason). When passed=True, reason is None.
CheckResult = tuple[bool, str | None]
CheckFn = Callable[[dict[str, Any]], CheckResult]


@dataclass(frozen=True)
class FilterRule:
    name: str
    check: CheckFn

    def evaluate(self, org: dict[str, Any]) -> CheckResult:
        return self.check(org)


# ── Helpers — read fields tolerantly ────────────────────────────────────────


def _meta(org: dict[str, Any]) -> dict[str, Any]:
    return org.get("org_meta") or {}


def _str(v: Any) -> str:
    """Best-effort string from possibly-None / int / list. Empty-string for
    nullish; the actor sometimes stores 'founded_year' as either int or str."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v).strip()


# ── Rule factories ──────────────────────────────────────────────────────────


def _make_country_in_allowlist(allowed: list[str]) -> FilterRule:
    allowed_lower = {c.strip().lower() for c in allowed if c.strip()}

    def check(org: dict[str, Any]) -> CheckResult:
        country = _str(_meta(org).get("country"))
        if not country:
            # Missing country is treated as a failure of THIS rule, since the
            # geographic predicate is the highest-leverage filter.
            return (False, "country_in_allowlist: country missing from org_meta")
        if country.lower() in allowed_lower:
            return (True, None)
        return (False, f"country_in_allowlist: {country!r} not in allowlist")

    return FilterRule("country_in_allowlist", check)


def _make_size_in_allowlist(allowed: list[str]) -> FilterRule:
    allowed_set = {s.strip() for s in allowed if s.strip()}

    def check(org: dict[str, Any]) -> CheckResult:
        size = _str(_meta(org).get("size"))
        if not size:
            # Missing size — don't reject (Apollo doesn't always report it).
            return (True, None)
        if size in allowed_set:
            return (True, None)
        return (False, f"size_in_allowlist: {size!r} not in allowlist")

    return FilterRule("size_in_allowlist", check)


def _make_founded_year_at_or_after(min_year: int) -> FilterRule:
    def check(org: dict[str, Any]) -> CheckResult:
        raw = _meta(org).get("founded_year")
        if raw in (None, "", "<UNKNOWN>"):
            # Missing founded_year is permissive — pass.
            return (True, None)
        try:
            year = int(str(raw).strip())
        except (TypeError, ValueError):
            return (True, None)  # Unparseable → permissive.
        if year >= min_year:
            return (True, None)
        return (False, f"founded_year_at_or_after: {year} < {min_year}")

    return FilterRule("founded_year_at_or_after", check)


def _make_industry_not_in_excluded(excluded: list[str]) -> FilterRule:
    excluded_lower = [e.strip().lower() for e in excluded if e.strip()]

    def check(org: dict[str, Any]) -> CheckResult:
        industry = _str(_meta(org).get("industry")).lower()
        if not industry:
            return (True, None)
        for bad in excluded_lower:
            if bad in industry:
                return (
                    False,
                    f"industry_not_in_excluded: industry {industry!r} matches {bad!r}",
                )
        return (True, None)

    return FilterRule("industry_not_in_excluded", check)


def _make_description_no_noise_patterns(patterns: list[str]) -> FilterRule:
    # Compile to lowercase substring matches. We don't use regex here — the
    # patterns are intentionally plain fragments so the YAML stays
    # operator-editable without regex literacy.
    norm_patterns = [p.strip().lower() for p in patterns if p.strip()]

    def check(org: dict[str, Any]) -> CheckResult:
        desc = _str(_meta(org).get("description")).lower()
        if not desc:
            return (True, None)
        for pat in norm_patterns:
            if pat in desc:
                return (
                    False,
                    f"description_no_noise_patterns: description matches {pat!r}",
                )
        return (True, None)

    return FilterRule("description_no_noise_patterns", check)


# Registry of rule-name → factory(config_value). Adding a rule type means
# adding a factory above and an entry here.
_BUILTIN_RULES: dict[str, Callable[[Any], FilterRule]] = {
    "allowed_countries":          _make_country_in_allowlist,
    "allowed_sizes":              _make_size_in_allowlist,
    "min_founded_year":           _make_founded_year_at_or_after,
    "excluded_industries":        _make_industry_not_in_excluded,
    "noise_description_patterns": _make_description_no_noise_patterns,
}


# ── BaselineFilter ──────────────────────────────────────────────────────────


@dataclass
class FilterResult:
    passed: bool
    failed_rules: list[str]              # rule names that returned False
    reasons: list[str]                   # human-readable reasons (one per failed rule)


class BaselineFilter:
    """Run a fixed set of rules against an org dict.

    Construct via `BaselineFilter.from_yaml()` for the production config,
    or pass `rules=[...]` directly for tests.
    """

    def __init__(self, rules: list[FilterRule]) -> None:
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: Path | str = CONFIG_PATH) -> "BaselineFilter":
        with open(path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        rules: list[FilterRule] = []
        for key, factory in _BUILTIN_RULES.items():
            if key in cfg and cfg[key] is not None:
                rules.append(factory(cfg[key]))
        return cls(rules)

    def evaluate(self, org: dict[str, Any]) -> FilterResult:
        failed_rules: list[str] = []
        reasons: list[str] = []
        for rule in self.rules:
            ok, reason = rule.evaluate(org)
            if not ok:
                failed_rules.append(rule.name)
                reasons.append(reason or rule.name)
        return FilterResult(
            passed=(not failed_rules),
            failed_rules=failed_rules,
            reasons=reasons,
        )


# ── Self-test ───────────────────────────────────────────────────────────────


def _self_test() -> int:
    """Inline test cases. No pytest dep — runs via `python -m`."""

    def org(meta: dict[str, Any]) -> dict[str, Any]:
        return {"org_name": "Test Co", "org_website": "", "org_meta": meta, "leads": []}

    cases: list[tuple[str, FilterRule, dict[str, Any], bool]] = []

    # --- country_in_allowlist (5 cases) ---
    r_country = _make_country_in_allowlist(["United States", "Ukraine", "Germany"])
    cases.append(("country: US passes",        r_country, org({"country": "United States"}), True))
    cases.append(("country: Germany passes",   r_country, org({"country": "Germany"}),       True))
    cases.append(("country: case-insensitive", r_country, org({"country": "ukraine"}),       True))
    cases.append(("country: India rejected",   r_country, org({"country": "India"}),         False))
    cases.append(("country: missing rejected", r_country, org({}),                            False))

    # --- size_in_allowlist (4 cases) ---
    r_size = _make_size_in_allowlist(["2 - 10", "11 - 50", "51 - 200", "201 - 500"])
    cases.append(("size: 11-50 passes",        r_size, org({"size": "11 - 50"}),  True))
    cases.append(("size: 0-1 rejected",        r_size, org({"size": "0 - 1"}),    False))
    cases.append(("size: 1001-5000 rejected",  r_size, org({"size": "1001 - 5000"}), False))
    cases.append(("size: missing passes",      r_size, org({}),                    True))

    # --- founded_year_at_or_after (5 cases) ---
    r_year = _make_founded_year_at_or_after(2008)
    cases.append(("year: 2008 passes",         r_year, org({"founded_year": 2008}),  True))
    cases.append(("year: 2020 passes",         r_year, org({"founded_year": "2020"}), True))
    cases.append(("year: 1995 rejected",       r_year, org({"founded_year": "1995"}), False))
    cases.append(("year: missing passes",      r_year, org({}),                       True))
    cases.append(("year: <UNKNOWN> passes",    r_year, org({"founded_year": "<UNKNOWN>"}), True))

    # --- industry_not_in_excluded (3 cases) ---
    r_industry = _make_industry_not_in_excluded(
        ["Staffing and Recruiting", "Real Estate"]
    )
    cases.append(("industry: defense passes",         r_industry,
                  org({"industry": "Defense and Space Manufacturing"}), True))
    cases.append(("industry: staffing rejected",      r_industry,
                  org({"industry": "Staffing and Recruiting"}),         False))
    cases.append(("industry: substring matches",      r_industry,
                  org({"industry": "Commercial Real Estate Services"}), False))

    # --- description_no_noise_patterns (3 cases) ---
    r_desc = _make_description_no_noise_patterns(
        ["staffing agency", "marketing agency"]
    )
    cases.append(("desc: clean passes",               r_desc,
                  org({"description": "We build counter-UAS systems."}), True))
    cases.append(("desc: 'staffing agency' rejected", r_desc,
                  org({"description": "We are a leading staffing agency for tech."}), False))
    cases.append(("desc: missing passes",             r_desc, org({}), True))

    failures: list[str] = []
    for label, rule, org_in, want_pass in cases:
        ok, reason = rule.evaluate(org_in)
        if ok is want_pass:
            print(f"  PASS  {label}")
        else:
            failures.append(f"  FAIL  {label}: got passed={ok!r} reason={reason!r}")
            print(failures[-1])

    # --- BaselineFilter integration test (1 case) ---
    bf = BaselineFilter([r_country, r_size, r_year, r_industry, r_desc])
    good = org({
        "country": "United States", "size": "11 - 50",
        "founded_year": 2020, "industry": "Defense and Space Manufacturing",
        "description": "We build counter-UAS systems.",
    })
    bad = org({
        "country": "India", "size": "0 - 1",
        "founded_year": 1995, "industry": "Real Estate",
        "description": "We are a marketing agency.",
    })
    res_good = bf.evaluate(good)
    res_bad  = bf.evaluate(bad)
    if res_good.passed and not res_good.failed_rules:
        print("  PASS  integration: clean org passes all 5 rules")
    else:
        failures.append(f"  FAIL  integration good: {res_good!r}")
        print(failures[-1])
    if (not res_bad.passed) and len(res_bad.failed_rules) == 5:
        print(f"  PASS  integration: bad org fails all 5 rules ({res_bad.failed_rules})")
    else:
        failures.append(f"  FAIL  integration bad: {res_bad!r}")
        print(failures[-1])

    n_total = len(cases) + 2
    print(f"\n{n_total - len(failures)}/{n_total} cases passed")
    return 0 if not failures else 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apify baseline filter")
    p.add_argument("--self-test", action="store_true",
                   help="Run inline test cases (no pytest dep).")
    p.add_argument("--show-config", action="store_true",
                   help="Load YAML and print the constructed rule names.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.self_test:
        return _self_test()
    if args.show_config:
        bf = BaselineFilter.from_yaml()
        print(f"loaded {len(bf.rules)} rules from {CONFIG_PATH}")
        for r in bf.rules:
            print(f"  - {r.name}")
        return 0
    print("nothing to do — pass --self-test or --show-config", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
