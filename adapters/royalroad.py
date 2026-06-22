"""Adapter for Royal Road fiction RSS feeds.

Royal Road's feed is RSS 2.0 with a ``generator`` channel field (and none of
the optional ``copyright``/``ttl`` fields F5 uses). Item ``guid``s are
elements with an ``isPermaLink`` attribute, and ``description`` is HTML-rich
chapter content (``<p>``/``<span>``/``<em>``). The HTML is kept as-is in the
entry dicts: clean_summary() strips and truncates it at embed time, exactly
as it does for generic feedparser entries.
"""
import logging
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import ClassVar

log = logging.getLogger("adapters.royalroad")


def _parse_pub_date(value: str) -> time.struct_time | None:
    """RFC 822 pubDate -> UTC struct_time (the shape feedparser produces)."""
    try:
        return parsedate_to_datetime(value).utctimetuple()
    except (TypeError, ValueError):
        return None


@dataclass
class RoyalRoadRSSItem:
    title: str
    link: str
    guid: str
    pub_date: str
    description: str  # HTML-rich; stripped later by clean_summary()

    def as_entry(self) -> dict:
        """Entry dict in the shape entry_key()/build_embed() expect."""
        return {
            "id": self.guid,
            "link": self.link,
            "title": self.title,
            "published": self.pub_date,
            "published_parsed": _parse_pub_date(self.pub_date),
            "summary": self.description,
        }


@dataclass
class RoyalRoadRSSFeed:
    feed_type: ClassVar[str] = "royalroad"

    title: str
    link: str
    description: str
    language: str
    generator: str
    items: list[RoyalRoadRSSItem] = field(default_factory=list)

    def entries(self) -> list[dict]:
        return [item.as_entry() for item in self.items]

    @classmethod
    def from_parsed(cls, parsed) -> "RoyalRoadRSSFeed":
        """Build from a feedparser result.

        feedparser renames the Royal Road fields: guid -> entry.id (its
        isPermaLink attribute -> entry.guidislink), pubDate ->
        entry.published, description -> entry.summary.
        """
        feed = parsed.feed
        return cls(
            title=feed.get("title", ""),
            link=feed.get("link", ""),
            description=feed.get("subtitle") or feed.get("description", ""),
            language=feed.get("language", ""),
            generator=feed.get("generator", ""),
            items=[
                RoyalRoadRSSItem(
                    title=e.get("title", ""),
                    link=e.get("link", ""),
                    guid=e.get("id") or e.get("link", ""),
                    pub_date=e.get("published", ""),
                    description=e.get("summary", ""),
                )
                for e in parsed.entries
            ],
        )


ADAPTER = RoyalRoadRSSFeed
