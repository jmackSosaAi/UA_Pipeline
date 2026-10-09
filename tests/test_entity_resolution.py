from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.entity_resolution import (
    match_or_resolve_company,
    normalize_company_name,
    normalize_website,
)


def _db(rows: list[tuple]) -> Path:
    """Build a test DB. Rows may be the legacy 4-tuple (name,
    name_latin, hq_country, linkedin_url) OR the 5-tuple form that
    appends website."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = Path(tmp.name)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_latin TEXT,
            hq_country TEXT,
            linkedin_url TEXT,
            website TEXT
        )
        """
    )
    padded = [tuple(list(row) + [None] * (5 - len(row))) for row in rows]
    conn.executemany(
        "INSERT INTO companies (name, name_latin, hq_country, linkedin_url, website) "
        "VALUES (?, ?, ?, ?, ?)",
        padded,
    )
    conn.commit()
    conn.close()
    return path


class EntityResolutionTests(unittest.TestCase):
    def test_exact_linkedin_match(self) -> None:
        path = _db([("Anduril Industries", None, "United States", "https://www.linkedin.com/company/anduril")])
        company_id, diagnostic = match_or_resolve_company(
            "Different Name",
            linkedin_url="https://www.linkedin.com/company/anduril",
            db_path=path,
        )
        self.assertEqual(company_id, 1)
        self.assertEqual(diagnostic["match_type"], "linkedin_url")

    def test_exact_name_latin_match(self) -> None:
        path = _db([("ТОВ Приклад", "Example Defense", "Ukraine", None)])
        company_id, diagnostic = match_or_resolve_company("Example Defense", country="Ukraine", db_path=path)
        self.assertEqual(company_id, 1)
        self.assertEqual(diagnostic["match_type"], "name_latin")

    def test_suffix_variation_match(self) -> None:
        path = _db([("Shield AI Inc.", None, "United States", None)])
        company_id, diagnostic = match_or_resolve_company("Shield AI LLC", country="United States", db_path=path)
        self.assertEqual(company_id, 1)
        self.assertEqual(diagnostic["match_type"], "normalized_name")

    def test_case_and_whitespace_match(self) -> None:
        path = _db([("  Helsing GmbH ", None, "Germany", None)])
        company_id, _ = match_or_resolve_company("HELSING", country="Germany", db_path=path)
        self.assertEqual(company_id, 1)

    def test_country_disambiguation(self) -> None:
        path = _db(
            [
                ("Atlas Robotics Ltd", None, "United Kingdom", None),
                ("Atlas Robotics LLC", None, "United States", None),
            ]
        )
        company_id, _ = match_or_resolve_company("Atlas Robotics", country="United States", db_path=path)
        self.assertEqual(company_id, 2)

    def test_near_miss_below_threshold_returns_none(self) -> None:
        path = _db([("Blue Canyon Technologies", None, "United States", None)])
        company_id, diagnostic = match_or_resolve_company("Blue Ocean Robotics", country="United States", db_path=path)
        self.assertIsNone(company_id)
        self.assertIn(diagnostic["match_type"], {"low_confidence", "new"})

    def test_ambiguous_fuzzy_candidates_return_none(self) -> None:
        path = _db(
            [
                ("Nova Robotics Platform Inc", None, None, None),
                ("Nova Robotics Systems LLC", None, None, None),
            ]
        )
        company_id, diagnostic = match_or_resolve_company("Nova Robotics", db_path=path)
        self.assertIsNone(company_id)
        self.assertEqual(diagnostic["match_type"], "ambiguous")

    def test_normalize_company_name(self) -> None:
        self.assertEqual(normalize_company_name(" Shield AI, Inc. "), "shield ai")


class NormalizeWebsiteTests(unittest.TestCase):
    def test_strip_scheme(self) -> None:
        self.assertEqual(normalize_website("https://example.com"), "example.com")
        self.assertEqual(normalize_website("http://example.com"), "example.com")
        self.assertEqual(normalize_website("//example.com"), "example.com")

    def test_strip_www_prefix(self) -> None:
        self.assertEqual(normalize_website("https://www.example.com"), "example.com")
        self.assertEqual(normalize_website("www.example.com"), "example.com")

    def test_strip_trailing_slash(self) -> None:
        self.assertEqual(normalize_website("https://example.com/"), "example.com")
        self.assertEqual(normalize_website("https://example.com/path/"), "example.com/path")

    def test_strip_query_and_fragment(self) -> None:
        self.assertEqual(normalize_website("https://example.com?utm=foo"), "example.com")
        self.assertEqual(normalize_website("https://example.com/path#hash"), "example.com/path")
        self.assertEqual(normalize_website("https://www.example.com/?ref=x"), "example.com")

    def test_case_insensitive(self) -> None:
        self.assertEqual(normalize_website("HTTPS://WWW.Example.COM/"), "example.com")
        self.assertEqual(normalize_website("ExAmPle.CoM"), "example.com")

    def test_null_or_empty(self) -> None:
        self.assertEqual(normalize_website(None), "")
        self.assertEqual(normalize_website(""), "")
        self.assertEqual(normalize_website("   "), "")

    def test_rejects_non_url_strings(self) -> None:
        # No dot → not a website
        self.assertEqual(normalize_website("just-a-name"), "")
        self.assertEqual(normalize_website("invalid"), "")


