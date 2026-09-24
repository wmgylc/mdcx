import pytest

from mdcx.crawlers.theporndb_knowledge import (
    KnownEntities,
    find_known_entities,
    load_known_entities,
    remember_entities,
)
from mdcx.crawlers.theporndb_match import (
    build_entity_search_specs,
    build_search_variants,
    duration_tolerance_seconds,
    duration_within_tolerance,
    explain_duration_candidates,
    explain_filename_candidates,
    explain_known_entity_candidates,
    find_known_entity_candidates,
    parse_tpdb_filename,
    score_candidate,
    select_unique_candidate,
)


def _candidate(slug: str) -> dict:
    return {
        "slug": slug,
        "title": "Whale",
        "date": "2020-02-13",
        "site": {"name": "Deeper", "short_name": "deeper"},
        "performers": [{"name": "Kayden Kross"}, {"name": "Kenna James"}],
    }


def test_parser_extracts_filename_fields_and_ignores_parent_directories():
    parsed = parse_tpdb_filename(
        "/library/@Wrong Actor/JulesJordan.22.12.22.Kenna.James.And.Manuels.Fat.Cock.Are.A.Perfect.Fit.XXX.1080p.HEVC.x265.PRT.mp4"
    )

    assert parsed.site == "julesjordan"
    assert parsed.release_date == "2022-12-22"
    assert parsed.release_dates == ("2022-12-22",)
    assert parsed.name.casefold() == "kenna james and manuels fat cock are a perfect fit"


def test_parser_cleans_release_tags_and_builds_progressively_broader_queries():
    parsed = parse_tpdb_filename("Deeper.20.02.13.Kayden.Kross.and.Kenna.James.XXX.2160p.mp4")

    assert parsed.site == "deeper"
    assert parsed.release_date == "2020-02-13"
    assert parsed.release_dates == ("2020-02-13", "2013-02-20")
    assert parsed.name == "Kayden Kross and Kenna James"
    assert [variant.query for variant in build_search_variants(parsed)] == [
        "deeper.2020-02-13.Kayden Kross and Kenna James",
        "deeper.2020-02-13",
        "deeper.2013-02-20.Kayden Kross and Kenna James",
        "deeper.2013-02-20",
        "deeper.Kayden Kross and Kenna James",
        "deeper",
    ]


def test_parser_supports_date_orders_and_keeps_both_ambiguous_interpretations():
    parsed = parse_tpdb_filename("BlackedRaw.03-04-21.Scene.Title.mp4")

    assert parsed.site == "blackedraw"
    assert parsed.release_dates == ("2003-04-21", "2021-03-04", "2021-04-03")
    queries = [variant.query for variant in build_search_variants(parsed)]
    assert "blackedraw.2021-03-04.Scene Title" in queries
    assert "blackedraw.2021-04-03.Scene Title" in queries


def test_fallback_query_plan_keeps_all_dates_and_performer_hints_within_bound():
    vendors = ("deeper", "BlackedRaw", "Site C", "Site D")
    performers = ("Actor One", "Actor Two", "Actor Three", "Actor Four", "Actor Five", "Actor Six")
    dates = ("2021-03-04", "2021-04-03", "2003-04-21")

    specs = build_entity_search_specs(vendors, performers, dates)

    assert len(specs) == 21
    assert len(specs) == len(set(specs))
    assert all((vendor, date) in specs for vendor in vendors for date in dates)
    assert all(("", date) in specs for date in dates)
    assert all((vendors[0], performer) in specs for performer in performers)
    assert build_entity_search_specs((), performers[:1], ()) == [("", performers[0])]
    assert build_entity_search_specs(("deeper",), (), ()) == [("deeper", "")]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Deeper.21_09_09.mp4", ("2021-09-09", "2009-09-21")),
        ("Deeper.2021_9_9.mp4", ("2021-09-09",)),
        ("Deeper.20210909.mp4", ("2021-09-09",)),
        ("Deeper.210909.mp4", ("2021-09-09", "2009-09-21")),
    ],
)
def test_parser_supports_slash_unpadded_and_compact_dates(filename, expected):
    assert parse_tpdb_filename(filename).release_dates == expected


