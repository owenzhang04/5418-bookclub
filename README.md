# 5418 Book Club

A small deployed website for the 5418 Book Club (~15 members). Public landing shows the current book and upcoming meetings; members RSVP by name. Admin section is gated by a shared passcode.

## Live site

[https://five418-bookclub.onrender.com](https://five418-bookclub.onrender.com) *(after first deploy)*

## Features

- **Public landing** — current book with countdown, upcoming meetings with RSVP counts, archive of past books
- **RSVP by name** — no accounts, no email, no friction
- **Passcode-gated admin** — set books (with Open Library search), edit the current book's reading dates, manage meetings, view RSVPs
- **Meeting notes** — add post-meeting notes after the date passes
- **Backup + restore** — download the SQLite file, or upload one to replace it (validated, and the old database is snapshotted first)

## Tech stack

- Python 3.14 + FastAPI
- Jinja2 templates
- SQLite (stdlib)
- bcrypt + itsdangerous for auth
- Open Library API for book metadata (free, no key)
- Deployed to Render free tier; SQLite replicated to Backblaze B2 via Litestream

## Quick start (local)

```bash
cd 5418-bookclub
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill .env
cp .env.example .env
# Generate passcode hash and session secret (see .env.example)

python seed.py          # seed demo data
uvicorn app:app --reload
# open http://127.0.0.1:8000
```

Local development is http, so `.env` sets `COOKIE_SECURE=0`. Everywhere
else the session cookie is `Secure` by default.

Run the app as a **single process**. The login and RSVP rate limiters keep
their counters in memory, so `uvicorn --workers N` would give each worker
its own set and multiply every limit by N.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

They drive the real app through `TestClient` against a throwaway SQLite
file in `tmp_path` — nothing is mocked, and nothing touches
`data/bookclub.db`. `test_every_admin_route_requires_login` walks
`app.routes` and asserts every `/admin` path redirects to `/login`, which
is the cheapest guard against a new route that only looks gated.

## Deploy

1. Create a private Backblaze B2 bucket and an application key scoped to it (no credit card). See `RENDER_DEPLOY_NOTES.txt`.
2. Set `endpoint` / `region` / `bucket` in `litestream.yml` to match that bucket.
3. Push to GitHub → Render Blueprint (or Manual Deploy).
4. Env vars in the Render dashboard:
   - `ADMIN_PASSCODE_HASH` — from local `SECRETS_LOCAL.txt` (never commit this)
   - `SESSION_SECRET` — same, or leave Render's generated value after rotating
   - `BOOKCLUB_DB_PATH` — `/opt/render/project/src/data/bookclub.db`
   - `BOOKCLUB_TZ` — optional; default `America/Chicago`
   - `B2_KEY_ID` / `B2_APPLICATION_KEY` — the Backblaze application key
   - `COOKIE_SECURE` — leave unset in production

See `render.yaml` and `RENDER_DEPLOY_NOTES.txt`.

### Why Litestream (and why not a Render disk)

Render's free filesystem is ephemeral: SQLite would reset on every deploy and every spin-down after 15 idle minutes. Persistent disks fix that but require a paid instance (~$7.25/mo).

Instead, Litestream continuously replicates the SQLite file to Backblaze B2 and restores it on boot. Cost: $0. Tradeoffs: a write within ~1 second of an abrupt kill can be lost, and free-tier cold starts (~1 minute) remain. `db.py` does not change — Litestream wraps the process.

### Schema changes

`db.py` tracks a `SCHEMA_VERSION` in SQLite's `user_version` counter. `init_db()` runs `CREATE TABLE IF NOT EXISTS`, then `_migrate()`, which walks an older database forward with guarded `ALTER TABLE`s, then `_repair_current_books()`. Adding a column means adding it to `SCHEMA`, bumping `SCHEMA_VERSION`, and adding a **new** step to `_migrate` — never editing an existing one, since deployed databases may already have run it. Skip any of that and a deployed database keeps its old shape and the app 500s with "no such column".

Migrations run on one connection inside one transaction, and every step is additionally guarded by an inspection of the current schema, so they're safe to re-run against a database of unknown provenance — including one restored from an old backup through `/admin/backup`.

## Project size

~2,900 LOC across Python, templates, and CSS.
