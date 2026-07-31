"""Open Library client.

Two endpoints:
- Search: `GET /search.json?q=<title>&limit=10` returns a list of works
  with title, authors, cover id, publish year, page count.
- Covers are served from `https://covers.openlibrary.org/b/id/{id}-M.jpg`.

In-memory cache so repeated searches are instant, with a TTL and a size
cap. Deliberately not `lru_cache`: an outage used to be memoized as an
empty result list and served forever after, which reads on screen as
"Open Library doesn't have this book."

All functions are sync (httpx.Client, not AsyncClient) so they're easy
to call from FastAPI route handlers without async/await noise.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

BASE_URL = os.environ.get("OPENLIBRARY_BASE_URL", "https://openlibrary.org")
TIMEOUT = 8.0

# Long enough that clicking around a search doesn't re-hit the API, short
# enough that a new edition shows up the same afternoon.
CACHE_TTL_SECONDS = 15 * 60
CACHE_MAX_ENTRIES = 128

_cache: dict[tuple[str, int], tuple[float, tuple[dict, ...]]] = {}


def clear_cache() -> None:
    """Drop everything memoized. Used by tests."""
    _cache.clear()


def search_books_cached(query: str, limit: int = 10) -> tuple[dict, ...]:
    """Cached search. Returns a tuple of normalized result dicts.

    Each result has: key, title, author, cover_url, publish_year, page_count.
    `cover_url` may be None if the result has no cover id.

    Only non-empty results are cached, and network failures propagate to the
    caller rather than being stored as "no matches" — a search that fails should
    say so, and should work again the moment Open Library does.
    """
    key = (query.strip(), limit)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None:
        cached_at, results = hit
        if now - cached_at < CACHE_TTL_SECONDS:
            return results
        del _cache[key]

    results = tuple(_search_books(query, limit))
    if results:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _evict(now)
        _cache[key] = (now, results)
    return results


def _evict(now: float) -> None:
    """Make room: expired entries first, then the oldest one standing."""
    for key in [k for k, (at, _) in _cache.items() if now - at >= CACHE_TTL_SECONDS]:
        del _cache[key]
    if len(_cache) >= CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        del _cache[oldest]


def _search_books(query: str, limit: int) -> list[dict[str, Any]]:
    """Hit Open Library. Raises `httpx.HTTPError` if the call doesn't land."""
    q = (query or "").strip()
    if not q:
        return []
    params = {"q": q, "limit": max(1, min(limit, 20))}
    resp = httpx.get(
        f"{BASE_URL}/search.json",
        params=params,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()

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
