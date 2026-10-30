import asyncio
import re
from copy import copy
from dataclasses import dataclass, replace
from datetime import date
from itertools import chain
from typing import TYPE_CHECKING

from ..config.enums import FixedScrapingType
from ..config.models import CONFIGURABLE_SCRAPING_TYPES, Language, Website
from ..crawlers.theporndb_knowledge import find_known_entities, find_known_entities_in_name, load_known_entities
from ..gen.field_enums import CrawlerResultFields
from ..manual import ManualConfig
from ..models.enums import FileMode
from ..models.flags import Flags
from ..models.log_buffer import LogBuffer
from ..models.types import CrawlerInput, CrawlerResponse, CrawlerResult, CrawlersResult, CrawlTask
from ..number import is_japanese_catalog_number, is_uncensored
from ..signals import signal
from ..utils.dataclass import update
from ..utils.video import get_video_duration_seconds, parse_duration_seconds
from .mosaic import is_guochan_mosaic, is_plain_uncensored_mosaic, normalize_mosaic

if TYPE_CHECKING:
    from ..config.models import Config
    from ..crawler import CrawlerProviderProtocol


MULTI_LANGUAGE_WEBSITES = [  # 支持多语言, language 参数有意义
    Website.AIRAV_CC,
    Website.IQQTV,
    Website.JAVLIBRARY,
]
SPECIFIC_CRAWLER_TITLE_LANGUAGE_SITES = {
    Website.AIRAV_CC,
    Website.IQQTV,
    Website.AVSEX,
    Website.JAVLIBRARY,
    Website.MDTV,
    Website.MADOUQU,
    Website.LULUBAR,
}


def sprint_source(website: Website, language: Language) -> str:
    if language == Language.UNDEFINED:
        return f"{website.value}"
    return f"{website.value} ({language.value})"


