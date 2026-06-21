#!/usr/bin/env python3
"""
Scrapes the most recent completed domestic weekend box office chart from
Box Office Mojo and writes it to data/boxoffice.json for the Dakboard
widget to consume via raw.githubusercontent.com.

No API key required — Box Office Mojo doesn't offer a free official API,
so this reads their public chart pages directly. Scraping happens here,
server-side, on a schedule; the browser widget never touches
boxofficemojo.com directly, which avoids CORS entirely.

Two-step fetch:
  1. GET /weekend/  — the yearly index of weekends, each row showing total
     gross for that weekend (or "-" if it hasn't happened/closed yet).
     We pick the most recent row that actually has a gross figure.
  2. GET /weekend/<id>/  — that specific weekend's ranked film-by-film
     chart, which is what we actually want for the widget.

Dependencies: requests, beautifulsoup4 (installed by the GitHub Action).
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

INDEX_URL = "https://www.boxofficemojo.com/weekend/"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "boxoffice.json")
HEADERS = {
    # A normal browser UA; Box Office Mojo's chart pages are public,
    # this just avoids being blocked as a bare script.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
MAX_ROWS = 15  # how many ranked titles to keep

# Poster lookup via TMDb (The Movie Database) — free for non-commercial use,
# requires only a free API key (no payment). Box Office Mojo's own chart
# page has no poster images, so this is a second, optional lookup per film.
# If TMDB_API_KEY isn't set, posters are simply skipped — the rest of the
# widget still works fine without them.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w185"  # small size, right for a list thumbnail


def fetch_poster_url(title):
    """Look up a film's poster on TMDb by title. Returns a full image URL,
    or None if not found / lookup fails / no API key configured. Never
    raises — a missing poster for one film shouldn't break the whole fetch."""
    if not TMDB_API_KEY:
        return None
    try:
        resp = requests.get(
            TMDB_SEARCH_URL,
            params={"api_key": TMDB_API_KEY, "query": title},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        poster_path = results[0].get("poster_path")
        return f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
    except Exception as e:
        print(f"  poster lookup failed for '{title}': {e}", file=sys.stderr)
        return None


def money_to_int(text):
    """'$54,336,626' -> 54336626. Returns None if not parseable (e.g. '-')."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else None


def pct_to_str(text):
    """Normalize '-67.7%' / '+624.3%' / '-' into a clean string or None."""
    if not text or text.strip() == "-":
        return None
    return text.strip()


def get(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_latest_weekend_url():
    """Parse the yearly weekend index and return the URL + date label of the
    most recent weekend that actually has chart data (skips upcoming
    weekends, which show as all dashes)."""
    html = get(INDEX_URL)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        raise RuntimeError("No table found on weekend index page — markup may have changed.")

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue
        date_link = cells[0].find("a")
        if not date_link or not date_link.get("href"):
            continue
        gross_text = cells[1].get_text(strip=True)
        if gross_text and gross_text != "-":
            # First row with real data is the most recent completed weekend
            # (table is sorted most-recent-first).
            full_url = urljoin(INDEX_URL, date_link["href"])
            date_label = date_link.get_text(strip=True)
            return full_url, date_label

    raise RuntimeError("Could not find any weekend with chart data on index page.")


def parse_weekend_chart(html):
    """Parse a Box Office Mojo weekend chart page.

    Verified column layout (as of June 2026):
    0=Rank 1=LW 2=Release 3=Gross 4=%±LW 5=Theaters 6=Change
    7=Average 8=TotalGross 9=Weeks 10=Distributor
    """
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find("h1")
    chart_title = title_el.get_text(strip=True) if title_el else None

    table = soup.find("table")
    if not table:
        raise RuntimeError("No table found on weekend chart page — markup may have changed.")

    films = []
    skipped_short_rows = 0
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 10:
            skipped_short_rows += 1
            continue  # header row or malformed row

        rank_text = cells[0].get_text(strip=True)
        if not rank_text.isdigit():
            continue

        title = cells[2].get_text(strip=True)
        gross = money_to_int(cells[3].get_text(strip=True))
        change = pct_to_str(cells[4].get_text(strip=True))
        theaters_text = cells[5].get_text(strip=True).replace(",", "")
        total_gross = money_to_int(cells[8].get_text(strip=True))
        weeks_text = cells[9].get_text(strip=True)
        distributor = cells[10].get_text(strip=True) if len(cells) > 10 else None

        films.append({
            "rank": int(rank_text),
            "title": title,
            "weekendGross": gross,
            "changePct": change,
            "theaters": int(theaters_text) if theaters_text.isdigit() else None,
            "totalGross": total_gross,
            "weeksInRelease": int(weeks_text) if weeks_text.isdigit() else None,
            "distributor": distributor if distributor and distributor != "-" else None,
            "isNew": (weeks_text == "1"),
        })

        if len(films) >= MAX_ROWS:
            break

    if not films:
        raise RuntimeError(
            f"Parsed 0 films from chart table (skipped {skipped_short_rows} short/header rows) "
            "— Box Office Mojo's markup may have changed. Check column positions in parse_weekend_chart()."
        )

    return chart_title, films


def main():
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    previous = None
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r") as f:
                previous = json.load(f)
        except (json.JSONDecodeError, OSError):
            previous = None

    try:
        weekend_url, weekend_label = find_latest_weekend_url()
        html = get(weekend_url)
        chart_title, films = parse_weekend_chart(html)

        if TMDB_API_KEY:
            print(f"Looking up posters for {len(films)} films via TMDb...")
            for film in films:
                film["poster"] = fetch_poster_url(film["title"])
        else:
            print("TMDB_API_KEY not set — skipping poster lookup (widget will show fallback icons).")
            for film in films:
                film["poster"] = None

        output = {
            "weekendLabel": weekend_label,
            "chartTitle": chart_title,
            "films": films,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "source": "boxofficemojo.com",
            "posterSource": "themoviedb.org" if TMDB_API_KEY else None,
            "status": "ok",
        }
        print(f"Fetched {len(films)} films for: {chart_title} ({weekend_label})")

    except Exception as e:
        print(f"WARNING: fetch failed: {e}", file=sys.stderr)
        if previous:
            output = previous
            output["status"] = "stale"
            output["lastError"] = str(e)
            output["lastErrorAt"] = datetime.now(timezone.utc).isoformat()
            print("Falling back to previous cached data.")
        else:
            output = {
                "weekendLabel": None,
                "chartTitle": None,
                "films": [],
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "source": "boxofficemojo.com",
                "status": "error",
                "lastError": str(e),
            }
            with open(OUTPUT_PATH, "w") as f:
                json.dump(output, f, indent=2)
            sys.exit(1)

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()

