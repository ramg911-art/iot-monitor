"""One-time migration: add battery column to devices table. Run if upgrading from pre-battery schema."""
import asyncio
import sqlite3
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings


def run():
    settings = get_settings()
    db_url = settings.database_url
    if "sqlite" not in db_url:
        print("This script is for SQLite only.")
        return
    path = db_url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
    if not os.path.exists(path):
        print("DB not found:", path)
        return
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(devices)")
    cols = [row[1] for row in cur.fetchall()]
    if "battery" in cols:
        print("battery column already exists")
    else:
        cur.execute("ALTER TABLE devices ADD COLUMN battery INTEGER")
        conn.commit()
        print("Added battery column")
    conn.close()


if __name__ == "__main__":
    run()
