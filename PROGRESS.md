# Build Progress — 5418 Book Club

> **Spec:** `~/.openclaw/workspace/spec-bookclub-v1.md`
> **Started:** 2026-06-30 23:52 CDT
> **Pause-safe:** server runs locally on `http://127.0.0.1:8001`; the DB is at `data/bookclub.db`.

## Phase 1 — Scaffold + landing page ✅ DONE

**Built (930 LOC total):**
- `app.py` (73) — FastAPI app with `/` (landing) and `/healthz` (sanity check). `init_db` + `seed` run on startup.
- `db.py` (443) — SQLite layer: schema, all CRUD helpers, seed function.
- `seed.py` (9) — CLI helper to (re-)seed the DB.
- `templates/base.html` (40) — Layout: header with brand + admin nav slot, footer, content block.
- `templates/landing.html` (91) — Hero card (current book with cover), upcoming meetings grid, past books list.
- `static/style.css` (274) — Bookish warm-cream theme, sienna accent, serif headings, sans body, mobile responsive.
- `.env.example`, `.gitignore`, `requirements.txt` — config.

**Verified:**
- `uvicorn app:app` boots on `http://127.0.0.1:8001`, no errors.
- `/healthz` returns `{"status":"ok","club":"5418 Book Club","counts":{"books":1,"meetings":1,...}}` with seeded data.
- `/` renders 1 current book (Secret History) + 1 meeting (today) + 1 past book (Vanishing Half) when seeded with extras.
- Open Library cover URLs return 302 → CDN (normal; browsers follow).
- `/static/style.css` returns HTTP 200, 6569 bytes.
- `/rsvp`, `/login`, `/admin` all 404 as expected (Phase 2/3 work).

**One bug found + fixed:**
- `db.add_book()` was missing a `finished_on` kwarg. Past books couldn't be seeded. Added.

