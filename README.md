# 5418 Book Club

A small deployed website for the 5418 Book Club (~15 members). Public landing shows the current book and upcoming meetings; members RSVP by name. Admin section is gated by a shared passcode.

## Live site

[https://five418-bookclub.onrender.com](https://five418-bookclub.onrender.com) *(after first deploy)*

## Features

- **Public landing** — current book with countdown, upcoming meetings with RSVP counts, archive of past books
- **RSVP by name** — no accounts, no email, no friction
- **Passcode-gated admin** — set books (with Open Library search), manage meetings, view RSVPs, download DB backup
- **Meeting notes** — add post-meeting notes after the date passes

## Tech stack

- Python 3.14 + FastAPI
- Jinja2 templates
- SQLite (stdlib)
- bcrypt + itsdangerous for auth
- Open Library API for book metadata (free, no key)
- Deployed to Render free tier

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

## Deploy

1. Push to GitHub
2. Render → New Web Service → Connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Env vars: `ADMIN_PASSCODE_HASH`, `SESSION_SECRET`

See `render.yaml` for declarative config.

## Project size

~2,900 LOC across Python, templates, and CSS.
