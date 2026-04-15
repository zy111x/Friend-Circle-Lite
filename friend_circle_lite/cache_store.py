"""Persistent RSS cache storage.

SQLite is used for the feed cache because it is more robust than hand-edited
text formats for internal state:

- schema is explicit and stable;
- writes are transactional;
- corruption risk from accidental manual edits is lower;
- Python ships with `sqlite3`, so no extra dependency is required.

For smooth upgrades, this store can also migrate legacy cache data from the old
JSON cache file and the intermediate YAML cache file if they exist.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import yaml

from friend_circle_lite.models import CacheRecord


class FeedCacheStore:
    """Persist and load discovered RSS endpoints using SQLite."""

    def __init__(self, cache_path: str | Path | None):
        self.cache_path = Path(cache_path) if cache_path else None

    def load_records(self) -> list[CacheRecord]:
        """Load cache records from SQLite, migrating legacy formats if needed."""
        if not self.cache_path:
            return []

        if self.cache_path.exists():
            return self._load_from_sqlite()

        migrated_records = self._load_legacy_records()
        if migrated_records:
            if self.save_records(migrated_records):
                logging.info(f"已迁移 {len(migrated_records)} 条 RSS 缓存记录到 {self.cache_path}。")
            return migrated_records

        logging.info(f"缓存文件 {self.cache_path} 不存在，将在首次成功抓取后创建。")
        return []

    def save_records(self, records: list[CacheRecord]) -> bool:
        """Persist cache records to the SQLite database."""
        if not self.cache_path:
            return True

        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.cache_path) as connection:
                self._ensure_schema(connection)
                connection.execute("DELETE FROM feed_cache")
                connection.executemany(
                    "INSERT INTO feed_cache(name, url, source) VALUES (?, ?, ?)",
                    [(record.name, record.url, record.source) for record in sorted(records, key=lambda item: item.name)],
                )
                connection.commit()
            logging.info(f"缓存已保存到 {self.cache_path}（{len(records)} 条）。")
            return True
        except Exception as exc:
            logging.error(f"保存缓存文件失败: {self.cache_path}, 错误信息: {exc}")
            return False

    def _load_from_sqlite(self) -> list[CacheRecord]:
        """Load records from the current SQLite cache file."""
        try:
            with sqlite3.connect(self.cache_path) as connection:
                self._ensure_schema(connection)
                rows = connection.execute(
                    "SELECT name, url, source FROM feed_cache ORDER BY name"
                ).fetchall()
        except Exception as exc:
            logging.warning(f"读取 SQLite 缓存失败: {self.cache_path}, 错误信息: {exc}")
            return []

        return [
            CacheRecord(name=name, url=url, source=source or "cache")
            for name, url, source in rows
            if name and url
        ]

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        """Create the cache table when it does not exist yet."""
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_cache (
                name TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'cache'
            )
            """
        )

    def _load_legacy_records(self) -> list[CacheRecord]:
        """Read old cache formats for seamless upgrades."""
        json_records = self._load_legacy_json_cache()
        if json_records:
            return json_records

        yaml_records = self._load_legacy_yaml_cache()
        if yaml_records:
            return yaml_records

        return []

    def _load_legacy_json_cache(self) -> list[CacheRecord]:
        """Read the previous JSON cache file format."""
        if not self.cache_path:
            return []

        legacy_path = self.cache_path.with_name("cache.json")
        if not legacy_path.exists():
            return []

        try:
            with open(legacy_path, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except Exception as exc:
            logging.warning(f"读取旧缓存文件失败: {legacy_path}, 错误信息: {exc}")
            return []

        if not isinstance(payload, list):
            return []

        return self._normalize_legacy_items(payload)

    def _load_legacy_yaml_cache(self) -> list[CacheRecord]:
        """Read the temporary YAML cache format used during refactoring."""
        if not self.cache_path:
            return []

        legacy_path = self.cache_path.with_name("feed_cache.yaml")
        if not legacy_path.exists():
            return []

        try:
            with open(legacy_path, "r", encoding="utf-8") as file:
                payload = yaml.safe_load(file) or {}
        except Exception as exc:
            logging.warning(f"读取旧 YAML 缓存失败: {legacy_path}, 错误信息: {exc}")
            return []

        items = payload.get("feeds", []) if isinstance(payload, dict) else []
        return self._normalize_legacy_items(items)

    @staticmethod
    def _normalize_legacy_items(items: list[object]) -> list[CacheRecord]:
        """Normalize legacy cache items into typed cache records."""
        records: list[CacheRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            source = str(item.get("source", "cache")).strip() or "cache"
            if name and url:
                records.append(CacheRecord(name=name, url=url, source=source))
        return records
