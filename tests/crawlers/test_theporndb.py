from urllib.parse import parse_qs, urlparse

import oshash
import pytest

from mdcx.config.enums import Language, Switch, Website
from mdcx.config.manager import manager
from mdcx.crawlers.base import get_crawler
from mdcx.crawlers.theporndb import TheporndbCrawler, read_data
from mdcx.models.types import CrawlerInput


class FakeTheporndbClient:
    async def get_json(self, url, **kwargs):
        if url == "https://api.theporndb.net/scenes/test-scene":
            return {"data": _api_data("test-scene", "scenes")}, ""
        if url == "https://api.theporndb.net/scenes/test-movie":
            return {"data": None}, ""
        if url == "https://api.theporndb.net/movies/test-movie":
            return {"data": _api_data("test-movie", "movies")}, ""
        return None, f"unexpected url: {url}"


class FakeTheporndbHashHitClient:
    async def get_json(self, url, **kwargs):
        if url == "https://api.theporndb.net/scenes/hash/fakehash":
            return {"data": _api_data("hash-hit", "scenes", title="Hash Hit")}, ""
        return None, f"unexpected url: {url}"


class FakeTheporndbDeeperClient:
    async def get_json(self, url, **kwargs):
        candidate = _api_data(
            "deeper-succubus",
            "scenes",
            title="Succubus",
            date="2021-09-09",
            site_short_name="deeper",
            site_name="Deeper",
            duration=2362,
            performers=[{"name": "Kenna James", "parent": {"extras": {"gender": "Female"}}}],
        )
        if url == "https://api.theporndb.net/scenes/hash/fakehash":
            return None, "HTTP 404"
        if "2021-09-09" in url and ("parse=" in url or "q=" in url):
            return {"data": [candidate]}, ""
        if url == "https://api.theporndb.net/scenes/deeper-succubus":
            return {"data": candidate}, ""
        return {"data": []}, ""


class FakeTheporndbSearchClient:
    async def get_json(self, url, **kwargs):
        if url == "https://api.theporndb.net/scenes/hash/fakehash":
            return None, "HTTP 404"
        if url == "https://api.theporndb.net/scenes?parse=nurumassage.2026-02-23.ellie%20nova&per_page=100":
            return {
                "data": [
                    _api_data(
                        "nurumassage-ellie-nova",
                        "scenes",
                        title="Ellie Nova",
                        date="2026-02-23",
                        site_short_name="nurumassage",
                        site_name="Nuru Massage",
                    )
                ]
            }, ""
        if "?parse=" in url:
            return {"data": []}, ""
        if url == "https://api.theporndb.net/scenes/nurumassage-ellie-nova":
            return {"data": _api_data("nurumassage-ellie-nova", "scenes", title="Ellie Nova", date="2026-02-23")}, ""
        return None, f"unexpected url: {url}"


class FakeTheporndbEntityFallbackClient:
    def __init__(self):
        self.urls = []

    async def get_json(self, url, **kwargs):
        self.urls.append(url)
        if url == "https://api.theporndb.net/scenes/hash/fakehash":
            return None, "HTTP 404"
        if "?parse=" in url:
            return {"data": []}, ""
        query = parse_qs(urlparse(url).query)
        if query.get("site") and query.get("q") == ["Evelyn Claire"]:
            return {
                "data": [
                    _api_data(
                        "trouble-trouble",
                        "scenes",
                        title="Trouble Trouble",
                        date="2019-08-05",
                        site_short_name="blackedraw",
                        site_name="BlackedRaw",
                        duration=3368,
                        performers=[{"name": "Evelyn Claire", "parent": {"extras": {"gender": "Female"}}}],
                    ),
                    _api_data(
                        "vacation-scene",
                        "scenes",
                        title="My Hot Girlfriend's Adventures On Vacation",
                        date="2017-09-29",
                        site_short_name="blackedraw",
                        site_name="BlackedRaw",
                        duration=2396,
                        performers=[{"name": "Evelyn Claire", "parent": {"extras": {"gender": "Female"}}}],
                    ),
                ]
            }, ""
        if url == "https://api.theporndb.net/scenes/trouble-trouble":
            return {
                "data": _api_data(
                    "trouble-trouble",
                    "scenes",
                    title="Trouble Trouble",
                    date="2019-08-05",
                    site_short_name="blackedraw",
                    site_name="BlackedRaw",
                    duration=3368,
                    performers=[{"name": "Evelyn Claire", "parent": {"extras": {"gender": "Female"}}}],
                )
            }, ""
        return {"data": []}, ""


