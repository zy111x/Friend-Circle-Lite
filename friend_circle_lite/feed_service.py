"""Feed discovery, parsing, and incremental tracking services."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests

from friend_circle_lite import HEADERS_XML, timeout
from friend_circle_lite.models import Article, FeedEndpoint, Website
from friend_circle_lite.utils.time import format_published_time
from friend_circle_lite.utils.url import replace_non_domain


class FeedDiscoveryService:
    """Discover an RSS or Atom endpoint for a website."""

    POSSIBLE_FEEDS = [
        ("atom", "/atom.xml"),
        ("rss", "/rss.xml"),
        ("rss2", "/rss2.xml"),
        ("rss3", "/rss.php"),
        ("feed", "/feed"),
        ("feed2", "/feed.xml"),
        ("feed3", "/feed/"),
        ("feed4", "/feed.php"),
        ("index", "/index.xml"),
    ]

    def __init__(self, session: requests.Session):
        self.session = session

    def discover(self, website_url: str) -> FeedEndpoint | None:
        """Try common feed endpoints and return the first valid match."""
        for feed_type, path in self.POSSIBLE_FEEDS:
            feed_url = website_url.rstrip("/") + path
            try:
                response = self.session.get(feed_url, headers=HEADERS_XML, timeout=timeout)
            except requests.RequestException:
                continue

            if response.status_code != 200:
                continue

            content_type = response.headers.get("Content-Type", "").lower()
            if "xml" in content_type or "rss" in content_type or "atom" in content_type:
                return FeedEndpoint(url=feed_url, feed_type=feed_type, source="auto")

            text_head = response.text[:1000].lower()
            if "<rss" in text_head or "<feed" in text_head or "<rdf:rdf" in text_head:
                return FeedEndpoint(url=feed_url, feed_type=feed_type, source="auto")

        logging.warning(f"无法找到 {website_url} 的订阅链接")
        return None


class FeedParserService:
    """Parse a discovered feed into normalized article objects."""

    def __init__(self, session: requests.Session):
        self.session = session

    def parse(self, feed_url: str, count: int = 5, blog_url: str = "") -> list[Article]:
        """Parse a feed URL and return the newest `count` articles.

        The returned articles are normalized to the project's internal domain
        model, while preserving the original public output fields.
        """
        try:
            response = self.session.get(feed_url, headers=HEADERS_XML, timeout=timeout)
            response.encoding = response.apparent_encoding or "utf-8"
            feed = feedparser.parse(response.text)
        except Exception as exc:
            logging.error(f"无法解析 FEED 地址：{feed_url} ，请自行排查原因！错误信息: {exc}")
            return []

        default_author = feed.feed.author if "author" in feed.feed else ""
        articles: list[Article] = []

        for entry in feed.entries:
            published = self._extract_published_time(entry)
            article_link = replace_non_domain(entry.link, blog_url) if "link" in entry else ""
            article = Article(
                title=entry.title if "title" in entry else "",
                author=default_author,
                link=article_link,
                published=published,
                summary=entry.summary if "summary" in entry else "",
                content=entry.content[0].value if "content" in entry and entry.content else entry.description if "description" in entry else "",
            )
            articles.append(article)

        valid_articles = [article for article in articles if article.published]
        valid_articles.sort(key=lambda item: datetime.strptime(item.published, "%Y-%m-%d %H:%M"), reverse=True)
        return valid_articles[:count] if count < len(valid_articles) else valid_articles

    @staticmethod
    def _extract_published_time(entry) -> str:
        """Extract a normalized publish time from a feed entry."""
        if "published" in entry:
            return format_published_time(entry.published)
        if "updated" in entry:
            published = format_published_time(entry.updated)
            logging.warning(f"文章 {entry.title} 未包含发布时间，已使用更新时间 {published}")
            return published

        logging.warning(f"文章 {entry.title} 未包含任何时间信息, 请检查原文, 跳过该文章")
        return ""


class LatestArticleTracker:
    """Track whether a website published new posts since the last crawl."""

    def __init__(self, storage_path: str | Path):
        self.storage_path = Path(storage_path)

    def diff_and_persist(self, latest_articles: list[Article]) -> list[dict] | None:
        """Return newly seen articles and update the local snapshot file."""
        previous_links = self._load_previous_links()
        updated_articles = [article.to_tracking_dict() for article in latest_articles if article.link not in previous_links]
        self._persist(latest_articles)
        return updated_articles if updated_articles else None

    def _load_previous_links(self) -> set[str]:
        if not self.storage_path.exists():
            return set()
        try:
            with open(self.storage_path, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except Exception as exc:
            logging.warning(f"读取最新文章缓存失败: {self.storage_path}, 错误信息: {exc}")
            return set()

        articles = payload.get("articles", []) if isinstance(payload, dict) else []
        return {article.get("link", "") for article in articles if isinstance(article, dict) and article.get("link")}

    def _persist(self, latest_articles: list[Article]) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"articles": [article.to_tracking_dict() for article in latest_articles]}
        with open(self.storage_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=4)


def extract_blog_origin(url: str) -> str:
    """Return a normalized origin for display or author profile links."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"
