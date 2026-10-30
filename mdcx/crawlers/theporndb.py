#!/usr/bin/env python3
import asyncio
import re
from pathlib import Path
from typing import Literal, override
from urllib.parse import quote, urlencode

import oshash

from ..config.enums import Switch, Website
from ..config.manager import manager
from .base import BaseCrawler, Context, CralwerException, CrawlerData
from .theporndb_knowledge import compact_name, find_known_entities, find_known_entities_in_name, load_known_entities
from .theporndb_match import (
    DURATION_TOLERANCE_SECONDS,
    MATCH_THRESHOLD,
    ParsedTPDBFilename,
    build_entity_search_specs,
    build_search_variants,
    candidate_audit_summary,
    explain_duration_candidates,
    explain_filename_candidates,
    explain_full_filename_title_candidates,
    explain_known_entity_candidates,
    get_media_duration_seconds,
    merge_candidates,
    parse_tpdb_filename,
    parse_tpdb_folder_name,
)

TheporndbKind = Literal["scenes", "movies"]


def get_year(release):
    try:
        return re.findall(r"\d{4}", release)[0]
    except Exception:
        return ""


def get_number(series, release, title):
    try:
        if series and release:
            return series.replace(" ", "") + "." + re.findall(r"\d{2}-\d{2}-\d{2}", release)[0].replace("-", ".")
    except Exception:
        pass
    return title


def read_data(data, kind: TheporndbKind):
    title = data.get("title") or ""
    outline = data.get("description")
    outline = "" if not outline else outline.replace("＜p＞", "").replace("＜/p＞", "")
    release = data.get("date") or ""
    trailer = data.get("trailer") or ""
    try:
        cover = data["background"]["large"]
    except Exception:
        cover = data.get("image")
    cover = cover or ""
    try:
        poster = data["posters"]["large"]
    except Exception:
        poster = data.get("poster")
    poster = poster or ""
    try:
        runtime = str(int(int(data.get("duration")) / 60))
    except Exception:
        runtime = ""
    try:
        series = data["site"]["name"]
    except Exception:
        series = ""
    try:
        studio = data["site"]["network"]["name"]
    except Exception:
        studio = ""
    publisher = studio
    try:
        director = data["director"]["name"]
    except Exception:
        director = ""
    tag_list = []
    try:
        for each in data["tags"]:
            tag_list.append(each["name"])
    except Exception:
        pass
    slug = data.get("slug") or ""
    real_url = f"https://api.theporndb.net/{kind}/{slug}" if slug else ""
    all_actor_list = []
    actor_list = []
    performers = data.get("performers") or []
    if isinstance(performers, list):
        for performer in performers:
            if not isinstance(performer, dict) or not performer.get("name"):
                continue
            name = str(performer["name"])
            all_actor_list.append(name)
            parent = performer.get("parent") or {}
            extras = {}
            if isinstance(parent, dict):
                extras = parent.get("extras") or {}
            gender = extras.get("gender") if isinstance(extras, dict) else None
            if gender and gender != "Male":
                actor_list.append(name)
    number = get_number(series, release, title)
    return CrawlerData(
        number=number,
        title=title,
        originaltitle=title,
        actors=actor_list,
        all_actors=all_actor_list,
        outline=outline,
        originalplot=outline,
        tags=tag_list,
        release=release,
        year=get_year(release),
        runtime=runtime,
        score="",
        series=series,
        directors=[director] if director else [],
        studio=studio,
        publisher=publisher,
        thumb=cover,
        poster=poster,
        extrafanart=[],
        trailer=trailer,
        image_download=False,
        mosaic="无码",
        external_id=real_url,
        wanted="",
    )


