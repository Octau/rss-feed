"""Async SQLite storage for feeds and seen entries."""
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    name TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL DEFAULT 300,
    added_by INTEGER NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_polled REAL NOT NULL DEFAULT 0,
    UNIQUE (guild_id, url)
);

CREATE TABLE IF NOT EXISTS seen_entries (
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    entry_key TEXT NOT NULL,
    PRIMARY KEY (feed_id, entry_key)
);
"""

_conn: aiosqlite.Connection | None = None


async def init(path: str) -> None:
    global _conn
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA foreign_keys = ON")
    await _conn.executescript(SCHEMA)
    await _conn.commit()


async def close() -> None:
    if _conn is not None:
        await _conn.close()


async def add_feed(guild_id: int, url: str, name: str, webhook_url: str,
                   interval: int, added_by: int) -> int | None:
    """Returns the new feed id, or None if the URL is already registered."""
    try:
        cur = await _conn.execute(
            "INSERT INTO feeds (guild_id, url, name, webhook_url, interval_seconds, added_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, url, name, webhook_url, interval, added_by),
        )
    except aiosqlite.IntegrityError:
        return None
    await _conn.commit()
    return cur.lastrowid


async def get_feed(guild_id: int, ref: str) -> aiosqlite.Row | None:
    """Look up a feed by numeric id or by URL, scoped to a guild."""
    if ref.isdigit():
        cur = await _conn.execute(
            "SELECT * FROM feeds WHERE guild_id = ? AND id = ?", (guild_id, int(ref)))
    else:
        cur = await _conn.execute(
            "SELECT * FROM feeds WHERE guild_id = ? AND url = ?", (guild_id, ref))
    return await cur.fetchone()


async def remove_feed(feed_id: int) -> None:
    await _conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    await _conn.commit()


async def list_feeds(guild_id: int) -> list[aiosqlite.Row]:
    cur = await _conn.execute(
        "SELECT * FROM feeds WHERE guild_id = ? ORDER BY id", (guild_id,))
    return await cur.fetchall()


async def due_feeds(now: float) -> list[aiosqlite.Row]:
    cur = await _conn.execute(
        "SELECT * FROM feeds WHERE last_polled + interval_seconds <= ? ORDER BY last_polled",
        (now,),
    )
    return await cur.fetchall()


async def set_interval(feed_id: int, seconds: int) -> None:
    await _conn.execute(
        "UPDATE feeds SET interval_seconds = ? WHERE id = ?", (seconds, feed_id))
    await _conn.commit()


async def mark_due(feed_id: int) -> None:
    await _conn.execute("UPDATE feeds SET last_polled = 0 WHERE id = ?", (feed_id,))
    await _conn.commit()


async def update_poll_meta(feed_id: int, etag: str | None,
                           last_modified: str | None, polled_at: float) -> None:
    await _conn.execute(
        "UPDATE feeds SET etag = ?, last_modified = ?, last_polled = ? WHERE id = ?",
        (etag, last_modified, polled_at, feed_id),
    )
    await _conn.commit()


async def seen_keys(feed_id: int) -> set[str]:
    cur = await _conn.execute(
        "SELECT entry_key FROM seen_entries WHERE feed_id = ?", (feed_id,))
    return {row["entry_key"] for row in await cur.fetchall()}


async def add_seen(feed_id: int, keys: list[str], keep: int = 500) -> None:
    await _conn.executemany(
        "INSERT OR IGNORE INTO seen_entries (feed_id, entry_key) VALUES (?, ?)",
        [(feed_id, k) for k in keys],
    )
    # Keep the table bounded so long-running feeds don't grow forever.
    await _conn.execute(
        "DELETE FROM seen_entries WHERE feed_id = ? AND rowid NOT IN ("
        " SELECT rowid FROM seen_entries WHERE feed_id = ? ORDER BY rowid DESC LIMIT ?)",
        (feed_id, feed_id, keep),
    )
    await _conn.commit()