def test_candidate_match_uses_combinations_of_title_and_performers():
    parsed = parse_tpdb_filename("Deeper.20.02.13.Kayden.Kross.and.Kenna.James.XXX.2160p.mp4")
    candidate = _candidate("whale")

    score = score_candidate(parsed, candidate)

    assert score.site_match
    assert score.date_match
    assert score.name_score >= 94.9
    assert select_unique_candidate(parsed, [candidate]) == candidate


def test_candidate_match_rejects_wrong_site_date_and_ambiguous_matches():
    parsed = parse_tpdb_filename("Deeper.20.02.13.Kayden.Kross.and.Kenna.James.XXX.2160p.mp4")
    candidate = _candidate("whale")

    assert select_unique_candidate(parsed, [{**candidate, "date": "2021-02-13"}]) is None
    assert (
        select_unique_candidate(parsed, [{**candidate, "site": {"name": "Other Studio", "short_name": "other"}}])
        is None
    )
    assert select_unique_candidate(parsed, [candidate, {**candidate, "slug": "second-whale"}]) is None


def test_filename_candidate_audit_explains_site_date_and_name_rejections():
    parsed = parse_tpdb_filename("Deeper.20.02.13.Kayden.Kross.and.Kenna.James.mp4")
    correct = _candidate("whale")
    wrong_site = {**correct, "slug": "wrong-site", "site": {"name": "Other", "short_name": "other"}}
    wrong_date = {**correct, "slug": "wrong-date", "date": "2021-02-13"}
    wrong_name = {**correct, "slug": "wrong-name", "title": "A completely unrelated title", "performers": []}

    matches, counts, examples = explain_filename_candidates(
        parsed, [correct, wrong_site, wrong_date, wrong_name]
    )

    assert matches == [correct]
    assert counts == {
        "candidates": 4,
        "site_mismatch": 1,
        "date_missing": 0,
        "date_mismatch": 1,
        "name_mismatch": 1,
        "matched": 1,
    }
    assert any("拒绝-site" in example for example in examples)
    assert any("拒绝-date" in example for example in examples)
    assert any("拒绝-name" in example for example in examples)


def test_filename_without_date_is_not_auto_matched():
    parsed = parse_tpdb_filename("Deeper.Kayden.Kross.and.Kenna.James.mp4")

    assert parsed.site == "deeper"
    assert parsed.release_date == ""
    assert select_unique_candidate(parsed, [_candidate("whale")]) is None


def test_successful_nfo_entities_are_persisted_and_matched_from_basename(tmp_path):
    added = remember_entities(
        sites=["BlackedRaw"],
        studios=["Gamma Enterprises"],
        performers=["Evelyn Claire", "EvelynClaire"],
        data_folder=tmp_path,
    )

    assert added == 3
    entities = load_known_entities(tmp_path)
    matched = find_known_entities(
        "/library/@Wrong Actor/BlackedRaw-EvelynClaire-Trouble.Trouble.1080p.mp4",
        entities,
    )

    assert matched.sites == ("BlackedRaw",)
    assert matched.performers == ("Evelyn Claire",)


def test_known_entity_matching_requires_name_boundaries_but_allows_punctuationless_names():
    known = KnownEntities(sites=("BlackedRaw",), performers=("Evelyn Claire",))

    matched = find_known_entities("BlackedRaw-EvelynClaire-Trouble.mp4", known)
    partial = find_known_entities("XBlackedRaw-EvelynClaireX-Trouble.mp4", known)

    assert matched.sites == ("BlackedRaw",)
    assert matched.performers == ("Evelyn Claire",)
    assert find_known_entities("BlackedRaw_Evelyn_Claire-Trouble.mp4", known) == matched
    assert partial.sites == ()
    assert partial.performers == ()


