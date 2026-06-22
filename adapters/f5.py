"""Adapter for the F5 Support (NGINX) RSS feed.

Filters to security advisories only — items without a CVE-YYYY-NNNN reference
in their title or description are dropped before reaching the poller.
"""
import re
from dataclasses import dataclass, field
from typing import ClassVar

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)


@dataclass
class F5RSSItem:
    title: str
    link: str
    guid: str
    pub_date: str
    published_parsed: object  # struct_time from feedparser, passed through
    description: str

    def as_entry(self) -> dict:
        return {
            "id": self.guid,
            "link": self.link,
            "title": self.title,
            "published": self.pub_date,
            "published_parsed": self.published_parsed,
            "summary": self.description,
        }


@dataclass
class F5RSSFeed:
    feed_type: ClassVar[str] = "f5"
    items: list[F5RSSItem] = field(default_factory=list)

    def entries(self) -> list[dict]:
        """Only items referencing a CVE in the title or description."""
        return [
            item.as_entry() for item in self.items
            if CVE_RE.search(item.title) or CVE_RE.search(item.description)
        ]

    @classmethod
    def from_parsed(cls, parsed) -> "F5RSSFeed":
        return cls(items=[
            F5RSSItem(
                title=e.get("title", ""),
                link=e.get("link", ""),
                guid=e.get("id") or e.get("link", ""),
                pub_date=e.get("published", ""),
                published_parsed=e.get("published_parsed"),
                description=e.get("summary", ""),
            )
            for e in parsed.entries
        ])


ADAPTER = F5RSSFeed
