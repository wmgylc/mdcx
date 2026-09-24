from pathlib import Path

import pytest

from mdcx.config.enums import FixedScrapingType, Website
from mdcx.core.file_crawler import FileScraper, classify_scrape_task
from mdcx.crawlers.theporndb_knowledge import KnownEntities
from mdcx.models.log_buffer import LogBuffer
from mdcx.models.types import CrawlerDebugInfo, CrawlerInput, CrawlerResponse, CrawlerResult, CrawlTask


class _RouteConfig:
    fixed_scraping_type = FixedScrapingType.AUTO
    website_youma = {Website.DMM}
    website_wuma = {Website.JAVBUS}
    website_suren = {Website.MGSTAGE}
    website_fc2 = {Website.FC2}
    website_oumei = {Website.THEPORNDB}
    website_guochan = {Website.MDTV}


class _AuditConfig:
    show_web_log = False


class _FakeCrawler:
    def __init__(self, response: CrawlerResponse):
        self.response = response

    async def run(self, task_input: CrawlerInput) -> CrawlerResponse:
        return self.response


class _FakeCrawlerProvider:
    def __init__(self, response: CrawlerResponse):
        self.response = response

    async def get(self, website: Website) -> _FakeCrawler:
        return _FakeCrawler(self.response)


def test_dictionary_route_audit_explains_western_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "mdcx.core.file_crawler.load_known_entities",
        lambda: KnownEntities(sites=("BlackedRaw",), performers=("Evelyn Claire",)),
    )
    task = CrawlTask.empty()
    task.number = "BlackedRaw-Evelyn Claire"
    task.file_path = Path("/pass/BlackedRaw-Evelyn Claire.mp4")

    classification = classify_scrape_task(task, _RouteConfig())
    emitted_logs: list[str] = []
    monkeypatch.setattr("mdcx.core.file_crawler.signal.show_log_text", emitted_logs.append)
    LogBuffer.log().clear()
    FileScraper._write_route_audit(task, classification)

    audit = LogBuffer.log().get()
    assert len(emitted_logs) == 1
    assert "刮削路由审计" in emitted_logs[0]
    assert classification.scraping_type == FixedScrapingType.OUMEI
    assert "刮削路由审计" in audit
    assert "自动规则未先识别欧美" in audit
    assert "site=BlackedRaw" in audit
    assert "performer=Evelyn Claire" in audit
    assert "target=theporndb" in audit
    LogBuffer.clear_task()


@pytest.mark.asyncio
@pytest.mark.parametrize("successful", [True, False])
async def test_theporndb_audit_is_persisted_for_success_and_failure_and_redacts_secrets(
    successful: bool, monkeypatch: pytest.MonkeyPatch
):
    data = None
    error = None
    if successful:
        data = CrawlerResult.empty()
        data.title = "Trouble Trouble"
        data.external_id = "https://api.theporndb.net/scenes/trouble-trouble"
        data.source = Website.THEPORNDB.value
    else:
        error = RuntimeError("no unique match")

    response = CrawlerResponse(
        data=data,
        debug_info=CrawlerDebugInfo(
            execution_time=1.25,
            error=error,
            logs=[
                "Hash 查询未命中，继续文件名搜索",
                "请求地址: https://api.theporndb.net/scenes?token=secret-value&q=Evelyn",
                "Authorization: Bearer top-secret",
            ],
        ),
    )
    task_input = CrawlerInput.empty()
    task_input.file_path = Path("/pass/BlackedRaw-Evelyn Claire.mp4")
    task_input.number = "BlackedRaw-Evelyn Claire"
    emitted_logs: list[str] = []
    monkeypatch.setattr("mdcx.core.file_crawler.signal.show_log_text", emitted_logs.append)
    LogBuffer.log().clear()

    result = await FileScraper(_AuditConfig(), _FakeCrawlerProvider(response))._call_crawler(
        task_input, Website.THEPORNDB
    )

    audit = LogBuffer.log().get()
    assert len(emitted_logs) == 1
    assert "ThePornDB 匹配审计" in emitted_logs[0]
    assert "secret-value" not in emitted_logs[0]
    assert "top-secret" not in emitted_logs[0]
    assert (result.data is not None) is successful
    assert f"status={'成功' if successful else '失败'}" in audit
    assert "Hash 查询未命中" in audit
    assert "api.theporndb.net/scenes?token=[redacted]&q=Evelyn" in audit
    assert "Bearer [redacted]" in audit
    assert "secret-value" not in audit
    assert "top-secret" not in audit
    LogBuffer.clear_task()


@pytest.mark.asyncio
async def test_javbus_failure_audit_persists_search_steps_and_redacts_secrets(monkeypatch: pytest.MonkeyPatch):
    response = CrawlerResponse(
        debug_info=CrawlerDebugInfo(
            execution_time=0.5,
            error=RuntimeError("search result did not match"),
            search_urls=["https://www.javbus.hair/search/Deeper.21.09.09?token=secret-value"],
            logs=["搜索结果候选数: 0", "搜索候选比较完成: checked=0, matched=0"],
        )
    )
    task_input = CrawlerInput.empty()
    task_input.file_path = Path("/pass/Deeper.21.09.09.mp4")
    task_input.number = "Deeper.21.09.09"
    emitted_logs: list[str] = []
    monkeypatch.setattr("mdcx.core.file_crawler.signal.show_log_text", emitted_logs.append)
    LogBuffer.log().clear()

    await FileScraper(_AuditConfig(), _FakeCrawlerProvider(response))._call_crawler(task_input, Website.JAVBUS)

    assert len(emitted_logs) == 1
    assert "JavBus 匹配审计" in emitted_logs[0]
    assert "搜索结果候选数: 0" in emitted_logs[0]
    assert "https://www.javbus.hair/search/Deeper.21.09.09?token=[redacted]" in emitted_logs[0]
    assert "secret-value" not in emitted_logs[0]
    LogBuffer.clear_task()