def _normalize_release_value(value: object) -> str:
    release = str(value).strip()
    if not release:
        return ""
    release = release.replace("/", "-").replace(".", "-")
    if not (match := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", release)):
        return ""
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _is_suren_number(file_number: str, short_number: str) -> bool:
    return bool(short_number) or "SIRO" in file_number.upper()


def _has_oumei_date_pattern(value: str) -> bool:
    """Match the legacy Western release-name shape, e.g. Studio.24.01.31."""
    return bool(re.search(r"[^.]+\.\d{2}\.\d{2}\.\d{2}", value))


@dataclass(frozen=True)
class ScrapeClassification:
    scraping_type: FixedScrapingType
    scraping_type_source: str
    sites: list[Website] | set[Website] | None = None
    website: Website | None = None
    mosaic: str = ""
    reason: str = ""
    try_parallel_channel_fallback: bool = False

    @property
    def output_subfolder(self) -> str | None:
        """Separate Japanese and Western results while leaving other types unchanged."""
        if self.scraping_type == FixedScrapingType.OUMEI:
            return "us"
        if self.scraping_type in {
            FixedScrapingType.AUTO,
            FixedScrapingType.YOUMA,
            FixedScrapingType.WUMA,
            FixedScrapingType.SUREN,
            FixedScrapingType.FC2,
        }:
            return "jp"
        return None


def classify_scrape_task(task_input: CrawlTask, config: "Config", use_fixed_type: bool = True) -> ScrapeClassification:
    file_number = task_input.number
    file_path = task_input.file_path
    file_path_str = str(file_path).lower() if file_path else ""
    mosaic = task_input.mosaic

    fixed_type = config.fixed_scraping_type
    fixed_sites = {
        FixedScrapingType.YOUMA: config.website_youma,
        FixedScrapingType.WUMA: config.website_wuma,
        FixedScrapingType.SUREN: config.website_suren,
        FixedScrapingType.FC2: config.website_fc2,
        FixedScrapingType.OUMEI: config.website_oumei,
        FixedScrapingType.GUOCHAN: config.website_guochan,
    }
    if use_fixed_type and fixed_type != FixedScrapingType.AUTO:
        return ScrapeClassification(
            fixed_type, "fixed", sites=fixed_sites[fixed_type], reason="用户固定的刮削类型优先于自动识别"
        )

    if (
        is_guochan_mosaic(mosaic)
        or (re.search(r"([^A-Z]|^)MD[A-Z-]*\d{4,}", file_number) and "MDVR" not in file_number)
        or re.search(r"MKY-[A-Z]+-\d{3,}", file_number)
    ):
        return ScrapeClassification(
            FixedScrapingType.GUOCHAN,
            "auto",
            sites=config.website_guochan,
            mosaic="国产",
            reason="命中国产片番号/马赛克规则",
        )

    if file_number.startswith("KIN8"):
        return ScrapeClassification(FixedScrapingType.YOUMA, "auto", website=Website.KIN8, reason="番号前缀 KIN8")

    if file_number.startswith("DLID"):
        return ScrapeClassification(FixedScrapingType.AUTO, "auto", website=Website.GETCHU, reason="番号前缀 DLID")

    if "getchu" in file_path_str or "里番" in file_path_str or "裏番" in file_path_str:
        return ScrapeClassification(
            FixedScrapingType.AUTO, "auto", website=Website.GETCHU_DMM, reason="路径命中 Getchu/里番关键词"
        )

    if "mywife" in file_path_str:
        return ScrapeClassification(FixedScrapingType.YOUMA, "auto", website=Website.MYWIFE, reason="路径命中 mywife")

    if "FC2" in file_number.upper():
        file_number_1 = re.search(r"\d{5,}", file_number)
        if file_number_1:
            return ScrapeClassification(
                FixedScrapingType.FC2, "auto", sites=config.website_fc2, reason="FC2 番号包含 5 位以上数字"
            )
        if not use_fixed_type:
            return ScrapeClassification(FixedScrapingType.AUTO, "auto", reason="指定站点模式下的 FC2 未识别")
        raise Exception(f"未识别的 FC2 番号: {file_number}")

    if _has_oumei_date_pattern(file_number) or (
        "欧美" in file_path_str and "东欧美" not in file_path_str
    ):
        return ScrapeClassification(
            FixedScrapingType.OUMEI,
            "auto",
            sites=config.website_oumei,
            reason="命中欧美日期编号格式或欧美路径关键词",
        )

    # Check the video basename first; parent folders are only considered at the final type default.
    western_dictionary_note = "未检查欧美实体词典（缺少文件路径）"
    known_entities = load_known_entities() if file_path else None
    if file_path and known_entities is not None:
        matched_entities = find_known_entities(file_path, known_entities)
        if matched_entities.sites or matched_entities.studios or matched_entities.performers:
            matched_summary = (
                f"site={','.join(matched_entities.sites) or '-'}; "
                f"studio={','.join(matched_entities.studios) or '-'}; "
                f"performer={','.join(matched_entities.performers) or '-'}"
            )
            return ScrapeClassification(
                FixedScrapingType.OUMEI,
                "auto",
                sites=config.website_oumei,
                reason=f"自动规则未先识别欧美；文件名命中欧美实体词典（{matched_summary}）",
            )
        western_dictionary_note = "影片文件名未命中欧美实体词典"

    if is_plain_uncensored_mosaic(mosaic):
        return ScrapeClassification(
            FixedScrapingType.WUMA,
            "auto",
            sites=config.website_wuma,
            reason=f"命中无码马赛克规则；{western_dictionary_note}",
        )

    if _is_suren_number(file_number, task_input.short_number):
        return ScrapeClassification(
            FixedScrapingType.SUREN,
            "auto",
            sites=config.website_suren,
            reason=f"命中素人番号/短番号规则；{western_dictionary_note}",
        )

    if is_uncensored(file_number):
        return ScrapeClassification(
            FixedScrapingType.WUMA,
            "auto",
            sites=config.website_wuma,
            reason=f"番号命中无码规则；{western_dictionary_note}",
        )

    if re.match(r"\D{2,}00\d{3,}", file_number) and "-" not in file_number and "_" not in file_number:
        return ScrapeClassification(
            FixedScrapingType.YOUMA,
            "auto",
            sites={Website.DMM},
            reason=f"命中有码 DMM 番号格式；{western_dictionary_note}",
        )

    # Parent-folder entities are only a routing hint after all file-level rules fail.
    parent_folder_note = "未检查欧美父文件夹词典（缺少文件路径）"
    if file_path:
        parent_folder = file_path.parent.name
        if _has_oumei_date_pattern(parent_folder):
            return ScrapeClassification(
                FixedScrapingType.OUMEI,
                "auto",
                sites=config.website_oumei,
                reason=(
                    "文件名未命中类型规则；直接父文件夹命中原欧美日期格式"
                    f"（{parent_folder}）"
                ),
            )

        if known_entities is not None:
            matched_parent_entities = find_known_entities_in_name(parent_folder, known_entities)
            if matched_parent_entities.sites or matched_parent_entities.studios or matched_parent_entities.performers:
                matched_summary = (
                    f"site={','.join(matched_parent_entities.sites) or '-'}; "
                    f"studio={','.join(matched_parent_entities.studios) or '-'}; "
                    f"performer={','.join(matched_parent_entities.performers) or '-'}"
                )
                return ScrapeClassification(
                    FixedScrapingType.OUMEI,
                    "auto",
                    sites=config.website_oumei,
                    reason=(
                        "文件名规则未能分类；直接父文件夹命中欧美实体词典"
                        f"（{matched_summary}）"
                    ),
                )
            parent_folder_note = "直接父文件夹未命中欧美实体词典"
        else:
            parent_folder_note = "欧美父文件夹实体词典不可用"

    return ScrapeClassification(
        FixedScrapingType.YOUMA,
        "auto",
        sites=config.website_youma,
        reason=(
            f"所有显式类型规则及欧美实体词典均未命中，采用原日本有码默认兜底；"
            f"{western_dictionary_note}；{parent_folder_note}"
        ),
        try_parallel_channel_fallback=True,
    )


def apply_scrape_classification(result: CrawlersResult, classification: ScrapeClassification) -> None:
    result.scraping_type = classification.scraping_type
    result.scraping_type_source = classification.scraping_type_source


def resolve_scrape_result_classification(
    classification: ScrapeClassification, result: CrawlersResult
) -> ScrapeClassification:
    """Use the successful fallback channel's type for output-side processing."""
    if not result.scraping_type_source or result.scraping_type == classification.scraping_type:
        return classification
    return replace(
        classification,
        scraping_type=result.scraping_type,
        scraping_type_source=result.scraping_type_source,
        reason=f"匹配结果由 {result.scraping_type.value} 类型渠道确认",
        try_parallel_channel_fallback=False,
    )


def classify_existing_scrape_result(
    task_input: CrawlTask, result: CrawlersResult, config: "Config", use_fixed_type: bool = True
) -> ScrapeClassification:
    # 读取已有 NFO 时保留文件上下文，只用 NFO 中更可信的番号和马赛克覆盖分类输入。
    classification_input = update(
        task_input,
        {
            "number": result.number or task_input.number,
            "mosaic": result.mosaic or task_input.mosaic,
        },
    )
    classification = classify_scrape_task(classification_input, config, use_fixed_type=use_fixed_type)
    apply_scrape_classification(result, classification)
    return classification


def _deal_res(res: CrawlersResult) -> CrawlersResult:
    res.mosaic = normalize_mosaic(res.mosaic)

    # 标签
    tag = re.sub(r",\d+[kKpP],", ",", res.tag)
    tag_rep_word = [",HD高画质", ",HD高畫質", ",高画质", ",高畫質"]
    for each in tag_rep_word:
        if tag.endswith(each):
            tag = tag.replace(each, "")
        tag = tag.replace(each + ",", ",")
    res.tag = tag

    # 发行日期
    res.release = _normalize_release_value(res.release)

    # 评分
    if res.score:
        res.score = f"{float(res.score):.1f}"

    # publisher
    if not res.publisher:
        res.publisher = res.studio

    # 字符转义，避免显示问题
    key_word = [
        "title",
        "originaltitle",
        "number",
        "outline",
        "originalplot",
        "series",
        "studio",
        "publisher",
    ]
    rep_word = {
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&apos;": "'",
        "&quot;": '"',
        "&lsquo;": "「",
        "&rsquo;": "」",
        "&hellip;": "…",
        "<br/>": "",
        "・": "·",
        """: "「",
        """: "」",
        "...": "…",
        "\xa0": "",
        "\u3000": "",
        "\u2800": "",
    }
    for each in key_word:
        for key, value in rep_word.items():
            # res[each] = res[each].replace(key, value)
            setattr(res, each, getattr(res, each).replace(key, value))

    return res


class FileScraper:
    def __init__(self, config: "Config", crawler_provider: "CrawlerProviderProtocol"):
        self.config = config
        self.crawler_provider = crawler_provider

    @staticmethod
    def _is_invalid_runtime(value: object) -> bool:
        runtime = str(value).strip()
        if not runtime:
            return False
        if re.fullmatch(r"0+(?:\.0+)?", runtime):
            return True
        return False

    @staticmethod
    def _normalized_number_key(value: object) -> str:
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    @staticmethod
    def _runtime_to_seconds(value: object) -> float | None:
        return parse_duration_seconds(value, bare_unit="minutes")

    @staticmethod
    def _has_match_title(result: CrawlersResult | None) -> bool:
        """A channel without a usable title has not identified a scrape result."""
        return bool(result and str(result.title or "").strip())

    async def _preload_japanese_duration_candidates(
        self,
        task_input: CrawlerInput,
        classification: ScrapeClassification,
        type_site_set: set[Website],
        all_res: dict[tuple[Website, Language], CrawlerResult],
        failed: set[tuple[Website, Language]],
        req_info: list[str],
    ) -> list[Website]:
        """Load configured Japanese sources and rank same-number results by local runtime."""
        if classification.scraping_type not in {
            FixedScrapingType.YOUMA,
            FixedScrapingType.WUMA,
            FixedScrapingType.SUREN,
            FixedScrapingType.FC2,
        }:
            return []
        if not task_input.file_path or not is_japanese_catalog_number(task_input.number):
            return []

        local_duration = await asyncio.to_thread(get_video_duration_seconds, task_input.file_path)
        if local_duration is None:
            return []

        runtime_field = CrawlerResultFields.RUNTIME
        if hasattr(self.config, "get_type_field_config"):
            runtime_priority = self.config.get_type_field_config(classification.scraping_type, runtime_field)
        else:
            runtime_priority = self.config.get_field_config(runtime_field)
        runtime_sites = [site for site in runtime_priority.site_prority if site in type_site_set]
        if len(runtime_sites) < 2:
            return []

        runtime_language = self.config.get_field_config(runtime_field).language
        preload_errors: list[str] = []
        for site in runtime_sites:
            language = runtime_language if site in MULTI_LANGUAGE_WEBSITES else Language.UNDEFINED
            key = (site, language)
            if key in all_res or key in failed:
                continue

            site_input = copy(task_input)
            site_input.language = runtime_language
            site_input.org_language = runtime_language
            if site in MULTI_LANGUAGE_WEBSITES and language == Language.UNDEFINED:
                site_input.language = Language.JP
                site_input.org_language = Language.JP

            try:
                response = await self._call_crawler(site_input, site)
                req_info.append(f"{sprint_source(*key)} ({response.debug_info.execution_time:.2f}s)")
                if response.data is None:
                    error_type = type(response.debug_info.error).__name__ if response.debug_info.error else "EmptyResult"
                    failed.add(key)
                    preload_errors.append(f"{site.value}: {error_type}")
                    continue
                all_res[key] = response.data
                if site in MULTI_LANGUAGE_WEBSITES and (site, Language.UNDEFINED) not in all_res:
                    all_res[(site, Language.UNDEFINED)] = response.data
            except TimeoutError:
                failed.add(key)
                preload_errors.append(f"{site.value}: 请求超时")
            except Exception as error:
                failed.add(key)
                preload_errors.append(f"{site.value}: {type(error).__name__}")

        expected_numbers = {
            key
            for value in (task_input.number, task_input.short_number)
            if (key := self._normalized_number_key(value))
        }
        candidates: list[tuple[float, int, Website, str]] = []
        for order, site in enumerate(runtime_sites):
            data = self._get_cached_site_result(all_res, site, runtime_language)
            if data is None or self._normalized_number_key(data.number) not in expected_numbers:
                continue
            candidate_duration = self._runtime_to_seconds(data.runtime)
            if candidate_duration is None:
                continue
            delta = abs(local_duration - candidate_duration)
            candidates.append((delta, order, site, str(data.runtime)))

        if len(candidates) < 2:
            if preload_errors:
                LogBuffer.log().write("\n  日本番号时长候选预取失败: " + " | ".join(preload_errors))
            return []

        candidates.sort(key=lambda item: (item[0], item[1]))
        ranked_sites = [site for _, _, site, _ in candidates]
        audit = [
            f"{site.value}: runtime={runtime}, delta={delta / 60:.1f}min"
            for delta, _, site, runtime in candidates
        ]
        LogBuffer.log().write(
            f"\n  ⏱ [日本番号时长消歧] local={local_duration / 60:.1f}min; "
            f"按近到远排序: {' | '.join(audit)}"
        )
        if preload_errors:
            LogBuffer.log().write("\n  日本番号其他来源未返回: " + " | ".join(preload_errors))
        return ranked_sites

    @staticmethod
    def _normalize_release(value: object) -> str:
        return _normalize_release_value(value)

    @staticmethod
    def _normalize_year(value: object) -> str:
        year = str(value).strip()
        if not year:
            return ""
        if not (match := re.search(r"\d{4}", year)):
            return ""
        year = match.group()
        return "" if year == "0000" else year

    @staticmethod
    def _get_cached_site_result(
        all_res: dict[tuple[Website, Language], CrawlerResult],
        site: Website,
        language: Language,
    ) -> CrawlerResult | None:
        if site not in MULTI_LANGUAGE_WEBSITES:
            language = Language.UNDEFINED
        if data := all_res.get((site, language)):
            return data
        return all_res.get((site, Language.UNDEFINED))

    @staticmethod
    def _redact_audit_text(value: object) -> str:
        text = " ".join(str(value).splitlines()).strip()
        text = re.sub(
            r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;\"']+",
            r"\1[redacted]",
            text,
        )
        text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [redacted]", text)
        text = re.sub(
            r"(?i)([?&](?:api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|token|key|signature|policy|x-amz-signature|x-amz-credential)=)[^&\s]+",
            r"\1[redacted]",
            text,
        )
        text = re.sub(
            r"(?i)([\"']?(?:theporndb_api_token|api[_-]?token|access[_-]?token)[\"']?\s*[:=]\s*[\"']?)[^\s,;}\"']+",
            r"\1[redacted]",
            text,
        )
        return text[:1200] + ("…" if len(text) > 1200 else "")

    @classmethod
    def _write_theporndb_audit(
        cls, task_input: CrawlerInput, response: CrawlerResponse, detail_log_enabled: bool = False
    ) -> None:
        """Persist ordered ThePornDB diagnostics for both successful and failed lookups."""
        info = response.debug_info
        status = "成功" if response.data is not None else "失败"
        file_path = task_input.file_path or "-"
        lines = [
            f"\n 🧾 [ThePornDB 匹配审计] file={file_path} number={task_input.number} "
            f"status={status} elapsed={info.execution_time:.2f}s steps={len(info.logs)}"
        ]
        if info.error:
            lines.append(f"    最终错误: {type(info.error).__name__}: {cls._redact_audit_text(info.error)}")
        if response.data is not None:
            data = response.data
            lines.append(
                "    最终数据: "
                f"title={cls._redact_audit_text(data.title or '-')}; "
                f"external_id={cls._redact_audit_text(data.external_id or '-')}; "
                f"source={cls._redact_audit_text(data.source or '-')}"
            )
        lines.extend(
            f"    [{index:03d}] {cls._redact_audit_text(message)}"
            for index, message in enumerate(info.logs, start=1)
        )
        if info.search_urls:
            lines.append("    搜索请求: " + " | ".join(cls._redact_audit_text(url) for url in info.search_urls))
        if info.detail_urls:
            lines.append("    详情请求: " + " | ".join(cls._redact_audit_text(url) for url in info.detail_urls))
        audit = "\n".join(lines)
        LogBuffer.log().write(audit)
        if not detail_log_enabled:
            signal.show_log_text(audit)

    @classmethod
    def _write_javbus_failure_audit(
        cls, task_input: CrawlerInput, response: CrawlerResponse, detail_log_enabled: bool = False
    ) -> None:
        """Persist JavBus search diagnostics when the crawler does not find a record."""
        info = response.debug_info
        lines = [
            f"\n 🧾 [JavBus 匹配审计] file={task_input.file_path or '-'} "
            f"number={task_input.number} status=失败 elapsed={info.execution_time:.2f}s steps={len(info.logs)}"
        ]
        if info.error:
            lines.append(f"    最终错误: {type(info.error).__name__}: {cls._redact_audit_text(info.error)}")
        lines.extend(
            f"    [{index:03d}] {cls._redact_audit_text(message)}"
            for index, message in enumerate(info.logs, start=1)
        )
        if info.search_urls:
            lines.append("    搜索请求: " + " | ".join(cls._redact_audit_text(url) for url in info.search_urls))
        if info.detail_urls:
            lines.append("    详情请求: " + " | ".join(cls._redact_audit_text(url) for url in info.detail_urls))
        audit = "\n".join(lines)
        LogBuffer.log().write(audit)
        if not detail_log_enabled:
            signal.show_log_text(audit)

    @staticmethod
    def _write_route_audit(
        task_input: CrawlTask,
        classification: ScrapeClassification,
        forced_website: Website | None = None,
        detail_log_enabled: bool = False,
    ) -> None:
        if forced_website:
            target = forced_website.value
            reason = f"单站调用指定站点；自动分类参考：{classification.reason or '-'}"
        elif classification.website:
            target = classification.website.value
            reason = classification.reason or "专用爬虫分类"
        else:
            target = ",".join(sorted(site.value for site in (classification.sites or []))) or "-"
            reason = classification.reason or "未提供分类原因"
            if classification.try_parallel_channel_fallback:
                channel_types = ",".join(scraping_type.value for scraping_type in CONFIGURABLE_SCRAPING_TYPES)
                target = f"全部已配置类型渠道[{channel_types}]（并行兜底）"
                reason += "；并行尝试所有有站点配置的刮削类型，按类型注册顺序选择首个有效结果"
        audit = (
            "\n 🔎 [刮削路由审计] "
            f"file={task_input.file_path or '-'} number={task_input.number} "
            f"mosaic={task_input.mosaic or '-'} type={classification.scraping_type.value} "
            f"source={classification.scraping_type_source} target={target} reason={reason}"
        )
        LogBuffer.log().write(audit)
        if not detail_log_enabled:
            signal.show_log_text(audit)

    async def _call_crawler(
        self, task_input: CrawlerInput, website: Website, timeout: float | None = 30
    ) -> CrawlerResponse:
        """
        调用指定网站的爬虫函数

        Args:
            task_input (CrawlerInput): 包含爬虫所需的输入数据
            website (str): 网站名称
            timeout (float | None): 请求超时时间，默认为30秒

        Raises:
            asyncio.TimeoutError: 如果请求超时
            Exception: 爬虫函数抛出的异常
        """
        short_number = task_input.short_number
        original_number = task_input.number

        # 259LUXU-1111， mgstage 和 avsex 之外使用 LUXU-1111（素人番号时，short_number有值，不带前缀数字；反之，short_number为空)
        if short_number and website != "mgstage" and website != "avsex":
            task_input.number = short_number

        try:
            c = await self.crawler_provider.get(website)

            # 移除外层超时限制，让内层的 GatherGroup 处理超时和重试
            # 原有的超时机制已由各个 HTTP 请求单独处理
            r = await c.run(task_input)
            if website == Website.THEPORNDB:
                self._write_theporndb_audit(
                    task_input, r, detail_log_enabled=getattr(self.config, "show_web_log", False)
                )
            elif website == Website.JAVBUS and r.data is None:
                self._write_javbus_failure_audit(
                    task_input, r, detail_log_enabled=getattr(self.config, "show_web_log", False)
                )
            return r
        except Exception as e:
            if website in (Website.THEPORNDB, Website.JAVBUS):
                audit_title = "ThePornDB" if website == Website.THEPORNDB else "JavBus"
                audit = (
                    f"\n 🧾 [{audit_title} 匹配审计] "
                    f"file={task_input.file_path or '-'} number={task_input.number} "
                    f"crawler-call-exception={type(e).__name__}: {self._redact_audit_text(e)}"
                )
                LogBuffer.log().write(audit)
                if not getattr(self.config, "show_web_log", False):
                    signal.show_log_text(audit)
            raise
        finally:
            task_input.number = original_number

    async def _call_crawlers(
        self, task_input: CrawlerInput, classification: ScrapeClassification | list[Website] | set[Website]
    ) -> CrawlersResult | None:
        """
        获取一组网站的数据：按照设置的网站组，请求各字段数据，并返回最终的数据
        采用按需请求策略：仅请求必要的网站，失败时才请求下一优先级网站
        """
        use_type_field_config = isinstance(classification, ScrapeClassification)
        if not isinstance(classification, ScrapeClassification):
            classification = ScrapeClassification(FixedScrapingType.AUTO, "compat", sites=classification)
        type_sites = list(classification.sites or [])
        type_site_set = set(type_sites)
        all_res: dict[tuple[Website, Language], CrawlerResult] = {}
        failed: set[tuple[Website, Language]] = set()  # 记录失败的网站
        reduced = CrawlersResult.empty()
        req_info: list[str] = []  # 请求信息列表
        try_all_images = bool(
            getattr(self.config, "scrape_like", "") == "info"
            and getattr(self.config, "field_priority_try_all_images", False)
        )
        image_fields = {CrawlerResultFields.THUMB, CrawlerResultFields.POSTER}
        duration_ranked_sites = await self._preload_japanese_duration_candidates(
            task_input, classification, type_site_set, all_res, failed, req_info
        )
        duration_site_rank = {site: index for index, site in enumerate(duration_ranked_sites)}
        if duration_ranked_sites:
            reduced.field_log += (
                "\n\n    ⏱ 日本番号时长候选优先: "
                + " -> ".join(site.value for site in duration_ranked_sites)
            )

        # 按字段分别处理，每个字段按优先级尝试获取
        for field in ManualConfig.REDUCED_FIELDS:
            # 获取该字段的优先级列表
            f_config = self.config.get_field_config(field)
            if use_type_field_config and hasattr(self.config, "get_type_field_config"):
                type_field_config = self.config.get_type_field_config(classification.scraping_type, field)
            else:
                type_field_config = f_config
            f_sites = [s for s in type_field_config.site_prority if s in type_site_set]
            if duration_ranked_sites:
                f_sites = sorted(f_sites, key=lambda site: duration_site_rank.get(site, len(duration_site_rank)))
            f_lang = f_config.language

            reduced.field_log += (
                f"\n\n    📌 {field} \n    ====================================\n"
                f"    🌐 优先级设置: {' -> '.join(s.value for s in f_sites)}"
            )

            # 按优先级依次尝试获取字段值
            for site in f_sites:
                # 检查是否已经请求过该网站
                key = (site, f_lang)

                # 如果网站不支持多语言, 则使用 UNDEFINED
                if site not in MULTI_LANGUAGE_WEBSITES:
                    key = (site, Language.UNDEFINED)

                # 如果已有该网站数据，直接使用
                if key in all_res:
                    site_data = all_res[key]
                elif key in failed:
                    # 不再请求已失败的网站
                    reduced.field_log += f"\n    🔴 {site:<15} (已失败, 跳过)"
                    continue
                else:
                    # 如果网站数据尚未请求，则进行请求
                    try:
                        task_input.language = f_lang
                        task_input.org_language = f_lang
                        # 多语言网站, 指定一个默认语言
                        if site in MULTI_LANGUAGE_WEBSITES and key[1] == Language.UNDEFINED:
                            task_input.language = Language.JP
                            task_input.org_language = Language.JP
                        web_data = await self._call_crawler(task_input, site)
                        req_info.append(f"{sprint_source(*key)} ({web_data.debug_info.execution_time:.2f}s)")
                        if web_data.data is None:
                            if e := web_data.debug_info.error:
                                raise e
                            raise ValueError(f"{site} 返回了空数据")
                        site_data = web_data.data
                        # 处理并保存结果
                        all_res[key] = web_data.data
                        # 多语言网站, 如果 undefined 尚不存在, 也使用当前语言数据
                        if site in MULTI_LANGUAGE_WEBSITES and (site, Language.UNDEFINED) not in all_res:
                            all_res[(site, Language.UNDEFINED)] = web_data.data
                    except TimeoutError:
                        reduced.field_log += f"\n    🔴 {site:<15} (请求超时)"
                        failed.add(key)
                        continue
                    except Exception as e:
                        reduced.field_log += f"\n    🔴 {site:<15} (失败: {str(e)})"
                        failed.add(key)
                        continue

                # 检查字段数据
                field_value = getattr(site_data, field.value, None)
                if not field_value:
                    reduced.field_log += f"\n    🔴 {site:<15} (未找到)"
                    continue
                if field == CrawlerResultFields.RUNTIME and self._is_invalid_runtime(field_value):
                    reduced.field_log += f"\n    🟡 {site:<15} (runtime=0, 舍弃)"
                    continue
                if field == CrawlerResultFields.RELEASE:
                    normalized_release = self._normalize_release(field_value)
                    if not normalized_release:
                        reduced.field_log += f"\n    🟡 {site:<15} (release无效, 舍弃: {field_value})"
                        continue
                    field_value = normalized_release
                if field == CrawlerResultFields.YEAR:
                    normalized_year = self._normalize_year(field_value)
                    if not normalized_year:
                        reduced.field_log += f"\n    🟡 {site:<15} (year无效, 舍弃: {field_value})"
                        continue
                    field_value = normalized_year

                is_primary_field_value = not getattr(reduced, field.value, None)

                # 添加来源信息
                if is_primary_field_value:
                    reduced.field_sources[field] = site.value

                # 添加 external_id
                reduced.external_ids[site] = site_data.external_id

                if field == CrawlerResultFields.POSTER and is_primary_field_value:
                    reduced.image_download = site_data.image_download
                elif field == CrawlerResultFields.ORIGINALTITLE and site_data.actor:
                    reduced.amazon_orginaltitle_actor = site_data.actor.split(",")[0]

                # 保存数据
                if is_primary_field_value:
                    setattr(reduced, field.value, field_value)
                    reduced.field_log += f"\n    🟢 {site}\n     ↳{getattr(reduced, field.value)}"
                elif try_all_images and field in image_fields:
                    reduced.field_log += f"\n    🟢 {site} (候选)\n     ↳{field_value}"
                # 找到有效数据，跳出循环继续处理下一个字段
                if not (try_all_images and field in image_fields):
                    break
            else:  # 所有来源都无此字段
                reduced.field_log += "\n    🔴 所有来源均无数据"

        # 所有来源均失败
        if len(all_res) == 0:
            failure_audit = "所有配置来源均未返回影片数据。"
            if reduced.field_log:
                failure_audit += "\n" + reduced.field_log
            LogBuffer.error().write(failure_audit)
            return None

        if use_type_field_config and hasattr(self.config, "get_type_field_config"):
            poster_priority_config = self.config.get_type_field_config(
                classification.scraping_type, CrawlerResultFields.POSTER
            )
        else:
            poster_priority_config = self.config.get_field_config(CrawlerResultFields.POSTER)

        poster_priority_sites = poster_priority_config.site_prority
        if duration_ranked_sites:
            poster_priority_sites = sorted(
                poster_priority_sites,
                key=lambda site: duration_site_rank.get(site, len(duration_site_rank)),
            )
        poster_language = self.config.get_field_config(CrawlerResultFields.POSTER).language

        # 需尽力收集的字段
        for data in all_res.values():
            # 记录所有来源的 thumb url 以便后续下载
            if data.thumb:
                reduced.thumb_list.append((data.source, data.thumb))

        for site in poster_priority_sites:
            data = self._get_cached_site_result(all_res, site, poster_language)
            # 记录海报候选时严格遵循当前类型的海报优先级，避免其它字段请求过的站点混入候选。
            if data and data.poster:
                reduced.poster_list.append((data.source, data.poster, data.image_download))

        for data in all_res.values():
            # 记录所有来源的 actor 用于 Amazon 搜图
            if data.actor:
                reduced.actor_amazon.extend(data.actors)
        # 去重
        reduced.thumb_list = list(dict.fromkeys(reduced.thumb_list))  # 保序
        reduced.poster_list = list(dict.fromkeys(reduced.poster_list))  # 保序
        reduced.actor_amazon = list(set(reduced.actor_amazon))

        # 处理 release
        if normalized_release := self._normalize_release(reduced.release):
            reduced.release = normalized_release
        else:
            reduced.release = ""

        # 处理 year
        if normalized_year := self._normalize_year(reduced.year):
            reduced.year = normalized_year
        elif reduced.release:
            reduced.year = reduced.release[:4]
        else:
            reduced.year = ""

        # 处理 mosaic
        for site, result in all_res.items():
            if mosaic := normalize_mosaic(result.mosaic):
                reduced.mosaic = mosaic
                break

        # 使用 actors 字段补全 all_actors, 理想情况下前者应该是后者的子集
        # 对 actors 的所有后处理都需要同样地应用到 all_actors
        reduced.all_actors = list(dict.fromkeys(chain(reduced.all_actors, reduced.actors)))

        reduced.site_log = f"\n 🌐 [website] {'-> '.join(req_info)}"

        return reduced

    def _get_specific_crawler_language(self, website: Website) -> tuple[Language, Language]:
        title_language = self.config.get_field_config(CrawlerResultFields.TITLE).language
        org_language = title_language

        if website not in SPECIFIC_CRAWLER_TITLE_LANGUAGE_SITES:
            title_language = Language.JP
        elif website == Website.MDTV:
            title_language = Language.ZH_CN

        return title_language, org_language

    def _convert_specific_crawler_result(
        self,
        web_data_json: CrawlerResult,
        website: Website,
        title_language: Language,
        execution_time: float,
    ) -> CrawlersResult:
        res = update(CrawlersResult.empty(), web_data_json)
        if res.title:
            if res.thumb:
                res.thumb_list = [(website, res.thumb)]
            if res.poster:
                res.poster_list = [(website, res.poster, res.image_download)]

            # 加入来源信息
            res.field_sources = dict.fromkeys(CrawlerResultFields, website.value)

            # external_id
            res.external_ids[website] = web_data_json.external_id

            res.site_log = f"\n 🌐 [website] {sprint_source(website, title_language)} ({execution_time:.2f}s)"

        res.actor_amazon = web_data_json.actors
        res.all_actors = list(dict.fromkeys(chain(res.all_actors, web_data_json.actors)))
        return res

    async def _call_specific_crawler(self, task_input: CrawlerInput, website: Website) -> CrawlersResult | None:
        file_number = task_input.number
        short_number = task_input.short_number

        title_language, org_language = self._get_specific_crawler_language(website)
        task_input.language = title_language
        task_input.org_language = org_language
        web_data = await self._call_crawler(task_input, website)
        web_data_json = web_data.data
        if web_data_json is None:
            if e := web_data.debug_info.error:
                LogBuffer.error().write(str(e))
            return None

        res = self._convert_specific_crawler_result(
            web_data_json, website, title_language, web_data.debug_info.execution_time
        )

        if short_number:
            res.number = file_number

        return res

    async def _call_speed_crawlers(
        self, task_input: CrawlerInput, classification: ScrapeClassification
    ) -> CrawlersResult | None:
        """
        速度优先：按影片类型的网站顺序逐站尝试，首个返回数据的网站直接作为最终结果。
        """
        failed_info: list[str] = []
        for website in list(classification.sites or []):
            title_language, org_language = self._get_specific_crawler_language(website)
            task_input.language = title_language
            task_input.org_language = org_language
            try:
                web_data = await self._call_crawler(task_input, website)
            except TimeoutError:
                failed_info.append(f"{website.value}(请求超时)")
                continue
            except Exception as e:
                failed_info.append(f"{website.value}(失败: {e})")
                continue

            if web_data.data is None:
                if e := web_data.debug_info.error:
                    failed_info.append(f"{website.value}(失败: {e})")
                else:
                    failed_info.append(f"{website.value}(返回空数据)")
                continue

            res = self._convert_specific_crawler_result(
                web_data.data, website, title_language, web_data.debug_info.execution_time
            )
            if failed_info:
                res.field_log = "\n    ⚡ 速度优先跳过: " + " -> ".join(failed_info)
            return res

        if failed_info:
            LogBuffer.error().write("速度优先所有来源均无结果: " + " -> ".join(failed_info))
        return None

    async def _call_configured_default_fallbacks(
        self, task_input: CrawlTask, japanese_classification: ScrapeClassification
    ) -> tuple[CrawlersResult | None, ScrapeClassification]:
        """Try every configured type group when automatic routing reaches its final default."""
        get_type_sites = getattr(self.config, "get_type_sites", None)
        ordered_types = (
            japanese_classification.scraping_type,
            *(
                scraping_type
                for scraping_type in CONFIGURABLE_SCRAPING_TYPES
                if scraping_type != japanese_classification.scraping_type
            ),
        )
        channels: list[ScrapeClassification] = []
        seen_sites: set[Website] = set()
        for scraping_type in ordered_types:
            configured_sites = (
                get_type_sites(scraping_type)
                if get_type_sites
                else getattr(self.config, f"website_{scraping_type.value}", None)
            )
            if not configured_sites:
                continue
            sites = [site for site in configured_sites if site not in seen_sites]
            seen_sites.update(configured_sites)
            if not sites:
                continue
            if scraping_type == japanese_classification.scraping_type:
                channels.append(
                    replace(
                        japanese_classification,
                        sites=sites,
                        try_parallel_channel_fallback=False,
                    )
                )
            else:
                channels.append(
                    replace(
                        japanese_classification,
                        scraping_type=scraping_type,
                        sites=sites,
                        website=None,
                        reason=f"默认分类兜底；并行尝试 {scraping_type.value} 类型渠道",
                        try_parallel_channel_fallback=False,
                    )
                )

        async def run_channel(
            classification: ScrapeClassification,
        ) -> tuple[CrawlersResult | None, str, str, str]:
            channel_input = copy(task_input)
            result: CrawlersResult | None = None
            failure = ""
            try:
                if self.config.scrape_like == "speed":
                    result = await self._call_speed_crawlers(channel_input, classification)
                else:
                    result = await self._call_crawlers(channel_input, classification)
            except Exception as error:
                failure = f"{type(error).__name__}: {error}"
            finally:
                logs = LogBuffer.log().get()
                errors = LogBuffer.error().get()
                LogBuffer.clear_task()
            return result, logs, errors, failure

        channel_order = " -> ".join(channel.scraping_type.value for channel in channels) or "无已配置渠道"
        LogBuffer.log().write(f"\n 🔀 [默认分类全渠道兜底] 并行开始，选择顺序={channel_order}")
        outcomes = await asyncio.gather(*(run_channel(channel) for channel in channels))

        for channel, outcome in zip(channels, outcomes, strict=True):
            result, logs, errors, failure = outcome
            has_match_title = self._has_match_title(result)
            status = "有效匹配" if has_match_title else ("缺少标题，按未匹配处理" if result else "无结果")
            title = f"，title={result.title}" if has_match_title else ""
            LogBuffer.log().write(f"\n    [{channel.scraping_type.value}] {status}{title}")
            if failure:
                LogBuffer.log().write(f"，异常={self._redact_audit_text(failure)}")
            if logs:
                LogBuffer.log().write(f"\n    [{channel.scraping_type.value}渠道详细日志]\n{logs}")
            if errors:
                LogBuffer.error().write(f"\n    [{channel.scraping_type.value}渠道错误]\n{errors}")

        for channel, outcome in zip(channels, outcomes, strict=True):
            if self._has_match_title(outcome[0]):
                LogBuffer.log().write(f"\n    全渠道最终选择：{channel.scraping_type.value}（按类型注册顺序首个有效结果）")
                return outcome[0], channel
        LogBuffer.log().write("\n    全渠道最终选择：所有已配置类型均未匹配到结果")
        return None, japanese_classification

    async def _crawl(self, task_input: CrawlTask, website: Website | None) -> CrawlersResult | None:  # 从JSON返回元数据
        appoint_number = task_input.appoint_number
        destroyed = task_input.destroyed
        file_number = task_input.number
        leak = task_input.leak
        mosaic = task_input.mosaic
        wuma = task_input.wuma
        youma = task_input.youma

        # ================================================网站规则添加开始================================================

        if website is None:  # 从全部网站刮削
            classification = classify_scrape_task(task_input, self.config)
            self._write_route_audit(
                task_input, classification, detail_log_enabled=self.config.show_web_log
            )
            if classification.mosaic:
                task_input.mosaic = classification.mosaic
                mosaic = classification.mosaic
            if classification.website:
                res = await self._call_specific_crawler(task_input, classification.website)
            elif classification.try_parallel_channel_fallback:
                res, classification = await self._call_configured_default_fallbacks(task_input, classification)
            elif self.config.scrape_like == "speed":
                res = await self._call_speed_crawlers(task_input, classification)
            else:
                res = await self._call_crawlers(task_input, classification)
        else:
            classification = classify_scrape_task(task_input, self.config, use_fixed_type=False)
            self._write_route_audit(
                task_input,
                classification,
                forced_website=website,
                detail_log_enabled=self.config.show_web_log,
            )
            res = await self._call_specific_crawler(task_input, website)

        # ================================================网站请求结束================================================
        # ======================================超时或未找到返回

        if res is None:
            return None

        number = file_number  # res.number 实际上并未设置, 此处取 file_number
        if appoint_number:
            number = appoint_number
        res.number = number  # 此处设置
        apply_scrape_classification(res, classification)

        # 从res获取mosaic
        res.mosaic = normalize_mosaic(res.mosaic)
        if is_plain_uncensored_mosaic(res.mosaic):
            wuma = True

        # 马赛克
        if leak:
            res.mosaic = "无码流出"
        elif destroyed:
            res.mosaic = "无码破解"
        elif wuma:
            res.mosaic = "无码"
        elif youma:
            res.mosaic = "有码"
        elif mosaic:
            res.mosaic = normalize_mosaic(mosaic)
        if not res.mosaic:
            if is_uncensored(number):
                res.mosaic = "无码"
            else:
                res.mosaic = "有码"
        res.mosaic = normalize_mosaic(res.mosaic)

        # 原标题，用于amazon搜索
        res.originaltitle_amazon = res.originaltitle
        if res.actor_amazon:
            for each in res.actor_amazon:  # 去除演员名，避免搜索不到
                try:
                    end_actor = re.compile(rf" {each}$")
                    res.originaltitle_amazon = re.sub(end_actor, "", res.originaltitle_amazon)
                except Exception:
                    pass
        res.amazon_raw_director = res.director
        res.amazon_raw_studio = res.studio
        res.amazon_raw_publisher = res.publisher

        # VR 时下载小封面
        if "VR" in number:
            res.image_download = True

        return res

    def _get_site(self, task_input: CrawlTask, file_mode: FileMode):
        # 获取刮削网站
        website_name = None
        if file_mode == FileMode.Single:  # 刮削单文件（工具页面）
            website_name = Flags.website_name
        elif file_mode == FileMode.Again:  # 重新刮削
            website_temp = task_input.website_name
            if website_temp:
                website_name = website_temp
        elif self.config.scrape_like == "single":
            website_name = self.config.website_single

        return website_name

    async def run(self, task_input: CrawlTask, file_mode: FileMode) -> CrawlersResult | None:
        site = self._get_site(task_input, file_mode)
        if site is not None:
            site = Website(site)
        res = await self._crawl(task_input, site)
        if res is None:
            return None
        return _deal_res(res)