def _api_data(
    slug: str,
    kind: str,
    title: str | None = None,
    date: str = "2026-04-03",
    site_short_name: str = "seriesa",
    site_name: str = "Series A",
    duration: int = 7200,
    performers: list[dict] | None = None,
) -> dict:
    return {
        "slug": slug,
        "title": title or f"{kind} title",
        "description": "Outline",
        "date": date,
        "trailer": "https://example.test/trailer.mp4",
        "background": {"large": "https://example.test/cover.jpg"},
        "posters": {"large": "https://example.test/poster.jpg"},
        "duration": duration,
        "site": {"name": site_name, "short_name": site_short_name, "network": {"name": "Network A"}},
        "director": {"name": "Director A"},
        "tags": [{"name": "Tag A"}],
        "performers": performers or [
            {"name": "Actor A", "parent": {"extras": {"gender": "Female"}}},
            {"name": "Actor B", "parent": {"extras": {"gender": "Male"}}},
        ],
    }


def _input(appoint_url: str) -> CrawlerInput:
    return CrawlerInput(
        appoint_number="",
        appoint_url=appoint_url,
        file_path=None,
        mosaic="",
        number="SceneTest",
        short_number="SceneTest",
        language=Language.JP,
        org_language=Language.JP,
    )


def _file_input() -> CrawlerInput:
    data = _input("")
    data.number = "Nurumassage.26.02.23"
    data.short_number = "Nurumassage.26.02.23"
    data.file_path = "D:/Code/Personal/daiguaxiao/nurumassage.26.02.23.ellie.nova.mp4"
    return data


@pytest.mark.asyncio
async def test_theporndb_crawler_reads_scene_detail_url():
    old_token = manager.config.theporndb_api_token
    manager.config.theporndb_api_token = "token"
    try:
        crawler = TheporndbCrawler(client=FakeTheporndbClient())
        res = await crawler.run(_input("https://theporndb.net/scenes/test-scene"))
    finally:
        manager.config.theporndb_api_token = old_token

    assert res.debug_info.error is None
    assert res.data is not None
    assert res.data.source == "theporndb"
    assert res.data.number == "SeriesA.26.04.03"
    assert res.data.title == "scenes title"
    assert res.data.actors == ["Actor A"]
    assert res.data.all_actors == ["Actor A", "Actor B"]
    assert res.data.directors == ["Director A"]
    assert res.data.tags == ["Tag A"]
    assert res.data.runtime == "120"
    assert res.data.thumb == "https://example.test/cover.jpg"
    assert res.data.poster == "https://example.test/poster.jpg"
    assert res.data.external_id == "https://api.theporndb.net/scenes/test-scene"
    assert any("指定详情页 URL" in message for message in res.debug_info.logs)
    assert any("详情映射完成" in message for message in res.debug_info.logs)


@pytest.mark.asyncio
async def test_theporndb_crawler_falls_back_to_movies():
    old_token = manager.config.theporndb_api_token
    manager.config.theporndb_api_token = "token"
    try:
        crawler = TheporndbCrawler(client=FakeTheporndbClient())
        res = await crawler.run(_input("https://theporndb.net/movies/test-movie"))
    finally:
        manager.config.theporndb_api_token = old_token

    assert res.debug_info.error is None
    assert res.data is not None
    assert res.data.title == "movies title"
    assert res.data.external_id == "https://api.theporndb.net/movies/test-movie"