class TheporndbCrawler(BaseCrawler):
    @classmethod
    @override
    def site(cls) -> Website:
        return Website.THEPORNDB

    @classmethod
    @override
    def base_url_(cls) -> str:
        return "https://api.theporndb.net"

    def _headers(self):
        api_token = manager.config.theporndb_api_token
        if not api_token:
            raise CralwerException("请添加 API Token 后刮削！（「设置」-「网络」-「API Token」）")
        return {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.202 Safari/537.36",
        }

    async def _search_by_known_entities(
        self,
        ctx: Context,
        kind: TheporndbKind,
        file_path: str,
        parsed,
        existing_candidates: list[dict],
        headers: dict[str, str],
        *,
        hint_name: str | None = None,
        hint_source: str = "文件名",
    ) -> list[dict]:
        known = load_known_entities()
        matched = (
            find_known_entities(file_path, known)
            if hint_name is None
            else find_known_entities_in_name(hint_name, known)
        )
        release_dates = parsed.release_dates
        # The folder's parsed site/date/title already gets the normal search pass;
        # this dictionary pass only trusts learned vendor names, not its first token.
        site_hint = parsed.site if hint_source == "文件名" else ""
        vendor_hints_list = []
        seen_vendor_keys: set[str] = set()
        for vendor in (site_hint, *matched.vendors):
            vendor_key = compact_name(vendor)
            if vendor and vendor_key not in seen_vendor_keys:
                vendor_hints_list.append(vendor)
                seen_vendor_keys.add(vendor_key)
        vendor_hints = tuple(vendor_hints_list)
        ctx.debug(
            f"{hint_source}词典实体识别: "
            f"sites={', '.join(matched.sites) or '-'}, "
            f"studios={', '.join(matched.studios) or '-'}, "
            f"performers={', '.join(matched.performers) or '-'}; "
            f"dictionary sizes: sites={len(known.sites)}, studios={len(known.studios)}, "
            f"performers={len(known.performers)}"
        )
        ctx.debug(f"候选提示: vendors/sites={', '.join(vendor_hints) or '-'}, dates={' | '.join(release_dates) or '-'}")
        if hint_source == "父文件夹" and not (matched.vendors or matched.performers or release_dates):
            ctx.debug("父文件夹兜底跳过：目录名没有已知实体或有效日期")
            return []
        if not vendor_hints and not matched.performers and not release_dates:
            ctx.debug(f"字典＋日期兜底跳过：{hint_source}没有可用于筛选候选的实体或日期")
            return []

        ctx.debug(
            f"{hint_source}字典＋日期候选规则: 识别到的厂商/站点、演员分别作为可选约束；"
            "解析出的任一可能日期可命中，候选缺少日期时保留给时长阶段判断；"
            f"不要求{hint_source}必须包含演员，也不在此阶段用时长做最终判断"
        )

        candidates = list(existing_candidates)
        search_errors: list[str] = []
        query_specs = build_entity_search_specs(vendor_hints, matched.performers, release_dates)
        ctx.debug(
            f"{hint_source}字典＋日期查询计划: {len(query_specs)} 条（保留全部日期解释）"
        )

        for site, query_text in query_specs:
            query_params = {"site": site} if site else {}
            if query_text:
                query_params["q"] = query_text
            query_params["per_page"] = 100
            params = urlencode(query_params)
            search_url = f"{self.base_url}/{kind}?{params}"
            ctx.debug(f"{hint_source}字典＋日期候选补充请求: {search_url}")
            ctx.debug_info.search_urls = [*(ctx.debug_info.search_urls or []), search_url]
            try:
                response, error = await self.async_client.get_json(search_url, headers=headers)
            except Exception as e:
                search_errors.append(f"site={site or '*'}, query={query_text or '*'}: {type(e).__name__}: {e}")
                ctx.debug(
                    f"候选补充请求抛出异常: site={site or '*'}, query={query_text or '*'}, "
                    f"error={type(e).__name__}: {e}"
                )
                continue
            if response is None:
                if "HTTP 401" in str(error):
                    raise CralwerException("请检查 ThePornDB API Token 是否正确")
                search_errors.append(str(error))
                ctx.debug(
                    f"候选补充请求失败: site={site or '*'}, query={query_text or '*'}, error={error}"
                )
                continue
            if not isinstance(response, dict):
                ctx.debug(
                    f"候选补充响应格式异常: site={site or '*'}, query={query_text or '*'}, "
                    f"response_type={type(response).__name__}"
                )
                continue
            rows = response.get("data") or []
            if isinstance(rows, dict):
                rows = [rows]
            if isinstance(rows, list):
                previous_count = len(candidates)
                candidates = merge_candidates(candidates, rows)
                ctx.debug(
                    f"候选补充查询结果: site={site or '*'}, query={query_text or '*'}, "
                    f"returned={len(rows)}, added={len(candidates) - previous_count}, total={len(candidates)}"
                )
            else:
                ctx.debug(
                    f"候选补充查询结果格式异常: site={site or '*'}, query={query_text or '*'}, "
                    f"data_type={type(rows).__name__}"
                )

        matches, audit_counts, audit_examples = explain_known_entity_candidates(
            candidates,
            matched,
            release_dates=release_dates,
            site_hint=site_hint,
        )
        ctx.debug(
            f"{hint_source}字典＋日期候选筛选统计: "
            f"候选={audit_counts['candidates']}, 符合条件={audit_counts['eligible']}, "
            f"厂商不符={audit_counts['vendor_mismatch']}, 演员不符={audit_counts['performer_mismatch']}, "
            f"日期不符={audit_counts['date_mismatch']}, 日期未知={audit_counts['date_unknown']}, "
            f"缺少筛选提示={audit_counts['missing_hints']}"
        )
        for example in audit_examples:
            ctx.debug(f"{hint_source}字典＋日期候选样本: {example}")
        ctx.debug(f"{hint_source}字典＋日期兜底候选数: {len(matches)}")
        if search_errors:
            ctx.debug(f"候选补充搜索错误: {'; '.join(search_errors)}")
        return matches

    async def _search_by_filename_candidates(
        self,
        ctx: Context,
        kind: TheporndbKind,
        parsed: ParsedTPDBFilename,
        existing_candidates: list[dict],
        headers: dict[str, str],
        *,
        hint_source: str,
    ) -> tuple[list[dict], list[dict], list[str], str]:
        """Run the same filename-style query and strict candidate check for either name source."""
        variants = build_search_variants(parsed)
        ctx.debug(
            f"{hint_source}解析: site={parsed.site or '-'}, "
            f"dates={' | '.join(parsed.release_dates) or '-'}, name={parsed.name or '-'}"
        )
        ctx.debug(
            f"{hint_source}查询计划: variants={len(variants)}, "
            f"queries={' | '.join(variant.query for variant in variants) or '-'}"
        )
        ctx.debug(
            f"{hint_source}候选规则: site_match、发布日期命中任一解析结果，"
            f"并且标题/演员组合 name_score≥{MATCH_THRESHOLD:.1f}"
        )

        candidates = list(existing_candidates)
        search_errors: list[str] = []
        last_search_url = ""
        ambiguous_dates = len(parsed.release_dates) > 1
        for variant_index, variant in enumerate(variants, start=1):
            query = "parse" if kind == "scenes" else "q"
            encoded_query = quote(variant.query, safe=".-_")
            last_search_url = f"{self.base_url}/{kind}?{query}={encoded_query}&per_page=100"
            ctx.debug(
                f"{hint_source}查询 [{variant_index}/{len(variants)}] 开始: "
                f"query={variant.query}, url={last_search_url}"
            )
            ctx.debug_info.search_urls = [*(ctx.debug_info.search_urls or []), last_search_url]
            try:
                response, error = await self.async_client.get_json(last_search_url, headers=headers)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                response = None
            if response is None:
                if "HTTP 401" in str(error):
                    raise CralwerException("请检查 ThePornDB API Token 是否正确")
                search_errors.append(str(error))
                ctx.debug(f"{hint_source}查询请求失败: query={variant.query}, error={error}; 继续下一查询")
                continue
            if not isinstance(response, dict):
                ctx.debug(
                    f"{hint_source}查询响应格式异常: query={variant.query}, "
                    f"response_type={type(response).__name__}; 继续下一查询"
                )
                continue

            result_data = response.get("data") or []
            if isinstance(result_data, dict):
                result_data = [result_data]
            old_candidate_count = len(candidates)
            if isinstance(result_data, list):
                candidates = merge_candidates(candidates, result_data)
            matches, audit_counts, audit_examples = explain_filename_candidates(parsed, candidates)
            ctx.debug(
                f"{hint_source}查询响应: query={variant.query}, "
                f"returned={len(result_data) if isinstance(result_data, list) else 0}, "
                f"新增去重候选={len(candidates) - old_candidate_count}, 累计候选={len(candidates)}"
            )
            ctx.debug(
                f"{hint_source}候选筛选统计: "
                f"候选={audit_counts['candidates']}, 命中={audit_counts['matched']}, "
                f"站点不符={audit_counts['site_mismatch']}, 日期缺失={audit_counts['date_missing']}, "
                f"日期不符={audit_counts['date_mismatch']}, 名称分低于阈值={audit_counts['name_mismatch']}"
            )
            for example in audit_examples:
                ctx.debug(f"{hint_source}候选判定: {example}")
            if len(matches) == 1:
                candidate_slug = matches[0].get("slug") or matches[0].get("id")
                if candidate_slug:
                    ctx.debug(
                        f"原有逻辑暂定候选（尚未成功）: source={hint_source}, query={variant.query}, "
                        f"{candidate_audit_summary(matches[0])}"
                    )
                    if not ambiguous_dates:
                        break
                    ctx.debug(f"{hint_source}日期存在多种解释，继续查询其余日期候选")
                else:
                    ctx.debug(f"{hint_source}唯一候选缺少 slug/id，继续更宽查询")
            elif len(matches) > 1:
                ctx.debug(f"{hint_source}查询产生 {len(matches)} 个候选: query={variant.query}，继续补齐候选")
            else:
                ctx.debug(f"{hint_source}查询未命中: query={variant.query}")

        strict_candidates, audit_counts, audit_examples = explain_filename_candidates(parsed, candidates)
        ctx.debug(
            f"{hint_source}原有逻辑候选汇总: 接口结果={len(candidates)}, "
            f"严格候选={len(strict_candidates)}, 站点不符={audit_counts['site_mismatch']}, "
            f"日期缺失={audit_counts['date_missing']}, 日期不符={audit_counts['date_mismatch']}, "
            f"名称不符={audit_counts['name_mismatch']}"
        )
        for example in audit_examples:
            ctx.debug(f"{hint_source}候选: {example}")
        return candidates, strict_candidates, search_errors, last_search_url

    @override
    async def _run(self, ctx: Context):
        errors = []
        self._duration_cache: dict[str, float | None] = {}
        ctx.debug(
            "ThePornDB 流程开始: "
            f"file={ctx.input.file_path or '-'}, number={ctx.input.number or '-'}, "
            f"token_configured={bool(manager.config.theporndb_api_token)}"
        )
        for kind in ("scenes", "movies"):
            ctx.debug(f"类型阶段开始: kind={kind}")
            try:
                data = await self._scrape_kind(ctx, kind)
                result = data.to_result()
                result.source = self.site().value
                ctx.debug(
                    "ThePornDB 最终成功: "
                    f"kind={kind}, title={result.title or '-'}, external_id={result.external_id or '-'}"
                )
                return result
            except Exception as e:
                errors.append(f"{kind}: {e}")
                ctx.debug(f"类型阶段失败: kind={kind}, error={type(e).__name__}: {e}; 将尝试下一个类型")
        ctx.debug(f"ThePornDB 所有类型均失败: {'；'.join(errors)}")
        raise CralwerException("；".join(errors))

    async def _scrape_kind(self, ctx: Context, kind: TheporndbKind) -> CrawlerData:
        file_path = str(ctx.input.file_path or f"{ctx.input.number}.mp4")
        ctx.debug(f"输入检查: kind={kind}, file={file_path}, appoint_url={ctx.input.appoint_url or '-'}")
        ctx.debug(f"认证检查: ThePornDB token 已配置={bool(manager.config.theporndb_api_token)}（不记录 token 值）")
        headers = self._headers()
        real_url = ctx.input.appoint_url.replace("//theporndb", "//api.theporndb")
        real_url = real_url.replace("/scenes/", f"/{kind}/").replace("/movies/", f"/{kind}/") if real_url else ""
        match_source = "指定详情页 URL" if real_url else ""
        if real_url:
            ctx.debug(f"匹配策略: 使用指定详情页，不执行 Hash/文件名/实体词典搜索; kind={kind}")

        if not real_url:
            use_hash = kind == "movies" or Switch.THEPORNDB_NO_HASH not in manager.config.switch_on
            hash_reason = (
                "movies 类型强制尝试 Hash"
                if kind == "movies"
                else "配置允许 Hash"
                if use_hash
                else "设置了 theporndb_no_hash，scenes 类型跳过 Hash"
            )
            ctx.debug(f"Hash 策略: kind={kind}, enabled={use_hash}, reason={hash_reason}")
            if use_hash:
                try:
                    file_hash = oshash.oshash(file_path)
                    hash_url = f"{self.base_url}/{kind}/hash/{file_hash}"
                    ctx.debug(f"Hash 计算完成: value={file_hash}")
                    ctx.debug(f"Hash 请求开始: {hash_url}")
                    ctx.debug_info.search_urls = [*(ctx.debug_info.search_urls or []), hash_url]
                    hash_search, error = await self.async_client.get_json(hash_url, headers=headers)
                    if hash_search is None:
                        if "HTTP 401" in str(error):
                            ctx.debug(f"Hash 请求被拒绝: {error}")
                            raise CralwerException("请检查 ThePornDB API Token 是否正确")
                        ctx.debug(f"Hash 请求失败，继续文件名搜索: {error}")
                        hash_search = {}
                    hash_data = hash_search.get("data") if isinstance(hash_search, dict) else None
                    if isinstance(hash_data, dict) and hash_data:
                        ctx.debug(f"Hash 匹配命中，停止本类型后续搜索: {candidate_audit_summary(hash_data)}")
                        match_source = "Hash 命中"
                        return read_data(hash_data, kind)
                    ctx.debug(
                        "Hash 匹配未命中: "
                        f"response_data_type={type(hash_data).__name__}, "
                        f"has_data={bool(hash_data) if hash_data is not None else False}; 继续文件名搜索"
                    )
                except CralwerException as e:
                    ctx.debug(f"Hash 阶段终止当前类型: {type(e).__name__}: {e}")
                    raise
                except Exception as e:
                    ctx.debug(f"Hash 计算/请求异常，回退文件名搜索: {type(e).__name__}: {e}")
            else:
                ctx.debug("Hash 阶段跳过: 按配置直接进入文件名搜索")

            parsed = parse_tpdb_filename(file_path)
            candidates, original_candidates, search_errors, last_search_url = await self._search_by_filename_candidates(
                ctx,
                kind,
                parsed,
                [],
                headers,
                hint_source="文件名",
            )

            if file_path not in self._duration_cache:
                self._duration_cache[file_path] = await asyncio.to_thread(get_media_duration_seconds, file_path)
            local_duration = self._duration_cache[file_path]
            ctx.debug(
                f"本地视频时长: {local_duration:.1f}s" if local_duration is not None else "本地视频时长读取失败"
            )
            ctx.debug(f"最终时长筛选容差: ±{DURATION_TOLERANCE_SECONDS}s（绝对时长差不超过 3 分钟）")

            selected_candidates, duration_counts, duration_examples = explain_duration_candidates(
                original_candidates, local_duration
            )
            ctx.debug(
                "阶段3 时长筛选（阶段1候选）: "
                f"候选={duration_counts['candidates']}, 容差内={duration_counts['within_tolerance']}, "
                f"时长不符={duration_counts['duration_mismatch']}, 时长未知={duration_counts['duration_unknown']}, "
                f"仍有歧义={duration_counts['ambiguous']}"
            )
            for example in duration_examples:
                ctx.debug(f"阶段3 时长判定: {example}")

            match_source = ""
            candidate_queue = list(original_candidates)
            if not selected_candidates:
                ctx.debug("阶段1未得到唯一时长候选，进入阶段2：实体词典＋多格式日期补充候选")
                fallback_candidates = await self._search_by_known_entities(
                    ctx,
                    kind,
                    file_path,
                    parsed,
                    candidates,
                    headers,
                )
                candidate_queue = merge_candidates(original_candidates, fallback_candidates)
                ctx.debug(
                    f"阶段2候选合并: 原有逻辑候选={len(original_candidates)}, "
                    f"字典＋日期候选={len(fallback_candidates)}, 去重后候选={len(candidate_queue)}"
                )
                selected_candidates, duration_counts, duration_examples = explain_duration_candidates(
                    candidate_queue, local_duration
                )
                ctx.debug(
                    "阶段3 最终时长筛选: "
                    f"候选={duration_counts['candidates']}, 容差内={duration_counts['within_tolerance']}, "
                    f"时长不符={duration_counts['duration_mismatch']}, "
                    f"时长未知={duration_counts['duration_unknown']}, "
                    f"仍有歧义={duration_counts['ambiguous']}"
                )
                for example in duration_examples:
                    ctx.debug(f"阶段3 最终时长判定: {example}")
                if selected_candidates:
                    match_source = "原有逻辑/字典日期候选＋时长最终筛选"
            else:
                match_source = "原有逻辑候选＋时长最终筛选"

            if not selected_candidates:
                title_candidate_queue = merge_candidates(candidates, candidate_queue)
                title_candidates, title_counts, title_examples = explain_full_filename_title_candidates(
                    file_path, title_candidate_queue
                )
                ctx.debug(
                    "阶段3b 完整文件名标题兜底候选: "
                    f"候选={title_counts['candidates']}, 标题命中={title_counts['matched']}, "
                    f"标题不符={title_counts['name_mismatch']}"
                )
                for example in title_examples:
                    ctx.debug(f"阶段3b 完整文件名标题判定: {example}")
                selected_candidates, duration_counts, duration_examples = explain_duration_candidates(
                    title_candidates, local_duration
                )
                ctx.debug(
                    "阶段3b 标题候选时长确认: "
                    f"候选={duration_counts['candidates']}, 容差内={duration_counts['within_tolerance']}, "
                    f"时长不符={duration_counts['duration_mismatch']}, "
                    f"时长未知={duration_counts['duration_unknown']}, "
                    f"仍有歧义={duration_counts['ambiguous']}"
                )
                for example in duration_examples:
                    ctx.debug(f"阶段3b 标题时长判定: {example}")
                if selected_candidates:
                    match_source = "完整文件名标题候选＋时长最终筛选"

            if not selected_candidates:
                folder_name = Path(file_path).parent.name
                if folder_name:
                    folder_parsed = parse_tpdb_folder_name(folder_name)
                    ctx.debug(
                        "阶段4 Hash/文件名匹配未得到唯一结果，直接父文件夹完整重跑名称匹配: "
                        f"folder={folder_name}, site={folder_parsed.site or '-'}, "
                        f"dates={' | '.join(folder_parsed.release_dates) or '-'}, "
                        f"name={folder_parsed.name or '-'}"
                    )
                    (
                        folder_api_candidates,
                        folder_original_candidates,
                        folder_search_errors,
                        folder_last_search_url,
                    ) = await self._search_by_filename_candidates(
                        ctx,
                        kind,
                        folder_parsed,
                        candidate_queue,
                        headers,
                        hint_source="父文件夹",
                    )
                    search_errors.extend(folder_search_errors)
                    last_search_url = folder_last_search_url or last_search_url
                    ctx.debug(
                        f"阶段4 父文件夹原有逻辑候选: 接口候选={len(folder_api_candidates)}, "
                        f"严格候选={len(folder_original_candidates)}"
                    )
                    (
                        selected_candidates,
                        duration_counts,
                        duration_examples,
                    ) = explain_duration_candidates(folder_original_candidates, local_duration)
                    ctx.debug(
                        "阶段4 父文件夹文件名候选时长筛选: "
                        f"候选={duration_counts['candidates']}, 容差内={duration_counts['within_tolerance']}, "
                        f"时长不符={duration_counts['duration_mismatch']}, "
                        f"时长未知={duration_counts['duration_unknown']}, "
                        f"仍有歧义={duration_counts['ambiguous']}"
                    )
                    for example in duration_examples:
                        ctx.debug(f"阶段4 父文件夹文件名时长判定: {example}")

                    if selected_candidates:
                        match_source = "父文件夹原有文件名候选＋时长最终筛选"
                    else:
                        ctx.debug("阶段4 父文件夹文件名逻辑未唯一通过，继续父文件夹实体词典＋日期兜底")
                        folder_dictionary_candidates = await self._search_by_known_entities(
                            ctx,
                            kind,
                            file_path,
                            folder_parsed,
                            folder_api_candidates,
                            headers,
                            hint_name=folder_name,
                            hint_source="父文件夹",
                        )
                        folder_candidate_queue = merge_candidates(
                            folder_original_candidates,
                            folder_dictionary_candidates,
                        )
                        ctx.debug(
                            f"阶段4 父文件夹候选合并: 名称严格候选={len(folder_original_candidates)}, "
                            f"字典＋日期候选={len(folder_dictionary_candidates)}, "
                            f"去重后={len(folder_candidate_queue)}"
                        )
                        selected_candidates, duration_counts, duration_examples = explain_duration_candidates(
                            folder_candidate_queue, local_duration
                        )
                        ctx.debug(
                            "阶段4 父文件夹字典候选时长筛选: "
                            f"候选={duration_counts['candidates']}, 容差内={duration_counts['within_tolerance']}, "
                            f"时长不符={duration_counts['duration_mismatch']}, "
                            f"时长未知={duration_counts['duration_unknown']}, "
                            f"仍有歧义={duration_counts['ambiguous']}"
                        )
                        for example in duration_examples:
                            ctx.debug(f"阶段4 父文件夹字典时长判定: {example}")
                        if selected_candidates:
                            match_source = "文件名/父文件夹词典日期候选＋时长最终筛选"
                else:
                    ctx.debug("阶段4 父文件夹兜底跳过：视频文件没有可用的父目录名")

            if selected_candidates:
                selected = selected_candidates[0]
                candidate_slug = selected.get("slug") or selected.get("id")
                if candidate_slug:
                    real_url = f"{self.base_url}/{kind}/{quote(str(candidate_slug), safe='-')}"
                    ctx.debug(f"最终匹配候选: 来源={match_source}; {candidate_audit_summary(selected)}")
                else:
                    real_url = ""
                    ctx.debug("最终时长候选缺少 slug/id，无法请求详情")
            else:
                real_url = ""
                ctx.debug("最终匹配决定: 无唯一候选通过时长筛选，保持未匹配")

            if not real_url:
                if search_errors and not candidates:
                    raise CralwerException(f"ThePornDB 搜索请求失败: {'; '.join(search_errors)}")
                raise CralwerException(f"未找到唯一匹配的内容: {last_search_url}")

        ctx.debug(f"最终匹配来源: {match_source}; kind={kind}; detail_url={real_url}")
        ctx.debug(f"详情请求开始: url={real_url}")
        ctx.debug_info.detail_urls = [*(ctx.debug_info.detail_urls or []), real_url]
        try:
            res_real, error = await self.async_client.get_json(real_url, headers=headers)
        except Exception as e:
            ctx.debug(f"详情请求抛出异常: {type(e).__name__}: {e}")
            raise
        if res_real is None:
            if "HTTP 401" in str(error):
                ctx.debug(f"详情请求认证失败: {error}")
                raise CralwerException("请检查 ThePornDB API Token 是否正确")
            ctx.debug(f"详情请求失败: {error}")
            raise CralwerException(f"请求错误: {error}")

        real_data = res_real.get("data") if isinstance(res_real, dict) else None
        if not isinstance(real_data, dict) or not real_data:
            ctx.debug(
                f"详情响应无有效 data: response_type={type(res_real).__name__}, "
                f"data_type={type(real_data).__name__}, value={real_data!r}"
            )
            raise CralwerException(f"未获取正确数据: {real_url}")
        ctx.debug(f"详情响应通过: {candidate_audit_summary(real_data)}")
        result = read_data(real_data, kind)
        ctx.debug(
            f"详情映射完成: title={result.title or '-'}, external_id={result.external_id or '-'}, "
            f"actors={','.join(result.all_actors) if isinstance(result.all_actors, list) else '-'}"
        )
        return result

    @override
    async def _generate_search_url(self, ctx: Context) -> list[str] | str | None:
        return None

    @override
    async def _parse_search_page(self, ctx: Context, html, search_url: str) -> list[str] | str | None:
        return None

    @override
    async def _parse_detail_page(self, ctx: Context, html, detail_url: str) -> CrawlerData | None:
        return None
