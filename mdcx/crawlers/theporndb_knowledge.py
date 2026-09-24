"""Persistent studio/site and performer names learned from successful NFOs."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_LOCK = threading.RLock()
_LOGGER = logging.getLogger(__name__)
_FILENAME = "theporndb_entities.json"
_CATEGORIES = ("sites", "studios", "performers")


@dataclass(frozen=True, slots=True)
class KnownEntities:
    sites: tuple[str, ...] = ()
    studios: tuple[str, ...] = ()
    performers: tuple[str, ...] = ()

    @property
    def vendors(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.sites, *self.studios)))


def _dictionary_path(data_folder: Path | None = None) -> Path:
    if data_folder is None:
        from ..config.manager import manager

        data_folder = manager.data_folder
    return Path(data_folder) / _FILENAME


def compact_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in folded if char.isalnum())


def _clean_names(values: Iterable[object]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = str(value or "").strip()
        key = compact_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _read(path: Path) -> dict[str, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取 ThePornDB 词典 {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"ThePornDB 词典格式无效: {path}")

    values = {}
    for category in _CATEGORIES:
        raw_names = payload.get(category, [])
        if not isinstance(raw_names, list):
            raise ValueError(f"ThePornDB 词典字段 {category} 格式无效: {path}")
        values[category] = _clean_names(raw_names)
    return values


def load_known_entities(data_folder: Path | None = None) -> KnownEntities:
    path = _dictionary_path(data_folder)
    with _LOCK:
        try:
            values = _read(path)
        except (OSError, ValueError) as error:
            _LOGGER.warning("读取 ThePornDB 演员/厂商词典失败，将按空词典继续匹配: %s", error)
            values = {category: [] for category in _CATEGORIES}
    return KnownEntities(**{key: tuple(value) for key, value in values.items()})


def remember_entities(
    *,
    sites: Iterable[object] = (),
    studios: Iterable[object] = (),
    performers: Iterable[object] = (),
    data_folder: Path | None = None,
) -> int:
    """Merge NFO entity names into the per-profile dictionary; return names added."""

    path = _dictionary_path(data_folder)
    incoming = {
        "sites": _clean_names(sites),
        "studios": _clean_names(studios),
        "performers": _clean_names(performers),
    }
    with _LOCK:
        values = _read(path)
        added = 0
        for category, names in incoming.items():
            existing_keys = {compact_name(name) for name in values[category]}
            for name in names:
                if compact_name(name) not in existing_keys:
                    values[category].append(name)
                    existing_keys.add(compact_name(name))
                    added += 1

        if added:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary_path.write_text(
                    json.dumps({"version": 1, **values}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary_path, path)
            finally:
                temporary_path.unlink(missing_ok=True)
    return added


def _fold_for_match(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in folded if not unicodedata.combining(char))


def _entity_pattern(name: str) -> re.Pattern[str] | None:
    words = [word for word in re.split(r"[\W_]+", _fold_for_match(name)) if word]
    if not words:
        return None
    pattern = r"(?<![^\W_])" + r"[\W_]*".join(re.escape(word) for word in words) + r"(?![^\W_])"
    return re.compile(pattern, re.UNICODE)


def _find_names_in_basename(values: Iterable[str], basename: str) -> tuple[str, ...]:
    matches: list[tuple[int, int, int, str]] = []
    ordered = sorted(values, key=lambda value: len(compact_name(value)), reverse=True)
    folded_basename = _fold_for_match(basename)
    for priority, name in enumerate(ordered):
        if len(compact_name(name)) < 5:
            continue
        pattern = _entity_pattern(name)
        if pattern is None:
            continue
        matches.extend(
            (match.start(), match.end(), priority, name)
            for match in pattern.finditer(folded_basename)
        )

    matches.sort(key=lambda item: (item[2], item[0]))
    selected: list[str] = []
    selected_set: set[str] = set()
    occupied: list[tuple[int, int]] = []
    for start, end, _, name in matches:
        if name in selected_set:
            continue
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        selected.append(name)
        selected_set.add(name)
        occupied.append((start, end))
    return tuple(selected)


def find_known_entities(file_path: str | Path, entities: KnownEntities) -> KnownEntities:
    """Match learned names against the video basename only; parent folders are ignored."""

    basename = str(file_path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return KnownEntities(
        sites=_find_names_in_basename(entities.sites, stem),
        studios=_find_names_in_basename(entities.studios, stem),
        performers=_find_names_in_basename(entities.performers, stem),
    )
