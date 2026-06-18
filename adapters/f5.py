"""Adapter for the F5 Support (NGINX) RSS feed.

F5's feed is RSS 2.0 with two channel fields most feeds omit: ``copyright``
and ``ttl`` (suggested cache lifetime, in minutes). This adapter normalizes
the feed into typed objects and exposes items as the plain entry dicts the
rest of the bot (entry_key, build_embed) consumes, so an F5 feed can flow
through the existing poll/announce pipeline unchanged.
"""
import logging
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import ClassVar

log = logging.getLogger("adapters.f5")

# F5's feed mixes product news with security advisories; only the latter (which
# carry a CVE identifier) are announced. Match CVE-YYYY-NNNN (4+ digit sequence).
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)


def _parse_pub_date(value: str) -> time.struct_time | None:
    """RFC 822 pubDate -> UTC struct_time (the shape feedparser produces)."""
    try:
        return parsedate_to_datetime(value).utctimetuple()
    except (TypeError, ValueError):
        return None


@dataclass
class F5RSSItem:
    title: str
    link: str
    guid: str
    pub_date: str
    description: str  # plain text in the F5 feed, no HTML stripping needed

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
class F5RSSFeed:
    feed_type: ClassVar[str] = "f5"

    title: str
    link: str
    description: str
    language: str
    copyright: str
    ttl: int  # minutes; 0 when the feed didn't provide one
    items: list[F5RSSItem] = field(default_factory=list)

    @property
    def poll_interval_seconds(self) -> int:
        """The feed's suggested polling interval (ttl is in minutes)."""
        return self.ttl * 60

    def entries(self) -> list[dict]:
        """Only items referencing a CVE in the title or description.

        F5's feed carries general product news alongside security advisories;
        servers subscribe for the advisories, so non-CVE items are dropped here
        and never reach the poller, previews, or seen-entry tracking.
        """
        return [
            item.as_entry() for item in self.items
            if CVE_RE.search(item.title) or CVE_RE.search(item.description)
        ]

    @classmethod
    def from_parsed(cls, parsed) -> "F5RSSFeed":
        """Build from a feedparser result.

        feedparser renames the F5 fields: copyright -> feed.rights,
        guid -> entry.id, pubDate -> entry.published,
        description -> entry.summary; ttl arrives as a string.
        """
        feed = parsed.feed
        try:
            ttl = int(feed.get("ttl", 0))
        except (TypeError, ValueError):
            log.warning("Unparseable ttl %r in feed %r", feed.get("ttl"),
                        feed.get("title"))
            ttl = 0
        return cls(
            title=feed.get("title", ""),
            link=feed.get("link", ""),
            description=feed.get("subtitle") or feed.get("description", ""),
            language=feed.get("language", ""),
            copyright=feed.get("rights") or feed.get("copyright", ""),
            ttl=ttl,
            items=[
                F5RSSItem(
                    title=e.get("title", ""),
                    link=e.get("link", ""),
                    guid=e.get("id") or e.get("link", ""),
                    pub_date=e.get("published", ""),
                    description=e.get("summary", ""),
                )
                for e in parsed.entries
            ],
        )

    @classmethod
    def from_dict(cls, data: dict) -> "F5RSSFeed":
        """Build from the raw object shape: {channel: {..., items: [...]}}."""
        channel = data.get("channel", data)
        try:
            ttl = int(channel.get("ttl", 0))
        except (TypeError, ValueError):
            ttl = 0
        return cls(
            title=channel.get("title", ""),
            link=channel.get("link", ""),
            description=channel.get("description", ""),
            language=channel.get("language", ""),
            copyright=channel.get("copyright", ""),
            ttl=ttl,
            items=[
                F5RSSItem(
                    title=i.get("title", ""),
                    link=i.get("link", ""),
                    guid=i.get("guid") or i.get("link", ""),
                    pub_date=i.get("pubDate", ""),
                    description=i.get("description", ""),
                )
                for i in channel.get("items", [])
            ],
        )


ADAPTER = F5RSSFeed
