"""Open Library client.

Two endpoints:
- Search: `GET /search.json?q=<title>&limit=10` returns a list of works
  with title, authors, cover id, publish year, page count.
- Covers are served from `https://covers.openlibrary.org/b/id/{id}-M.jpg`.

In-memory LRU cache so repeated searches are instant. No TTL — search
results are stable enough for a v1 admin tool. If we ever do anything
user-facing with this, we'd add a TTL.

All functions are sync (httpx.Client, not AsyncClient) so they're easy
to call from FastAPI route handlers without async/await noise.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import httpx

BASE_URL = os.environ.get("OPENLIBRARY_BASE_URL", "https://openlibrary.org")
TIMEOUT = 8.0


@lru_cache(maxsize=128)
def search_books_cached(query: str, limit: int = 10) -> tuple[dict, ...]:
    """Cached search. Returns a tuple of normalized result dicts.

    Each result has: key, title, author, cover_url, publish_year, page_count.
    `cover_url` may be None if the result has no cover id.
    """
    return tuple(_search_books(query, limit))


def _search_books(query: str, limit: int) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    params = {"q": q, "limit": max(1, min(limit, 20))}
    try:
        resp = httpx.get(
            f"{BASE_URL}/search.json",
            params=params,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return []

    docs = (resp.json() or {}).get("docs", []) or []
    out: list[dict[str, Any]] = []
    for d in docs:
        cover_id = d.get("cover_i")
        cover_url = (
            f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
            if cover_id
            else None
        )
        authors = d.get("author_name") or []
        out.append(
            {
                "key": d.get("key"),  # e.g. "/works/OL45804W"
                "title": d.get("title") or "(untitled)",
                "author": authors[0] if authors else "Unknown",
                "authors": authors,
                "cover_url": cover_url,
                "publish_year": d.get("first_publish_year"),
                "page_count": d.get("number_of_pages_median"),
            }
        )
    return out


def cover_url_for(cover_id: int | None, size: str = "M") -> str | None:
    """Build a cover URL for a known cover id. `size` is S/M/L."""
    if not cover_id:
        return None
    return f"https://covers.openlibrary.org/b/id/{cover_id}-{size}.jpg"


def normalize_work_key(key: str | None) -> str | None:
    """Open Library's `key` looks like '/works/OL45804W' or '/books/OL26375830M'.
    We just store it as-is; it's only used as a stable identifier."""
    return key or None