def test_corrupt_dictionary_is_not_overwritten(tmp_path):
    dictionary_path = tmp_path / "theporndb_entities.json"
    dictionary_path.write_text("{broken", encoding="utf-8")

    assert load_known_entities(tmp_path) == KnownEntities()

    try:
        remember_entities(performers=["Evelyn Claire"], data_folder=tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid dictionaries must not be silently replaced")

    assert dictionary_path.read_text(encoding="utf-8") == "{broken"


def test_known_entity_fallback_accepts_duration_within_tolerance_only():
    known = KnownEntities(sites=("BlackedRaw",), performers=("Evelyn Claire",))
    local_duration = 3368.32
    near = {
        "slug": "trouble-trouble",
        "title": "Trouble Trouble",
        "duration": 3500,
        "site": {"name": "BlackedRaw", "short_name": "blackedraw"},
        "performers": [{"name": "Evelyn Claire"}],
    }
    far = {**near, "slug": "another-scene", "duration": 2396}

    assert duration_tolerance_seconds(local_duration, near["duration"]) == 180
    assert duration_within_tolerance(local_duration, near["duration"])
    assert not duration_within_tolerance(local_duration, far["duration"])
    candidates = find_known_entity_candidates([near, far], known)
    assert candidates == [near, far]
    assert explain_duration_candidates(candidates, local_duration)[0] == [near]


def test_duration_tolerance_is_fixed_at_three_minutes():
    assert duration_tolerance_seconds(1800, 2400) == 180
    assert duration_within_tolerance(1800, 1980)
    assert not duration_within_tolerance(1800, 1981)
    assert not duration_within_tolerance(3600, 3900)


def test_known_entity_fallback_keeps_multiple_in_range_candidates_ambiguous():
    known = KnownEntities(sites=("BlackedRaw",), performers=("Evelyn Claire",))
    first = {
        "slug": "first",
        "title": "First Scene",
        "duration": 3300,
        "site": {"name": "BlackedRaw"},
        "performers": [{"name": "Evelyn Claire"}],
    }
    second = {**first, "slug": "second", "title": "Second Scene", "duration": 3400}

    candidates = find_known_entity_candidates([first, second], known)
    matches, counts, _ = explain_duration_candidates(candidates, 3368)

    assert matches == []
    assert counts["within_tolerance"] == 2
    assert counts["ambiguous"] == 2


def test_site_and_date_fallback_does_not_require_a_filename_performer():
    candidate = {
        "slug": "deeper-succubus",
        "title": "Succubus",
        "date": "2021-09-09",
        "duration": 2362,
        "site": {"name": "Deeper", "short_name": "deeper"},
        "performers": [{"name": "Kenna James"}],
    }

    matches, counts, _ = explain_known_entity_candidates(
        [candidate],
        KnownEntities(),
        site_hint="deeper",
        release_dates=("2021-09-09", "2009-09-21"),
    )

    assert matches == [candidate]
    assert counts["eligible"] == 1


def test_known_entity_fallback_explains_candidate_rejections():
    known = KnownEntities(sites=("BlackedRaw",), performers=("Evelyn Claire",))
    accepted = {
        "slug": "accepted",
        "title": "Trouble Trouble",
        "date": "2022-12-22",
        "duration": 3300,
        "site": {"name": "BlackedRaw"},
        "performers": [{"name": "Evelyn Claire"}],
    }
    candidates = [
        {**accepted, "slug": "wrong-vendor", "site": {"name": "Other"}},
        {**accepted, "slug": "wrong-performer", "performers": [{"name": "Other Actor"}]},
        {**accepted, "slug": "wrong-date", "date": "2023-12-22"},
        {**accepted, "slug": "wrong-duration", "duration": 5200},
        accepted,
    ]

    matches, counts, examples = explain_known_entity_candidates(
        candidates,
        known,
        release_date="2022-12-22",
    )

    assert matches == [
        {**accepted, "slug": "wrong-duration", "duration": 5200},
        accepted,
    ]
    assert counts == {
        "candidates": 5,
        "eligible": 2,
        "vendor_mismatch": 1,
        "performer_mismatch": 1,
        "date_mismatch": 1,
        "date_unknown": 0,
        "missing_hints": 0,
    }
    assert any("拒绝-vendor" in example for example in examples)
    assert any("拒绝-performer" in example for example in examples)
    assert any("拒绝-date" in example for example in examples)
    duration_matches, duration_counts, duration_examples = explain_duration_candidates(matches, 3300)
    assert duration_matches == [accepted]
    assert duration_counts["duration_mismatch"] == 1
    assert any("拒绝-duration" in example for example in duration_examples)
