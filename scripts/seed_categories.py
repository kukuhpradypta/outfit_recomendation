"""Seed the category table with default clothing categories.

Idempotent: existing categories (matched by name) are skipped, so it is safe to
run multiple times.

Run (dari root proyek, env py312 aktif):
    python scripts/seed_categories.py
"""
import sys
from pathlib import Path

# Allow running as `python scripts/seed_categories.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402
from app.repositories.category_repository import CategoryRepository  # noqa: E402

# (name, body_zone). body_zone drives placement on the mannequin preview.
DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("atasan", "upper"),
    ("bawahan", "lower"),
    ("sendal", "feet"),
    ("sepatu", "feet"),
    ("topi", "accessory"),
    ("kacamata", "accessory"),
]


def seed() -> None:
    db = SessionLocal()
    try:
        repo = CategoryRepository(db)
        created, skipped = 0, 0
        for name, zone in DEFAULT_CATEGORIES:
            if repo.get_by_name(name):
                print(f"  skip  (sudah ada): {name}")
                skipped += 1
                continue
            cat = repo.create(name=name, body_zone=zone)
            print(f"  added: {cat.name} (id={cat.id}, body_zone={cat.body_zone})")
            created += 1
        print(f"Selesai. Ditambahkan: {created}, dilewati: {skipped}.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
