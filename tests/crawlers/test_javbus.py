import pytest
from lxml import etree

from mdcx.crawlers.base.types import Context, CralwerException
from mdcx.crawlers.javbus import get_real_url, getRelease, getValidRelease, getYear
from mdcx.models.types import CrawlerInput


def _build_html(release: str):
    html = f"""
    <html>
      <body>
        <p><span class="header">發行日期:</span> {release}</p>
      </body>
    </html>
    """
    return etree.fromstring(html, etree.HTMLParser())


def test_get_valid_release_and_year():
    assert getValidRelease("2024-1-2") == "2024-01-02"
    assert getYear("2024-1-2") == "2024"


def test_get_release_placeholder_date_returns_invalid():
    html = _build_html("0000-00-00")
    release = getRelease(html)
    assert release == "0000-00-00"
    assert getValidRelease(release) == ""
    assert getYear(release) == ""


@pytest.mark.asyncio
async def test_get_real_url_initializes_optional_search_url_audit():
    class FakeSearchClient:
        async def get_text(self, url, **kwargs):
            assert url == "https://www.javbus.hair/search/Deeper.21.09.09"
            return (
                '<html><a class="movie-box" href="https://www.javbus.hair/DEEPER210909"></a></html>',
                "",
            )

    context = Context(CrawlerInput.empty())
    detail_url = await get_real_url(
        FakeSearchClient(), context, "Deeper.21.09.09", "us", "https://www.javbus.com", {}
    )

    assert detail_url == "https://www.javbus.hair/DEEPER210909"
    assert context.debug_info.search_urls == ["https://www.javbus.hair/search/Deeper.21.09.09"]


@pytest.mark.asyncio
async def test_get_real_url_audits_candidate_rejections():
    class FakeSearchClient:
        async def get_text(self, url, **kwargs):
            return (
                '<html><a class="movie-box" href="https://www.javbus.hair/DEEPER210908"></a>'
                '<a class="movie-box" href="https://www.javbus.hair/OTHER123"></a></html>',
                "",
            )

    context = Context(CrawlerInput.empty())
    with pytest.raises(CralwerException, match="未匹配到番号"):
        await get_real_url(FakeSearchClient(), context, "Deeper.21.09.09", "us", "https://www.javbus.com", {})

    assert any("搜索结果候选数: 2" in message for message in context.debug_info.logs)
    assert any("checked=2, matched=0" in message for message in context.debug_info.logs)
