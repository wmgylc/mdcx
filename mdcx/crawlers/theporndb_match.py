"""Filename parsing and conservative candidate matching for ThePornDB."""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from rapidfuzz import process, utils

from ..number import long_name
from ..utils.video import get_video_duration_seconds, parse_duration_seconds
from .theporndb_knowledge import KnownEntities, compact_name

MATCH_THRESHOLD = 94.9
MAX_PERFORMERS = 6
MAX_VENDOR_HINTS = 4
DURATION_TOLERANCE_SECONDS = 180

_DATE_RE = re.compile(
    r"(?<!\d)(?P<a>\d{2,4})[._/\- ](?P<b>\d{1,2})[._/\- ](?P<c>\d{1,4})(?!\d)",
    re.IGNORECASE,
)
_COMPACT_DATE_RE = re.compile(r"(?<!\d)(?P<value>\d{6}|\d{8})(?!\d)")
_QUALITY_RE = re.compile(
    r"\b(?:\d{3,4}x\d{3,4}|\d{2}fps|\d{3,4}p|\d+k)\b|\[web[- ]?dl-.*$",
    re.IGNORECASE,
)
_TECHNICAL_SUFFIX_RE = re.compile(r"\b(?:hevc|h\.?264|h\.?265|x264|x265|xvid|web[- ]?dl)\b.*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedTPDBFilename:
    site: str
    release_dates: tuple[str, ...]
    name: str

    @property
    def release_date(self) -> str:
        """Return the first parsed date for compatibility with single-date callers."""
        return self.release_dates[0] if self.release_dates else ""


@dataclass(frozen=True, slots=True)
class TPDBSearchVariant:
    site: str = ""
    release_date: str = ""
    name: str = ""

    @property
    def query(self) -> str:
        parts = [part for part in (self.site, self.release_date, self.name) if part]
        return ".".join(parts)


@dataclass(frozen=True, slots=True)
class CandidateMatch:
    site_match: bool
    date_match: bool
    name_score: float

    @property
    def is_match(self) -> bool:
        return self.site_match and self.date_match and self.name_score >= MATCH_THRESHOLD


def _clean_name(value: str) -> str:
    value = _TECHNICAL_SUFFIX_RE.sub("", value)
    value = re.sub(r"\bXXX\b.*$", "", value, flags=re.IGNORECASE)
    value = _QUALITY_RE.sub(" ", value)
    value = re.sub(r"[._\-]+", " ", value)
    return " ".join(value.split()).strip()


def _canonical_site(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]", "", value.lower())
    return long_name(compact) if compact else ""


def parse_tpdb_filename(file_path: str) -> ParsedTPDBFilename:
    """Parse site/date/name from the video basename; parent directories are ignored."""

    basename = str(file_path).replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return _parse_tpdb_name(stem)


def parse_tpdb_folder_name(folder_name: str) -> ParsedTPDBFilename:
    """Parse site/date/name hints from one folder name without treating its suffix as an extension."""

    basename = str(folder_name).replace("\\", "/").rsplit("/", 1)[-1]
    return _parse_tpdb_name(basename)


def _parse_tpdb_name(stem: str) -> ParsedTPDBFilename:
    date_match, release_dates = _find_filename_date(stem)

    if date_match:
        site_text = stem[: date_match.start()].strip(" ._-\t")
        name_text = stem[date_match.end() :].lstrip(" ._-\t")
    else:
        # Without a date boundary, token position alone cannot establish a site.
        # Keep the complete basename as the search/title hint; learned entities
        # are matched separately and may provide trustworthy vendor hints.
        site_text = ""
        name_text = stem

    site = _canonical_site(site_text)
    name = _clean_name(name_text)
    if name.upper().startswith("TS "):
        name = name[3:].lstrip()
    return ParsedTPDBFilename(site=site, release_dates=release_dates, name=name)


def _expand_year(value: str) -> int:
    year = int(value)
    if len(value) == 4:
        return year
    return 2000 + year if year <= 49 else 1900 + year


def _normalized_date(year: str, month: str, day: str) -> str | None:
    if len(year) not in (2, 4):
        return None
    full_year = _expand_year(year)
    try:
        return date(full_year, int(month), int(day)).isoformat()
    except ValueError:
        return None


def _date_interpretations(first: str, second: str, third: str) -> tuple[str, ...]:
    """Interpret year-first and year-last dates, retaining both valid month/day orders."""
    dates: list[str] = []

    def add(year: str, month: str, day: str) -> None:
        value = _normalized_date(year, month, day)
        if value and value not in dates:
            dates.append(value)

    # YY/MM/DD or YYYY/MM/DD.
    add(first, second, third)
    # MM/DD/YY or MM/DD/YYYY, and its DD/MM reverse.
    add(third, first, second)
    add(third, second, first)
    return tuple(dates)


def _find_filename_date(stem: str) -> tuple[re.Match[str] | None, tuple[str, ...]]:
    for pattern in (_DATE_RE, _COMPACT_DATE_RE):
        for match in pattern.finditer(stem):
            if pattern is _COMPACT_DATE_RE:
                value = match.group("value")
                if len(value) == 8:
                    dates = _date_interpretations(value[:4], value[4:6], value[6:])
                else:
                    dates = _date_interpretations(value[:2], value[2:4], value[4:])
            else:
                dates = _date_interpretations(match.group("a"), match.group("b"), match.group("c"))
            if dates:
                return match, dates
    return None, ()


def build_search_variants(parsed: ParsedTPDBFilename) -> list[TPDBSearchVariant]:
    """Return Namer-style progressively broader TPDB parse queries."""

    variants: list[TPDBSearchVariant] = []
    if parsed.site:
        for release_date in parsed.release_dates:
            variants.append(TPDBSearchVariant(parsed.site, release_date, parsed.name))
            variants.append(TPDBSearchVariant(parsed.site, release_date, ""))
        if parsed.release_dates and parsed.name:
            variants.append(TPDBSearchVariant(parsed.site, "", parsed.name))
        variants.append(TPDBSearchVariant(parsed.site, "", ""))
    else:
        for release_date in parsed.release_dates:
            variants.extend(
                [TPDBSearchVariant("", release_date, parsed.name), TPDBSearchVariant("", release_date, "")]
            )
        variants.append(TPDBSearchVariant("", "", parsed.name))

    unique: list[TPDBSearchVariant] = []
    seen: set[str] = set()
    for variant in variants:
        if variant.query and variant.query not in seen:
            unique.append(variant)
            seen.add(variant.query)
    return unique


def build_entity_search_specs(
    vendor_hints: Iterable[str], performer_hints: Iterable[str], release_dates: Iterable[str]
) -> list[tuple[str, str]]:
    """Build a bounded fallback plan without dropping parsed date interpretations."""
    vendors = list(dict.fromkeys(value for value in vendor_hints if value))[:MAX_VENDOR_HINTS]
    performers = list(dict.fromkeys(value for value in performer_hints if value))[:MAX_PERFORMERS]
    dates = list(dict.fromkeys(value for value in release_dates if value))

    specs = [(vendor, release_date) for vendor in vendors for release_date in dates]
    specs.extend(("", release_date) for release_date in dates)
    performer_site = vendors[0] if vendors else ""
    specs.extend((performer_site, performer) for performer in performers)
    if not dates and not performers:
        specs.extend((vendor, "") for vendor in vendors)

    # Filename parsing yields at most three date interpretations; these limits
    # bound the fallback plan to 4*3 + 3 + 6 = 21 requests.
    return list(dict.fromkeys(specs))


def _normalize_dates(value: Any) -> tuple[str, ...]:
    text = str(value or "").strip()
    iso_match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso_match:
        normalized = _normalized_date(*iso_match.groups())
        return (normalized,) if normalized else ()
    _, dates = _find_filename_date(text)
    return dates


def _normalize_date(value: Any) -> str:
    dates = _normalize_dates(value)
    return dates[0] if dates else ""


def _site_matches(parsed_site: str, candidate: dict[str, Any]) -> bool:
    site = candidate.get("site") or {}
    if not isinstance(site, dict):
        return False
    candidate_names = [site.get("short_name"), site.get("name")]
    candidate_names = [re.sub(r"[^a-z0-9]", "", str(name).lower()) for name in candidate_names if name]
    if not parsed_site:
        return bool(candidate_names)
    return any(parsed_site in candidate_name for candidate_name in candidate_names)


def _ascii_fold(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def _performer_names(candidate: dict[str, Any]) -> list[str]:
    performers = candidate.get("performers") or []
    if not isinstance(performers, list) or len(performers) > MAX_PERFORMERS:
        return []
    return [str(item.get("name") or "").strip() for item in performers if isinstance(item, dict) and item.get("name")]


def _name_choices(title: str, performers: list[str]) -> list[str]:
    terms = ([title] if title else []) + performers
    return [" ".join(parts) for size in range(1, len(terms) + 1) for parts in itertools.combinations(terms, size)]


def _best_name_score(query: str, choices: list[str]) -> float:
    if not query or not choices:
        return 0.0
    best = 0.0
    for normalized_query in dict.fromkeys((query, _ascii_fold(query))):
        result = process.extractOne(normalized_query, choices, processor=utils.default_process)
        if result:
            best = max(best, float(result[1]))
    return best


def score_candidate(parsed: ParsedTPDBFilename, candidate: dict[str, Any]) -> CandidateMatch:
    site_match = _site_matches(parsed.site, candidate)
    candidate_dates = set(_normalize_dates(candidate.get("date")))
    date_match = bool(set(parsed.release_dates).intersection(candidate_dates))

    title = str(candidate.get("title") or "").strip()
    performers = _performer_names(candidate)
    name_score = _best_name_score(parsed.name, _name_choices(title, performers))
    if name_score < 89.9:
        first_names = [performer.split()[0] for performer in performers if performer.split()]
        name_score = max(name_score, _best_name_score(parsed.name, _name_choices(title, first_names)))

    return CandidateMatch(site_match=site_match, date_match=date_match, name_score=name_score)


def find_matching_candidates(parsed: ParsedTPDBFilename, candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [candidate for candidate in candidates if score_candidate(parsed, candidate).is_match]


def merge_candidates(existing: list[dict[str, Any]], incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate search results while retaining the richer representation."""

    merged = [dict(candidate) for candidate in existing]
    indexes = {_candidate_key(candidate): index for index, candidate in enumerate(merged)}
    for candidate in incoming:
        if not isinstance(candidate, dict):
            continue
        key = _candidate_key(candidate)
        if key not in indexes:
            indexes[key] = len(merged)
            merged.append(dict(candidate))
            continue

        current = merged[indexes[key]]
        for field, value in candidate.items():
            if not current.get(field) and value:
                current[field] = value
            elif field == "performers" and isinstance(value, list) and len(value) > len(current.get(field, [])):
                current[field] = value
    return merged


def _candidate_key(candidate: dict[str, Any]) -> str:
    identity = candidate.get("slug") or candidate.get("id") or candidate.get("url")
    if identity:
        return str(identity).lower()
    site = candidate.get("site") or {}
    site_name = site.get("short_name", "") if isinstance(site, dict) else ""
    performers = ",".join(_performer_names(candidate))
    return "|".join(
        (str(candidate.get("title") or ""), str(candidate.get("date") or ""), str(site_name), performers)
    ).lower()


def select_unique_candidate(parsed: ParsedTPDBFilename, candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    matches = find_matching_candidates(parsed, candidates)
    return matches[0] if len(matches) == 1 else None


def _candidate_vendor_names(candidate: dict[str, Any]) -> list[str]:
    site = candidate.get("site") or {}
    if isinstance(site, str):
        return [site] if site else []
    if not isinstance(site, dict):
        return []
    names = [site.get("name"), site.get("short_name")]
    for key in ("parent", "network"):
        parent = site.get(key)
        if isinstance(parent, dict):
            names.extend((parent.get("name"), parent.get("short_name")))
    return [str(name).strip() for name in names if name]


def _candidate_all_performer_names(candidate: dict[str, Any]) -> list[str]:
    performers = candidate.get("performers") or []
    if not isinstance(performers, list):
        return []
    names = []
    for performer in performers:
        if not isinstance(performer, dict):
            continue
        name = performer.get("name")
        parent = performer.get("parent")
        if not name and isinstance(parent, dict):
            name = parent.get("name")
        if name:
            names.append(str(name).strip())
    return names


def candidate_audit_summary(candidate: dict[str, Any], decision: str = "") -> str:
    def clean(value: Any, limit: int = 100) -> str:
        text = " ".join(str(value or "").split())
        return text[:limit] + ("…" if len(text) > limit else "")

    identity = candidate.get("slug") or candidate.get("id") or "-"
    title = candidate.get("title") or "-"
    vendors = ", ".join(_candidate_vendor_names(candidate)[:3]) or "-"
    performers = ", ".join(_candidate_all_performer_names(candidate)[:6]) or "-"
    prefix = f"{decision}: " if decision else ""
    return (
        f"{prefix}id={clean(identity, 60)}, title={clean(title)}, "
        f"vendor={clean(vendors)}, performers={clean(performers)}, "
        f"date={clean(candidate.get('date') or '-')}, duration={clean(candidate.get('duration') or '-')}"
    )


def explain_filename_candidates(
    parsed: ParsedTPDBFilename,
    candidates: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Explain the site/date/name checks used by strict filename-query matching."""
    candidate_list = [candidate for candidate in candidates if isinstance(candidate, dict)]
    counts = {
        "candidates": len(candidate_list),
        "site_mismatch": 0,
        "date_missing": 0,
        "date_mismatch": 0,
        "name_mismatch": 0,
        "matched": 0,
    }
    matches: list[dict[str, Any]] = []
    examples: list[str] = []
    examples_by_decision: dict[str, int] = {}

    for candidate in candidate_list:
        score = score_candidate(parsed, candidate)
        if not score.site_match:
            decision = "拒绝-site"
            counts["site_mismatch"] += 1
        elif not parsed.release_dates:
            decision = "拒绝-date-missing"
            counts["date_missing"] += 1
        elif not score.date_match:
            decision = "拒绝-date"
            counts["date_mismatch"] += 1
        elif score.name_score < MATCH_THRESHOLD:
            decision = "拒绝-name"
            counts["name_mismatch"] += 1
        else:
            decision = "接受"
            counts["matched"] += 1
            matches.append(candidate)

        if examples_by_decision.get(decision, 0) < 2:
            examples_by_decision[decision] = examples_by_decision.get(decision, 0) + 1
            examples.append(
                f"{candidate_audit_summary(candidate, decision)}; "
                f"site_match={score.site_match}, date_match={score.date_match}, "
                f"name_score={score.name_score:.1f}/{MATCH_THRESHOLD:.1f}"
            )

    return matches, counts, examples


def explain_full_filename_title_candidates(
    filename: str | Path,
    candidates: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Compare the entire cleaned video basename with candidate title/performer combinations."""
    basename = Path(str(filename).replace("\\", "/")).name
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    query = _clean_name(stem)
    candidate_list = [candidate for candidate in candidates if isinstance(candidate, dict)]
    counts = {"candidates": len(candidate_list), "matched": 0, "name_mismatch": 0}
    matches: list[dict[str, Any]] = []
    examples: list[str] = []
    examples_by_decision: dict[str, int] = {}

    for candidate in candidate_list:
        title = _clean_name(str(candidate.get("title") or ""))
        performers = _performer_names(candidate)
        score = _best_name_score(query, _name_choices(title, performers))
        if score >= MATCH_THRESHOLD:
            decision = "接受-full-title"
            counts["matched"] += 1
            matches.append(candidate)
        else:
            decision = "拒绝-full-title"
            counts["name_mismatch"] += 1

        if examples_by_decision.get(decision, 0) < 4:
            examples_by_decision[decision] = examples_by_decision.get(decision, 0) + 1
            examples.append(
                f"{candidate_audit_summary(candidate, decision)}; "
                f"filename_title={query or '-'}, name_score={score:.1f}/{MATCH_THRESHOLD:.1f}"
            )

    return matches, counts, examples


def _duration_seconds(value: Any) -> float | None:
    return parse_duration_seconds(value, bare_unit="seconds")


def duration_tolerance_seconds(left: float, right: float) -> float:
    return DURATION_TOLERANCE_SECONDS


def duration_within_tolerance(left: float | None, right: float | None) -> bool | None:
    if left is None or right is None:
        return None
    return abs(left - right) <= duration_tolerance_seconds(left, right)


def explain_duration_candidates(
    candidates: Iterable[dict[str, Any]], local_duration_seconds: float | None
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Use runtime as the final gate, accepting only one candidate inside tolerance."""
    candidate_list = [candidate for candidate in candidates if isinstance(candidate, dict)]
    counts = {
        "candidates": len(candidate_list),
        "within_tolerance": 0,
        "duration_mismatch": 0,
        "duration_unknown": 0,
        "ambiguous": 0,
    }
    eligible: list[tuple[dict[str, Any], float, float]] = []
    examples: list[str] = []
    examples_by_decision: dict[str, int] = {}

    def record(candidate: dict[str, Any], decision: str, delta: float | None, tolerance: float | None) -> None:
        if examples_by_decision.get(decision, 0) >= 4:
            return
        examples_by_decision[decision] = examples_by_decision.get(decision, 0) + 1
        candidate_duration = _duration_seconds(candidate.get("duration"))
        runtime_detail = (
            f"; runtime_local={local_duration_seconds:.1f}s, runtime_candidate={candidate_duration:.1f}s, "
            f"runtime_delta={delta:.1f}s, runtime_tolerance=±{tolerance:.1f}s"
            if local_duration_seconds is not None and candidate_duration is not None and delta is not None and tolerance is not None
            else "; runtime_compare=unknown"
        )
        examples.append(candidate_audit_summary(candidate, decision) + runtime_detail)

    for candidate in candidate_list:
        candidate_duration = _duration_seconds(candidate.get("duration"))
        if local_duration_seconds is None or candidate_duration is None:
            counts["duration_unknown"] += 1
            record(candidate, "拒绝-runtime-unknown", None, None)
            continue
        delta = abs(local_duration_seconds - candidate_duration)
        tolerance = duration_tolerance_seconds(local_duration_seconds, candidate_duration)
        if delta <= tolerance:
            counts["within_tolerance"] += 1
            eligible.append((candidate, delta, tolerance))
            record(candidate, "时长通过", delta, tolerance)
        else:
            counts["duration_mismatch"] += 1
            record(candidate, "拒绝-duration", delta, tolerance)

    if len(eligible) == 1:
        selected = [eligible[0][0]]
    else:
        selected = []
        if len(eligible) > 1:
            counts["ambiguous"] = len(eligible)
            for candidate, delta, tolerance in eligible:
                record(candidate, "拒绝-runtime-ambiguous", delta, tolerance)
    return selected, counts, examples


# Compatibility alias; keep one PyAV duration reader for the project.
get_media_duration_seconds = get_video_duration_seconds


def explain_known_entity_candidates(
    candidates: Iterable[dict[str, Any]],
    matched_entities: KnownEntities,
    release_date: str = "",
    release_dates: Iterable[str] = (),
    site_hint: str = "",
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Filter fallback candidates by learned names and filename-derived site/date hints."""

    candidate_list = [candidate for candidate in candidates if isinstance(candidate, dict)]
    vendor_keys = {
        compact_name(name) for name in (*matched_entities.vendors, site_hint) if compact_name(name)
    }
    performer_keys = {compact_name(name) for name in matched_entities.performers if compact_name(name)}
    expected_dates = set(release_dates)
    if release_date:
        expected_dates.add(release_date)
    counts = {
        "candidates": len(candidate_list),
        "eligible": 0,
        "vendor_mismatch": 0,
        "performer_mismatch": 0,
        "date_mismatch": 0,
        "date_unknown": 0,
        "missing_hints": 0,
    }
    examples: list[str] = []
    examples_by_decision: dict[str, int] = {}

    def record_example(candidate: dict[str, Any], decision: str) -> None:
        if examples_by_decision.get(decision, 0) >= 2:
            return
        examples_by_decision[decision] = examples_by_decision.get(decision, 0) + 1
        examples.append(candidate_audit_summary(candidate, decision))

    if not vendor_keys and not performer_keys and not expected_dates:
        counts["missing_hints"] = 1
        return [], counts, examples

    matches: list[dict[str, Any]] = []
    for candidate in candidate_list:
        candidate_vendors = {compact_name(name) for name in _candidate_vendor_names(candidate)}
        candidate_performers = {compact_name(name) for name in _candidate_all_performer_names(candidate)}
        if vendor_keys and not vendor_keys.intersection(candidate_vendors):
            counts["vendor_mismatch"] += 1
            record_example(candidate, "拒绝-vendor")
            continue
        if performer_keys and not performer_keys.issubset(candidate_performers):
            counts["performer_mismatch"] += 1
            record_example(candidate, "拒绝-performer")
            continue

        candidate_dates = set(_normalize_dates(candidate.get("date")))
        if expected_dates and candidate_dates and not expected_dates.intersection(candidate_dates):
            counts["date_mismatch"] += 1
            record_example(candidate, "拒绝-date")
            continue
        if expected_dates and not candidate_dates:
            counts["date_unknown"] += 1

        matches.append(candidate)
        counts["eligible"] += 1
        record_example(candidate, "符合条件")

    return matches, counts, examples


def find_known_entity_candidates(
    candidates: Iterable[dict[str, Any]],
    matched_entities: KnownEntities,
    release_date: str = "",
    release_dates: Iterable[str] = (),
    site_hint: str = "",
) -> list[dict[str, Any]]:
    """Use learned entities and filename-derived site/date hints for fallback candidates."""

    matches, _, _ = explain_known_entity_candidates(
        candidates,
        matched_entities,
        release_date=release_date,
        release_dates=release_dates,
        site_hint=site_hint,
    )
    return matches
