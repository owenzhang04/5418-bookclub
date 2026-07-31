"""Tiny helper CLI: `python seed.py` re-runs the seed (idempotent)."""

import db

if __name__ == "__main__":
    db.init_db()
    db.ensure_club()
    db.seed()
    print("Seeded.")
