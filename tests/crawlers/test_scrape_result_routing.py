from mdcx.config.enums import FixedScrapingType
from mdcx.core.file_crawler import ScrapeClassification, resolve_scrape_result_classification
from mdcx.models.types import CrawlersResult


def test_successful_fallback_channel_controls_output_subfolder():
    initial = ScrapeClassification(
        FixedScrapingType.YOUMA,
        "auto",
        try_parallel_channel_fallback=True,
    )
    result = CrawlersResult.empty()
    result.scraping_type = FixedScrapingType.OUMEI
    result.scraping_type_source = "auto"

    resolved = resolve_scrape_result_classification(initial, result)

    assert resolved.scraping_type == FixedScrapingType.OUMEI
    assert resolved.output_subfolder == "us"
    assert resolved.try_parallel_channel_fallback is False
