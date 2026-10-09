"""Probe candidate RSS feed URLs and print a status table.

Tries each URL with feedparser using a desktop User-Agent (some hosts 403
on the default feedparser UA). Reports HTTP status, bozo flag, entry count,
and a verdict: working / dead / issues.

Usage:
    python -m scripts.probe_feeds
"""

import concurrent.futures as futures
import time
from typing import Optional

import feedparser

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

CANDIDATES: list[tuple[str, str, str]] = [
    # (label, group, url)
    ("Defense Daily",                 "trade",     "https://www.defensedaily.com/feed/"),
    ("Aviation Week",                 "trade",     "https://aviationweek.com/rss.xml"),
    ("Air & Space Forces Magazine",   "trade",     "https://www.airandspaceforces.com/feed/"),
    ("Army Recognition",              "trade",     "https://www.armyrecognition.com/rss/news_defence_security.html"),
    ("Naval Technology",              "trade",     "https://www.naval-technology.com/feed/"),
    ("Army Technology",               "trade",     "https://www.army-technology.com/feed/"),
    ("Air Force Technology",          "trade",     "https://www.airforce-technology.com/feed/"),
    ("Defense Update",                "trade",     "https://defense-update.com/feed"),
    ("Janes News (alt)",              "trade",     "https://www.janes.com/feeds/news.xml"),

    ("War on the Rocks",              "policy",    "https://warontherocks.com/feed/"),
    ("Atlantic Council",              "policy",    "https://www.atlanticcouncil.org/feed/"),
    ("CNAS",                          "policy",    "https://www.cnas.org/rss"),
    ("The National Interest",         "policy",    "https://nationalinterest.org/feed"),
    ("Foreign Policy",                "policy",    "https://foreignpolicy.com/feed/"),

    ("Kyiv Independent",              "ukraine",   "https://kyivindependent.com/rss/"),
    ("Ukrainska Pravda (EN)",         "ukraine",   "https://www.pravda.com.ua/eng/rss/"),
    ("Defense Express (alt)",         "ukraine",   "https://en.defence-ua.com/feed"),

    ("Reuters Defense",               "wire",      "https://www.reuters.com/world/defense/feed/"),
    ("WaPo National Security",        "wire",      "https://feeds.washingtonpost.com/rss/national/national-security"),
]


def probe(label: str, group: str, url: str) -> dict:
    t0 = time.perf_counter()
    parsed = feedparser.parse(url, agent=_UA)
    elapsed = time.perf_counter() - t0
    n = len(parsed.entries or [])
    bozo = bool(parsed.bozo)
    status = parsed.get("status")
    bozo_exc: Optional[str] = None
    if bozo:
        try:
            bozo_exc = type(parsed.bozo_exception).__name__
        except Exception:
            bozo_exc = "?"
    if n > 0 and not bozo:
        verdict = "working"
    elif n > 0 and bozo:
        verdict = "issues"          # parsed something but with warnings
    elif status == 404:
        verdict = "404"
    elif status in (301, 302, 308) and n == 0:
        verdict = "redirected"
    elif status == 403:
        verdict = "403"
    elif status is None:
        verdict = "no_response"
    else:
        verdict = "empty"
    return {
        "label": label, "group": group, "url": url,
        "status": status, "bozo": bozo, "bozo_exc": bozo_exc,
        "entries": n, "verdict": verdict, "elapsed": elapsed,
    }


def main() -> None:
    print(f"Probing {len(CANDIDATES)} candidate feeds...\n")
    rows: list[dict] = []
    with futures.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda c: probe(*c), CANDIDATES):
            rows.append(r)
    rows.sort(key=lambda r: (r["group"], r["label"].lower()))

    print(f"{'#':3} {'GROUP':8} {'NAME':32} {'STATUS':6} {'BOZO':5} {'ENT':>4}  VERDICT  TIME")
    print("─" * 95)
    for i, r in enumerate(rows, 1):
        bozo_disp = (r["bozo_exc"] or "y") if r["bozo"] else "—"
        st = str(r["status"] or "—")
        print(
            f"{i:3} {r['group']:8} {r['label'][:32]:32} {st:6} {bozo_disp[:5]:5} "
            f"{r['entries']:>4}  {r['verdict']:10} {r['elapsed']:.1f}s"
        )

    print("\nVerdict counts:")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    for v, n in sorted(counts.items()):
        print(f"  {v}: {n}")

    print("\nWorking URLs (suitable for FEEDS):")
    for r in rows:
        if r["verdict"] == "working":
            print(f"  {r['label']:34} → {r['url']}")


if __name__ == "__main__":
    main()
