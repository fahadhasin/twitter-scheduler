import sqlite3
import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "scheduler.db"


def get_conn():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS threads (
                id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'scheduled',
                scheduled_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                posted_at TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS tweets (
                id INTEGER PRIMARY KEY,
                thread_id INTEGER REFERENCES threads(id),
                position INTEGER,
                text TEXT,
                image_paths TEXT,
                tweet_id TEXT,
                UNIQUE(thread_id, position)
            );
        """)


def create_thread(scheduled_at: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO threads (scheduled_at) VALUES (?)", (scheduled_at,)
        )
        return cur.lastrowid


def add_tweet(thread_id: int, position: int, text: str, image_paths: list[str]):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tweets (thread_id, position, text, image_paths) VALUES (?, ?, ?, ?)",
            (thread_id, position, text, json.dumps(image_paths)),
        )


def get_thread(thread_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()


def get_tweets(thread_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tweets WHERE thread_id = ? ORDER BY position",
            (thread_id,),
        ).fetchall()


def get_pending_threads() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM threads WHERE status = 'scheduled' AND scheduled_at <= datetime('now')"
        ).fetchall()


def get_scheduled_threads() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM threads WHERE status = 'scheduled' ORDER BY scheduled_at"
        ).fetchall()


def mark_thread_posted(thread_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE threads SET status='posted', posted_at=datetime('now') WHERE id=?",
            (thread_id,),
        )


def mark_thread_failed(thread_id: int, error: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE threads SET status='failed', error=? WHERE id=?",
            (error, thread_id),
        )


def update_tweet_id(thread_id: int, position: int, tweet_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tweets SET tweet_id=? WHERE thread_id=? AND position=?",
            (tweet_id, thread_id, position),
        )


def cancel_thread(thread_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE threads SET status='cancelled' WHERE id=? AND status='scheduled'",
            (thread_id,),
        )
        return cur.rowcount > 0


def delete_thread_images(thread_id: int):
    """Delete downloaded images for a thread."""
    tweets = get_tweets(thread_id)
    for tweet in tweets:
        paths = json.loads(tweet["image_paths"] or "[]")
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
