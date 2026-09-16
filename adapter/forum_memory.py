"""Small, durable diary storage shared by AstrBook tools and the adapter.

The first AstrBook releases used more than one data-directory name and wrote the
JSON file directly.  This module keeps the public ``ForumMemory`` API intact
while making the on-disk format migration-safe and resilient to a partially
written/corrupt record.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot import logger
from astrbot.api.star import StarTools

CANONICAL_PLUGIN_NAME = "astrbot_plugin_astrbook"
LEGACY_PLUGIN_NAMES = (
    "astrbot-plugin-astrbook",
    "astrbook",
)
DEFAULT_MAX_ITEMS = 50
MIN_MAX_ITEMS = 1
MAX_MAX_ITEMS = 1000
MAX_DIARY_CHARS = 20_000


def _clamp_max_items(value: Any) -> int:
    """Return a safe memory limit for values coming from plugin config."""

    if isinstance(value, bool):
        value = DEFAULT_MAX_ITEMS
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = DEFAULT_MAX_ITEMS
    return max(MIN_MAX_ITEMS, min(MAX_MAX_ITEMS, value))


def _as_path(value: Any) -> Path:
    """Convert AstrBot's ``Path | str`` data-dir result to an absolute Path."""

    return Path(value).expanduser().resolve()