def test_read_data_keeps_later_performers_when_gender_metadata_is_missing():
    data = _api_data(
        "incomplete-performer-data",
        "scenes",
        performers=[
            {"name": "Actor With Missing Gender"},
            {"name": "Female Actor", "parent": {"extras": {"gender": "Female"}}},
            {"name": "Male Actor", "parent": {"extras": {"gender": "Male"}}},
        ],
    )

    result = read_data(data, "scenes")

    assert result.all_actors == ["Actor With Missing Gender", "Female Actor", "Male Actor"]
    assert result.actors == ["Female Actor"]


@pytest.mark.asyncio
async def test_theporndb_crawler_continues_search_when_hash_misses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(oshash, "oshash", lambda file_path: "fakehash")
    monkeypatch.setattr("mdcx.crawlers.theporndb.get_media_duration_seconds", lambda file_path: 7200)
    old_token = manager.config.theporndb_api_token
    old_switch_on = list(manager.config.switch_on)
    manager.config.theporndb_api_token = "token"
    manager.config.switch_on = [switch for switch in manager.config.switch_on if switch != Switch.THEPORNDB_NO_HASH]
    try:
        crawler = TheporndbCrawler(client=FakeTheporndbSearchClient())
        res = await crawler.run(_file_input())
    finally:
        manager.config.theporndb_api_token = old_token
        manager.config.switch_on = old_switch_on

    assert res.debug_info.error is None
    assert res.data is not None
    assert res.data.title == "Ellie Nova"
    assert any("Hash 匹配未命中" in message for message in res.debug_info.logs)
    assert any("原有逻辑暂定候选（尚未成功）" in message for message in res.debug_info.logs)
    assert any("阶段3 时长筛选（阶段1候选）" in message for message in res.debug_info.logs)
    assert any("最终匹配来源: 原有逻辑候选＋时长最终筛选" in message for message in res.debug_info.logs)
    assert any("ThePornDB 最终成功" in message for message in res.debug_info.logs)
    assert res.debug_info.search_urls[:2] == [
        "https://api.theporndb.net/scenes/hash/fakehash",
        "https://api.theporndb.net/scenes?parse=nurumassage.2026-02-23.ellie%20nova&per_page=100",
    ]
    assert any("2023-02-26" in url for url in res.debug_info.search_urls)


@pytest.mark.asyncio
async def test_theporndb_uses_learned_entity_and_duration_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(oshash, "oshash", lambda file_path: "fakehash")
    monkeypatch.setattr(manager, "data_folder", tmp_path)
    monkeypatch.setattr(manager.config, "theporndb_api_token", "token")
    monkeypatch.setattr(
        manager.config,
        "switch_on",
        [switch for switch in manager.config.switch_on if switch != Switch.THEPORNDB_NO_HASH],
    )
    monkeypatch.setattr("mdcx.crawlers.theporndb.get_media_duration_seconds", lambda file_path: 3368.32)
    from mdcx.crawlers.theporndb_knowledge import remember_entities

    remember_entities(
        sites=["BlackedRaw"],
        performers=["Evelyn Claire"],
        data_folder=tmp_path,
    )
    task_input = _file_input()
    task_input.file_path = "/library/BlackedRaw-Evelyn Claire.mp4"

    client = FakeTheporndbEntityFallbackClient()
    crawler = TheporndbCrawler(client=client)
    result = await crawler.run(task_input)

    assert result.debug_info.error is None
    assert result.data is not None
    assert result.data.title == "Trouble Trouble"
    logs = "\n".join(result.debug_info.logs)
    assert "Hash 匹配未命中" in logs
    assert "阶段1未得到唯一时长候选，进入阶段2" in logs
    assert "字典＋日期兜底候选数: 2" in logs
    assert "阶段3 最终时长筛选" in logs
    assert "最终匹配来源: 原有逻辑/字典日期候选＋时长最终筛选" in logs
    queries = [parse_qs(urlparse(url).query) for url in client.urls]
    assert any(query.get("site") and query.get("q") == ["Evelyn Claire"] for query in queries)