class WebsiteMatchTests(unittest.TestCase):
    def test_exact_website_match(self) -> None:
        path = _db([
            ("Picogrid Inc.", None, "United States", None, "https://picogrid.com"),
        ])
        company_id, diagnostic = match_or_resolve_company(
            "Picogrid", website="https://picogrid.com", db_path=path,
        )
        self.assertEqual(company_id, 1)
        self.assertEqual(diagnostic["match_type"], "website")

    def test_www_variation_matches(self) -> None:
        path = _db([
            ("ACME Robotics", None, None, None, "https://acme.com"),
        ])
        company_id, diagnostic = match_or_resolve_company(
            "Unrelated Name", website="https://www.acme.com/", db_path=path,
        )
        self.assertEqual(company_id, 1)
        self.assertEqual(diagnostic["match_type"], "website")

    def test_http_vs_https(self) -> None:
        path = _db([
            ("Atlas Space", None, None, None, "http://atlas.space"),
        ])
        company_id, _ = match_or_resolve_company(
            "Atlas Space Operations", website="https://atlas.space", db_path=path,
        )
        self.assertEqual(company_id, 1)

    def test_trailing_slash_normalised(self) -> None:
        path = _db([
            ("Helsing", None, "Germany", None, "https://helsing.ai"),
        ])
        company_id, _ = match_or_resolve_company(
            "HELSING", country="Germany", website="https://helsing.ai/", db_path=path,
        )
        self.assertEqual(company_id, 1)

    def test_case_insensitive_match(self) -> None:
        path = _db([
            ("Example Co", None, None, None, "https://example.com"),
        ])
        company_id, _ = match_or_resolve_company(
            "Example Co", website="HTTPS://WWW.EXAMPLE.COM", db_path=path,
        )
        self.assertEqual(company_id, 1)

    def test_query_string_stripped(self) -> None:
        path = _db([
            ("Snowpack", None, None, None, "https://snowpack.eu"),
        ])
        company_id, _ = match_or_resolve_company(
            "Snowpack", website="https://snowpack.eu?utm_source=iqt", db_path=path,
        )
        self.assertEqual(company_id, 1)

    def test_null_website_falls_through_to_name(self) -> None:
        # Source has no website → website slot is skipped, name match path runs.
        path = _db([
            ("Shield AI", None, "United States", None, "https://shield.ai"),
        ])
        company_id, diagnostic = match_or_resolve_company(
            "Shield AI", country="United States", website=None, db_path=path,
        )
        self.assertEqual(company_id, 1)
        # Should match via name_latin (or similar), NOT website.
        self.assertNotEqual(diagnostic["match_type"], "website")

    def test_db_row_with_no_website_does_not_false_match(self) -> None:
        # Source has a website, but no DB row has one — must not match.
        path = _db([
            ("Picogrid", None, None, None, None),
            ("Atlas Space", None, None, None, None),
        ])
        company_id, diagnostic = match_or_resolve_company(
            "Brand New Company", website="https://example.com", db_path=path,
        )
        # Match fails on website (no candidates); name "Brand New
        # Company" doesn't match either of those names either.
        self.assertIsNone(company_id)
        self.assertEqual(diagnostic["match_type"], "new")

    def test_country_disambiguates_website_match(self) -> None:
        # Two real companies happen to share a parent domain (e.g.
        # subsidiaries). Country filter applies to website matches too.
        path = _db([
            ("Atlas UK Ltd", None, "United Kingdom", None, "https://atlas.com"),
            ("Atlas US LLC", None, "United States", None, "https://atlas.com"),
        ])
        company_id, diagnostic = match_or_resolve_company(
            "Atlas", country="United States", website="https://atlas.com", db_path=path,
        )
        self.assertEqual(company_id, 2)
        self.assertEqual(diagnostic["match_type"], "website")

    def test_website_priority_over_name(self) -> None:
        # DB has a row with different name but matching website AND a
        # row with matching name but different website. Website slot
        # fires first → returns row 1, not row 2.
        path = _db([
            ("Old Name Inc", None, None, None, "https://newco.com"),
            ("New Co", None, None, None, "https://oldsite.com"),
        ])
        company_id, diagnostic = match_or_resolve_company(
            "New Co", website="https://newco.com", db_path=path,
        )
        self.assertEqual(company_id, 1)
        self.assertEqual(diagnostic["match_type"], "website")

    def test_linkedin_url_still_wins_when_both_present(self) -> None:
        # linkedin_url match should win even when website also matches a different row.
        path = _db([
            ("Company A", None, None, "https://www.linkedin.com/company/match-via-li/", "https://a.com"),
            ("Company B", None, None, None, "https://b.com"),
        ])
        company_id, diagnostic = match_or_resolve_company(
            "Different Name",
            linkedin_url="https://www.linkedin.com/company/match-via-li/",
            website="https://b.com",
            db_path=path,
        )
        self.assertEqual(company_id, 1)
        self.assertEqual(diagnostic["match_type"], "linkedin_url")


if __name__ == "__main__":
    unittest.main()