@dataclass
class MemoryItem:
    """A single diary entry."""

    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to the stable JSON representation used by the plugin."""

        return {
            "memory_type": "diary",
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Any) -> MemoryItem | None:
        """Parse one record, returning ``None`` for malformed input.

        A single damaged record must not make all previous diary entries
        inaccessible.  Older files occasionally contain naive ISO timestamps;
        those are retained as naive datetimes for backwards compatibility.
        """

        if not isinstance(data, dict):
            return None
        content = data.get("content")
        if not isinstance(content, str):
            return None
        content = content.strip()
        if not content:
            return None

        raw_timestamp = data.get("timestamp")
        timestamp: datetime
        try:
            if isinstance(raw_timestamp, (int, float)) and not isinstance(
                raw_timestamp, bool
            ):
                timestamp = datetime.fromtimestamp(raw_timestamp, tz=timezone.utc)
            elif isinstance(raw_timestamp, str) and raw_timestamp.strip():
                timestamp_text = raw_timestamp.strip()
                if timestamp_text.endswith("Z"):
                    timestamp_text = timestamp_text[:-1] + "+00:00"
                timestamp = datetime.fromisoformat(timestamp_text)
            else:
                return None
        except (TypeError, ValueError, OverflowError, OSError):
            return None

        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(content=content, timestamp=timestamp, metadata=dict(metadata))


class ForumMemory:
    """Bounded diary storage for the bot's AstrBook experiences.

    ``storage_dir`` is primarily useful for tests.  Normal installations use
    ``data/plugin_data/astrbot_plugin_astrbook``.  Files from the old hyphenated
    directory (and the old auto-detected directory) are read once and copied to
    the canonical location without deleting the source file.
    """

    def __init__(
        self,
        max_items: int = DEFAULT_MAX_ITEMS,
        storage_dir: Path | str | None = None,
    ) -> None:
        self._max_items = _clamp_max_items(max_items)
        self._memories: list[MemoryItem] = []
        self._lock = threading.RLock()
        self._migrated_from: Path | None = None

        self._storage_path, legacy_paths = self._resolve_storage_paths(storage_dir)
        self._legacy_storage_paths = legacy_paths
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

        # Migration is deliberately non-destructive: retain the old file as a
        # rollback/backup while ensuring all future writes use one path.
        if self._migrated_from is not None and self._memories:
            self._save()
            logger.info(
                "[ForumMemory] migrated diary data from %s to %s",
                self._migrated_from,
                self._storage_path,
            )

    @property
    def storage_path(self) -> Path:
        """Canonical JSON path (exposed for diagnostics and tests)."""

        return self._storage_path

    @property
    def max_items(self) -> int:
        """Current bounded-memory limit."""

        return self._max_items

    def add_diary(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add and persist a diary entry.

        Invalid/empty values are ignored rather than raising from an LLM tool
        callback.  Extremely large entries are bounded so one model response
        cannot grow the JSON file without limit.
        """

        if not isinstance(content, str):
            logger.warning("[ForumMemory] ignored non-text diary entry")
            return
        content = content.strip()
        if not content:
            logger.warning("[ForumMemory] ignored empty diary entry")
            return
        if len(content) > MAX_DIARY_CHARS:
            content = content[:MAX_DIARY_CHARS]
            logger.warning(
                "[ForumMemory] diary entry exceeded %s chars and was truncated",
                MAX_DIARY_CHARS,
            )
        if not isinstance(metadata, dict):
            metadata = {}

        item = MemoryItem(content=content, metadata=dict(metadata))
        with self._lock:
            self._memories.append(item)
            self._trim_locked()
            self._save_locked()
        logger.debug("[ForumMemory] Added diary: %s...", content[:50])

    def get_diaries(self, limit: int | None = None) -> list[MemoryItem]:
        """Return diary entries newest first.

        A non-positive limit intentionally returns an empty list; this avoids
        Python's surprising ``items[:-1]`` behavior for malformed tool args.
        """

        with self._lock:
            items = list(reversed(self._memories))
        if limit is None:
            return items
        try:
            limit = int(limit)
        except (TypeError, ValueError, OverflowError):
            limit = DEFAULT_MAX_ITEMS
        if limit <= 0:
            return []
        return items[:limit]

    def get_summary(self, limit: int = 10) -> str:
        """Return a human-readable summary for LLM context."""

        items = self.get_diaries(limit=limit)
        if not items:
            return "还没有写过论坛日记。"

        lines = ["📔 我在 AstrBook 论坛的日记："]
        for item in items:
            if item.timestamp.tzinfo is not None:
                time_str = item.timestamp.astimezone().strftime("%m-%d %H:%M")
            else:
                time_str = item.timestamp.strftime("%m-%d %H:%M")
            lines.append(f"  📝 [{time_str}] {item.content}")
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all memories and atomically persist the empty list."""

        with self._lock:
            self._memories.clear()
            self._save_locked()
        logger.info("[ForumMemory] Cleared all memories")

    def _trim_locked(self) -> None:
        if len(self._memories) > self._max_items:
            self._memories = self._memories[-self._max_items :]

    @staticmethod
    def _read_records(path: Path) -> tuple[bool, list[MemoryItem]]:
        """Read and validate one JSON file.

        The boolean distinguishes a valid empty file from an unreadable file so
        migration can continue to the next legacy candidate.
        """

        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            logger.warning("[ForumMemory] failed to read %s: %s", path, exc)
            return False, []

        if isinstance(data, dict):
            data = data.get("memories", data.get("items", []))
        if not isinstance(data, list):
            logger.warning("[ForumMemory] ignored non-list data in %s", path)
            return False, []

        records: list[MemoryItem] = []
        skipped = 0
        for raw in data:
            if isinstance(raw, dict) and raw.get("memory_type", "diary") != "diary":
                skipped += 1
                continue
            item = MemoryItem.from_dict(raw)
            if item is None:
                skipped += 1
                continue
            records.append(item)
        if skipped:
            logger.warning(
                "[ForumMemory] skipped %s malformed/unsupported record(s) in %s",
                skipped,
                path,
            )
        return True, records

    def _load(self) -> None:
        """Load and merge canonical plus legacy diary files.

        Some releases wrote through ``main.py`` to the underscored directory
        while the adapter simultaneously wrote to the old hyphenated one.  A
        simple fallback would silently discard one half of that split history,
        so merge every valid source and de-duplicate copied entries.
        """

        merged: list[MemoryItem] = []
        seen: set[tuple[str, str]] = set()
        migration_source: Path | None = None
        for path in [self._storage_path, *self._legacy_storage_paths]:
            if not path.exists() or not path.is_file():
                continue
            valid, records = self._read_records(path)
            if not valid:
                continue
            added = 0
            for item in records:
                key = (item.content, item.timestamp.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                added += 1
            if path != self._storage_path and added and migration_source is None:
                migration_source = path
            logger.debug(
                "[ForumMemory] Loaded %s diary entries from %s (%s new)",
                len(records),
                path,
                added,
            )

        def timestamp_key(item: MemoryItem) -> float:
            try:
                return item.timestamp.timestamp()
            except (OSError, OverflowError, ValueError):
                return 0.0

        merged.sort(key=timestamp_key)
        with self._lock:
            self._memories = merged[-self._max_items :]
        self._migrated_from = migration_source

    def _save_locked(self) -> None:
        """Atomically replace the canonical JSON file (caller holds lock)."""

        data = [item.to_dict() for item in self._memories]
        temp_path: Path | None = None
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self._storage_path.name}.",
                suffix=".tmp",
                dir=self._storage_path.parent,
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, ensure_ascii=False, indent=2, default=str)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, self._storage_path)
            temp_path = None
            # Best effort directory fsync on POSIX; unsupported platforms can
            # still rely on os.replace's atomic rename semantics.
            try:
                dir_fd = os.open(self._storage_path.parent, os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
                finally:
                    os.close(dir_fd)
        except (OSError, TypeError, ValueError) as exc:
            logger.error("[ForumMemory] Failed to save %s: %s", self._storage_path, exc)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _save(self) -> None:
        """Compatibility wrapper used by older callers/tests."""

        with self._lock:
            self._save_locked()

    @staticmethod
    def _same_path(first: Path, second: Path) -> bool:
        try:
            return first.resolve() == second.resolve()
        except OSError:
            return first.absolute() == second.absolute()

    @classmethod
    def _resolve_storage_paths(
        cls,
        storage_dir: Path | str | None,
    ) -> tuple[Path, list[Path]]:
        if storage_dir is not None:
            canonical_dir = _as_path(storage_dir)
            return canonical_dir / "forum_memory.json", []

        legacy_dirs: list[Path] = []

        def add_unique(target: list[Path], value: Any) -> None:
            try:
                path = _as_path(value)
            except (OSError, TypeError, ValueError):
                return
            if not any(cls._same_path(path, existing) for existing in target):
                target.append(path)

        # Explicit canonical name is stable even when caller stack detection
        # changes between AstrBot releases.
        canonical_dir: Path | None = None
        try:
            canonical_dir = _as_path(StarTools.get_data_dir(CANONICAL_PLUGIN_NAME))
        except Exception as exc:  # noqa: BLE001 - SDK may not be initialized yet
            logger.debug("[ForumMemory] canonical StarTools path unavailable: %s", exc)

        for name in LEGACY_PLUGIN_NAMES:
            try:
                add_unique(legacy_dirs, StarTools.get_data_dir(name))
            except Exception as exc:  # noqa: BLE001 - optional migration source
                logger.debug("[ForumMemory] legacy StarTools path unavailable: %s", exc)

        # Older versions called get_data_dir() without a name.  Include that
        # result as a migration candidate when the runtime can resolve it.
        auto_dir: Path | None = None
        try:
            auto_dir = _as_path(StarTools.get_data_dir())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ForumMemory] auto-detected StarTools path unavailable: %s", exc)
        if auto_dir is not None:
            if canonical_dir is None and auto_dir.name == CANONICAL_PLUGIN_NAME:
                canonical_dir = auto_dir
            elif not any(cls._same_path(auto_dir, item) for item in legacy_dirs):
                legacy_dirs.append(auto_dir)

        if canonical_dir is None:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                root = _as_path(get_astrbot_data_path()) / "plugin_data"
                canonical_dir = root / CANONICAL_PLUGIN_NAME
                for name in LEGACY_PLUGIN_NAMES:
                    add_unique(legacy_dirs, root / name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[ForumMemory] unable to resolve AstrBot data path: %s", exc)
                canonical_dir = Path.cwd() / "data" / "plugin_data" / CANONICAL_PLUGIN_NAME

        canonical_dir = canonical_dir.resolve()
        canonical_path = canonical_dir / "forum_memory.json"
        legacy_paths = [
            directory / "forum_memory.json"
            for directory in legacy_dirs
            if not cls._same_path(directory, canonical_dir)
        ]
        return canonical_path, legacy_paths

    def __len__(self) -> int:
        with self._lock:
            return len(self._memories)
