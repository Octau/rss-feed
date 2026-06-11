"""Async SQLite storage for feeds and seen entries."""
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    name TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    feed_type TEXT NOT NULL DEFAULT 'generic',
    interval_seconds INTEGER NOT NULL DEFAULT 14400,
    added_by INTEGER NOT NULL,
    etag TEXT,
    last_modified TEXT,
    last_polled REAL NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    icon_url TEXT,
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
    # CREATE TABLE IF NOT EXISTS won't alter pre-existing DBs, so patch in
    # columns added after the first release.
    cur = await _conn.execute("PRAGMA table_info(feeds)")
    columns = {row["name"] for row in await cur.fetchall()}
    if "feed_type" not in columns:
        await _conn.execute(
            "ALTER TABLE feeds ADD COLUMN feed_type TEXT NOT NULL DEFAULT 'generic'")
    if "fail_count" not in columns:
        await _conn.execute(
            "ALTER TABLE feeds ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
    if "last_error" not in columns:
        await _conn.execute(
            "ALTER TABLE feeds ADD COLUMN last_error TEXT")
    if "icon_url" not in columns:
        await _conn.execute(
            "ALTER TABLE feeds ADD COLUMN icon_url TEXT")
    await _conn.commit()


async def close() -> None:
    if _conn is not None:
        await _conn.close()


async def add_feed(guild_id: int, url: str, name: str, webhook_url: str,
                   interval: int, added_by: int,
                   feed_type: str = "generic",
                   icon_url: str | None = None) -> int | None:
    """Returns the new feed id, or None if the URL is already registered."""
    try:
        cur = await _conn.execute(
            "INSERT INTO feeds (guild_id, url, name, webhook_url, interval_seconds,"
            " added_by, feed_type, icon_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, url, name, webhook_url, interval, added_by, feed_type, icon_url),
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


async def update_feed(feed_id: int, *,
                      name: str | None = None,
                      webhook_url: str | None = None,
                      feed_type: str | None = None,
                      interval_seconds: int | None = None,
                      icon_url: str | None = None) -> None:
    """Edit one or more mutable fields on a feed."""
    fields: dict[str, object] = {}
    if name is not None:
        fields["name"] = name
    if webhook_url is not None:
        fields["webhook_url"] = webhook_url
    if feed_type is not None:
        fields["feed_type"] = feed_type
    if interval_seconds is not None:
        fields["interval_seconds"] = interval_seconds
    if icon_url is not None:
        fields["icon_url"] = icon_url
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    await _conn.execute(
        f"UPDATE feeds SET {set_clause} WHERE id = ?",
        (*fields.values(), feed_id),
    )
    await _conn.commit()


async def record_poll_failure(feed_id: int, error: str) -> int:
    """Increment fail_count, store last_error. Returns the new fail_count."""
    await _conn.execute(
        "UPDATE feeds SET fail_count = fail_count + 1, last_error = ? WHERE id = ?",
        (error[:500], feed_id),
    )
    await _conn.commit()
    cur = await _conn.execute(
        "SELECT fail_count FROM feeds WHERE id = ?", (feed_id,))
    row = await cur.fetchone()
    return row["fail_count"] if row else 0


async def record_poll_success(feed_id: int) -> bool:
    """Reset fail_count and last_error. Returns True if the feed had failures before."""
    cur = await _conn.execute(
        "SELECT fail_count FROM feeds WHERE id = ?", (feed_id,))
    row = await cur.fetchone()
    had_failures = row and row["fail_count"] > 0
    await _conn.execute(
        "UPDATE feeds SET fail_count = 0, last_error = NULL WHERE id = ?", (feed_id,))
    await _conn.commit()
    return bool(had_failures)


async def unhealthy_feeds(guild_id: int) -> list[aiosqlite.Row]:
    """Feeds in a guild that have at least one consecutive failure."""
    cur = await _conn.execute(
        "SELECT * FROM feeds WHERE guild_id = ? AND fail_count > 0 ORDER BY fail_count DESC",
        (guild_id,),
    )
    return await cur.fetchall()


async def mark_due(feed_id: int) -> None:
    await _conn.execute(
        "UPDATE feeds SET last_polled = 0, etag = NULL, last_modified = NULL WHERE id = ?",
        (feed_id,))
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
