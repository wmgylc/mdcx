from pathlib import Path

import pytest
from parsel import Selector

import mdcx.crawlers.javdb_new as javdb_module
from mdcx.config.enums import Language
from mdcx.crawlers.base import CrawlerData
from mdcx.crawlers.javdb_new import JavdbCrawler
from mdcx.models.types import CrawlerInput


def make_input(*, file_path=None):
    return CrawlerInput(
        appoint_number="",
        appoint_url="",
        file_path=file_path,
        mosaic="",
        number="ABF-056",
        short_number="ABF-056",
        language=Language.UNDEFINED,
        org_language=Language.UNDEFINED,
    )


@pytest.mark.asyncio
async def test_search_returns_all_exact_number_candidates():
    crawler = JavdbCrawler(client=None, base_url="https://javdb.com")
    ctx = crawler.new_context(make_input())
    html = Selector(
        text='''
        <a class="box" href="/v/new">
          <div class="video-title"><strong>ABF-056</strong> New listing</div>
          <div class="meta">2023-12-21</div>
        </a>
        <a class="box" href="/v/old">
          <div class="video-title"><strong>ABF-056</strong> Old listing</div>
          <div class="meta">2013-08-02</div>
        </a>
        <a class="box" href="/v/other-number">
          <div class="video-title"><strong>ABF-0560</strong> Different number</div>
          <div class="meta">2023-12-21</div>
        </a>
        '''
    )

    result = await crawler._parse_search_page(ctx, html, "https://javdb.com/search?q=ABF-056")

    assert result == ["https://javdb.com/v/new?locale=zh", "https://javdb.com/v/old?locale=zh"]


@pytest.mark.asyncio
async def test_detail_selects_candidate_closest_to_local_duration(monkeypatch: pytest.MonkeyPatch):
    crawler = JavdbCrawler(client=None, base_url="https://javdb.com")
    ctx = crawler.new_context(make_input(file_path=Path("/library/ABF-056.mp4")))
    details = {
        "https://javdb.com/v/new": CrawlerData(title="190-minute candidate", runtime="190"),
        "https://javdb.com/v/old": CrawlerData(title="147-minute candidate", runtime="147"),
    }

    monkeypatch.setattr(javdb_module, "get_video_duration_seconds", lambda _: 11_379.324)

    async def fetch_detail(_ctx, url):
        return "<html></html>", ""

    async def parse_detail(_ctx, _html, url):
        return details[url]

    monkeypatch.setattr(crawler, "_fetch_detail", fetch_detail)
    monkeypatch.setattr(crawler, "_parse_detail_page", parse_detail)

    selected = await crawler._detail(ctx, list(details))

    assert selected is details["https://javdb.com/v/new"]
    assert any("按时长差选择候选" in line for line in ctx.debug_info.logs)


@pytest.mark.asyncio
async def test_detail_keeps_site_order_when_local_duration_is_unavailable(monkeypatch: pytest.MonkeyPatch):
    crawler = JavdbCrawler(client=None, base_url="https://javdb.com")
    ctx = crawler.new_context(make_input(file_path=Path("/library/ABF-056.mp4")))
    details = {
        "https://javdb.com/v/first": CrawlerData(title="first", runtime="190"),
        "https://javdb.com/v/second": CrawlerData(title="second", runtime="147"),
    }
    visited = []

    monkeypatch.setattr(javdb_module, "get_video_duration_seconds", lambda _: None)

    async def fetch_detail(_ctx, url):
        visited.append(url)
        return "<html></html>", ""

    async def parse_detail(_ctx, _html, url):
        return details[url]

    monkeypatch.setattr(crawler, "_fetch_detail", fetch_detail)
    monkeypatch.setattr(crawler, "_parse_detail_page", parse_detail)

    selected = await crawler._detail(ctx, list(details))

    assert selected is details["https://javdb.com/v/first"]
    assert visited == ["https://javdb.com/v/first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["Blackedraw.19.08.30", "Blackedraw-19-08-30", "Studio.2024.08.30"])
async def test_detail_does_not_duration_rank_western_studio_date_number(
    monkeypatch: pytest.MonkeyPatch, number: str
):
    crawler = JavdbCrawler(client=None, base_url="https://javdb.com")
    task_input = make_input(file_path=Path(f"/library/{number}.mp4"))
    task_input.number = number
    ctx = crawler.new_context(task_input)
    details = {
        "https://javdb.com/v/first": CrawlerData(title="first", runtime="190"),
        "https://javdb.com/v/second": CrawlerData(title="second", runtime="147"),
    }
    visited = []

    def unexpected_duration_read(_):
        pytest.fail("Western studio/date identifiers must not trigger duration ranking")

    monkeypatch.setattr(javdb_module, "get_video_duration_seconds", unexpected_duration_read)

    async def fetch_detail(_ctx, url):
        visited.append(url)
        return "<html></html>", ""

    async def parse_detail(_ctx, _html, url):
        return details[url]

    monkeypatch.setattr(crawler, "_fetch_detail", fetch_detail)
    monkeypatch.setattr(crawler, "_parse_detail_page", parse_detail)

    selected = await crawler._detail(ctx, list(details))

    assert selected is details["https://javdb.com/v/first"]
    assert visited == ["https://javdb.com/v/first"]