**Seed currently in DB:**
- Current book: *The Secret History* by Donna Tartt (started today, no read-by yet).
- 4 meetings: today (Oz's place) + 7/14/21 days out, all 7pm TBD.
- 1 past book: *The Vanishing Half* by Brit Bennett (finished 2025-05-12).
- 0 RSVPs, 0 members — fill these in via admin (Phase 2+).

## Phase 2 — Auth + admin book management ✅ DONE

**Built (724 LOC this phase, 1,654 total):**
- `auth.py` (127) — bcrypt passcode check, signed cookie via `itsdangerous` (30-day TTL), `require_admin` FastAPI dependency, `is_admin(request)` template helper, HttpOnly + SameSite=Lax cookie flags.
- `books.py` (87) — Open Library search via `httpx`, `@lru_cache(maxsize=128)` on results, returns normalized dicts with cover URL built from `cover_i`.
- `app.py` (251) — extended with `/login`, `POST /login`, `POST /logout`, `/admin` (home), `/admin/book` (current), `/admin/book/search`, `POST /admin/book` (set), `POST /admin/book/finish`. Plus `load_dotenv` so `.env` is picked up automatically.
- 5 new templates: `login.html` (31), `admin/base.html` (15), `admin/home.html` (44), `admin/book.html` (52), `admin/book_search.html` (44).
- `style.css` (420) — added admin nav, tile grid, search row, book-current block, login form card, error banner. Mobile pass.
- `requirements.txt` — added `python-multipart` (FastAPI form parsing).

**One bug found + fixed during build:**
- `db.add_book()` was missing `finished_on` (from Phase 1). Phase 2 re-surfaced it via the finish flow → fixed in Phase 1 retroactively, but actually the Phase 1 fix was already in place. What I caught this session: wrong-passcode was returning HTTP 200 instead of 401 because `_render` didn't accept `status_code`. Fixed by passing it through to `TemplateResponse`.

**Auth flow (verified end-to-end with curl, 13/13 passed):**
1. `GET /admin` no cookie → 302 to `/login?next=/admin` ✓
2. `GET /admin/book` no cookie → 302 to `/login?next=/admin/book` ✓
3. `GET /login` → 200, form rendered ✓
4. `POST /login` wrong passcode → **401** with "Wrong passcode. Try again." inline ✓
5. `POST /login` correct passcode (5418) → **303** to `/admin`, cookie set ✓
6. `GET /admin` with cookie → 200, shows "Now reading: The Secret History" tile ✓
7. `GET /admin/book` with cookie → 200, full current-book card ✓
8. `GET /admin/book/search?q=station+eleven` → 10 real results from Open Library, first match: *Station Eleven* by Emily St. John Mandel, 2014, 333 pages ✓
9. `POST /admin/book` with selected result → **303** to `/admin/book`, new current in DB ✓
10. `GET /` → landing now shows *Station Eleven* in hero ✓
11. `POST /admin/book/finish` → **303**, current marked finished, moves to past ✓
12. `POST /logout` → **303** to `/`, cookie cleared ✓
13. `GET /admin` after logout → 302 to `/login` ✓

**Credentials (already in `.env`):**
- Passcode: `5418` (shared with roommate; easy to change later by regenerating bcrypt hash)
- `SESSION_SECRET` — random 32-byte value (already in `.env`, not shown)
- **Do not commit `.env`.** It's in `.gitignore`.

**DB state after clean re-seed:**
- 1 current book: *The Secret History* by Donna Tartt (started today, read_by 30 days from now)
- 4 meetings: today + 7/14/21 days, all 7pm
- 1 past book: *The Vanishing Half* by Brit Bennett (finished 2025-05-12)
- 0 RSVPs, 0 members — these come in Phase 3 + 4

## Phase 3 — Meetings + RSVP ✅ DONE

**Built (633 LOC this phase, 2,287 total):**
- `app.py` (398) — added 11 new routes:
  - Public: `GET /meetings/{id}`, `GET /rsvp`, `POST /rsvp`, `GET /rsvp/thanks`
  - Admin: `GET /admin/meetings`, `GET /admin/meetings/new`, `POST /admin/meetings`, `GET /admin/meetings/{id}/edit`, `POST /admin/meetings/{id}`, `POST /admin/meetings/{id}/delete`, `GET /admin/meetings/{id}/notes`, `POST /admin/meetings/{id}/notes`
  - Plus helpers `_all_books_for_admin()` and `_parse_int()`.
- `db.py` — fixed `add_meeting()` so `book_id` defaults to `None` (was required, which broke test scripts and any future caller that doesn't have a book picked).
- 6 new templates: `rsvp.html` (50), `rsvp_thanks.html` (29), `meeting.html` (62), `admin/meetings.html` (55), `admin/meeting_form.html` (66), `admin/meeting_notes.html` (40).
- `style.css` (491) — added admin list rows, form-card--wide, textarea/select styling, RSVP radio chips, meeting detail layout, meeting summary block, disabled state for textarea.

**Verified end-to-end (23/23 curl tests):**
- Login as admin → 303 ✓
- `/admin/meetings` lists 4 upcoming + 1 past ✓
- `/admin/meetings/new` form renders, dropdown shows books ✓
- `POST /admin/meetings` creates a meeting, redirects to list ✓
- `GET /admin/meetings/{id}/edit` pre-fills date/time/location ✓
- `POST /admin/meetings/{id}` updates → 303 ✓
- `POST /admin/meetings/{id}/delete` removes the meeting ✓
- Public `/rsvp` renders form with 3 radio buttons (yes/no/maybe) ✓
- `POST /rsvp` as "Oz" (yes) → 303 to thanks page ✓
- `POST /rsvp` as "Roommate" (maybe) → 303, persists ✓
- `/rsvp/thanks` shows confirmation with name + response ✓
- `/meetings/{id}` shows RSVP counts and "Who's in" list with names + colored badges ✓
- Landing page meeting cards show updated counts: `0 yes · 0 maybe · 0 no` (one of them has 1 yes from test) ✓
- `POST /rsvp` to a past meeting → 400 (server guard) ✓
- `GET /admin/meetings/{past}/notes` — past meeting, textarea enabled ✓
- `GET /admin/meetings/{future}/notes` — future meeting, textarea disabled with "Notes can only be added after the meeting date" error ✓
- `POST /admin/meetings/{past}/notes` saves notes → 303, DB has the notes ✓
- `POST /admin/meetings/{future}/notes` → 400 (server guard) ✓
- `/meetings/{past}` as a guest: notes render, RSVP block hidden (only show for upcoming) ✓
- `/admin/meetings` shows past meetings with "Add/Edit notes" + Edit + Delete actions ✓
- Empty name on RSVP → 422 (FastAPI form validation, not 400 — fine, defensive) ✓

**Bugs found + fixed:**
- `db.add_meeting()` had `book_id: int | None` (no default) but the spec and all callers treat it as optional. Changed to `book_id: int | None = None`. Surfaces in test scripts and any future admin flow that creates a meeting before picking a book.

**DB state after clean re-seed:**
- 1 current book (Secret History), 4 meetings (today + 7/14/21 days, all 7pm TBD), 1 past book (Vanishing Half).
- 0 RSVPs, 0 members — Phase 4.

## Phase 4 — Past books + members + polish (TODO)

- `/admin/past` archive.
- `/admin/members` list + add + delete.
- iCal export? — Oz said v2, skip.
- Mobile pass, basic CSS polish.

## Phase 5 — Deploy (TODO)

- Create GitHub repo `5418-bookclub`.
- Connect to Render free tier.
- Set env vars: `ADMIN_PASSCODE_HASH`, `SESSION_SECRET`.
- First deploy ~3 min, URL like `https://5418-bookclub.onrender.com`.

## Run locally

```bash
cd ~/Projects/5418-bookclub
source .venv/bin/activate   # (or use .venv/bin/python directly)
python seed.py              # idempotent; only seeds empty DB
uvicorn app:app --host 127.0.0.1 --port 8001 --reload
# Open http://127.0.0.1:8001
```

## Pause-safe cleanup

The server is currently running on `127.0.0.1:8001`. To stop:
```bash
pkill -f "uvicorn app:app"
```
