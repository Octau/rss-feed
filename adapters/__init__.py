"""Per-source feed adapters that normalize vendor feed shapes.

A module in this package registers an adapter by exporting ``ADAPTER``: a
class with a ``feed_type`` string, a ``from_parsed(parsed)`` classmethod and
an ``entries()`` method returning entry dicts compatible with the bot's
entry_key/build_embed helpers. The registry is built by scanning this package
at import time, so dropping a new module in is all it takes.

``GENERIC`` ("generic") means no adapter: raw feedparser entries are used.
"""
import importlib
import logging
import pkgutil

log = logging.getLogger("adapters")

GENERIC = "generic"

ADAPTERS: dict[str, type] = {}
for _mod_info in pkgutil.iter_modules(__path__):
    _mod = importlib.import_module(f"{__name__}.{_mod_info.name}")
    _adapter = getattr(_mod, "ADAPTER", None)
    if _adapter is not None:
        ADAPTERS[_adapter.feed_type] = _adapter

FEED_TYPES: tuple[str, ...] = (GENERIC, *sorted(ADAPTERS))


def adapt_entries(feed_type: str, parsed) -> list:
    """Entries from a feedparser result, normalized by the feed's adapter.

    Generic (and unknown) feed types fall back to the raw feedparser entries.
    """
    adapter = ADAPTERS.get(feed_type)
    if adapter is None:
        if feed_type != GENERIC:
            log.warning("No adapter for feed type %r, using raw entries", feed_type)
        return list(parsed.entries)
    return adapter.from_parsed(parsed).entries()