@pytest.mark.asyncio
async def test_deeper_candidate_outside_three_minute_runtime_tolerance_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    monkeypatch.setattr(oshash, "oshash", lambda file_path: "fakehash")
    monkeypatch.setattr(manager, "data_folder", tmp_path)
    monkeypatch.setattr(manager.config, "theporndb_api_token", "token")
    monkeypatch.setattr(
        manager.config,
        "switch_on",
        [switch for switch in manager.config.switch_on if switch != Switch.THEPORNDB_NO_HASH],
    )
    # The API candidate is 262 seconds longer, outside the configured 180-second limit.
    monkeypatch.setattr("mdcx.crawlers.theporndb.get_media_duration_seconds", lambda file_path: 2100)
    task_input = _file_input()
    task_input.number = "Deeper.21.09.09"
    task_input.file_path = "/library/Deeper.21.09.09.mp4"

    result = await TheporndbCrawler(client=FakeTheporndbDeeperClient()).run(task_input)

    assert result.debug_info.error is not None
    assert result.data is None
    logs = "\n".join(result.debug_info.logs)
    assert "dates=2021-09-09 | 2009-09-21" in logs
    assert "runtime_candidate=2362.0s" in logs
    assert "runtime_delta=262.0s" in logs
    assert "runtime_tolerance=±180.0s" in logs
    assert "拒绝-duration" in logs


@pytest.mark.asyncio
async def test_theporndb_logs_hash_hit_as_the_final_match_source(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(oshash, "oshash", lambda file_path: "fakehash")
    old_token = manager.config.theporndb_api_token
    old_switch_on = list(manager.config.switch_on)
    manager.config.theporndb_api_token = "token"
    manager.config.switch_on = [switch for switch in manager.config.switch_on if switch != Switch.THEPORNDB_NO_HASH]
    try:
        result = await TheporndbCrawler(client=FakeTheporndbHashHitClient()).run(_file_input())
    finally:
        manager.config.theporndb_api_token = old_token
        manager.config.switch_on = old_switch_on

    logs = "\n".join(result.debug_info.logs)
    assert result.debug_info.error is None
    assert result.data is not None and result.data.title == "Hash Hit"
    assert "Hash 匹配命中" in logs
    assert "文件名解析" not in logs


@pytest.mark.asyncio
async def test_theporndb_fallback_searches_broadly_for_learned_network(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(oshash, "oshash", lambda file_path: "fakehash")
    monkeypatch.setattr(manager, "data_folder", tmp_path)
    monkeypatch.setattr(manager.config, "theporndb_api_token", "token")
    monkeypatch.setattr(
        manager.config,
        "switch_on",
        [switch for switch in manager.config.switch_on if switch != Switch.THEPORNDB_NO_HASH],
    )
    monkeypatch.setattr("mdcx.crawlers.theporndb.get_media_duration_seconds", lambda file_path: 3368.32)
    from mdcx.crawlers.theporndb_knowledge import remember_entities

    remember_entities(studios=["Network A"], performers=["Evelyn Claire"], data_folder=tmp_path)
    task_input = _file_input()
    task_input.file_path = "/library/Network A-Evelyn Claire.mp4"

    client = FakeTheporndbEntityFallbackClient()
    crawler = TheporndbCrawler(client=client)
    result = await crawler.run(task_input)

    assert result.debug_info.error is None
    assert result.data is not None
    assert result.data.title == "Trouble Trouble"
    queries = [parse_qs(urlparse(url).query) for url in client.urls]
    assert any(query.get("site") and query.get("q") == ["Evelyn Claire"] for query in queries)


def test_theporndb_crawler_is_registered():
    assert get_crawler(Website.THEPORNDB) is TheporndbCrawler
