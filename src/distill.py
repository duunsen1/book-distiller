"""LLM 蒸馏逻辑：章节级抽取 + 书级合并。

底层模型由 llm.LLMBackend 提供，支持 Anthropic 官方 API 和任意 OpenAI 兼容接口，
通过 LLM_PROVIDER / DISTILL_MODEL / --provider / --model 切换。
"""
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from src.llm import LLMBackend, LLMError, get_backend
from src.extract import Chapter, ChapterList
from src.locator import PageLocator
from src.prompts import (
    BOOK_SUMMARY_SYSTEM_PROMPT,
    BOOK_SUMMARY_USER_PROMPT_TEMPLATE,
    CHAPTER_SYSTEM_PROMPT,
    CHAPTER_USER_PROMPT_TEMPLATE,
    MERGE_CHAPTER_SYSTEM_PROMPT,
    PAGE_LOCATE_SYSTEM_PROMPT,
    PAGE_LOCATE_USER_PROMPT_TEMPLATE,
)

logger = logging.getLogger(__name__)

CHUNK_TOKEN_TARGET = 18_000  # 单块目标 token 数，留出系统提示词和输出空间
CHUNK_OVERLAP_RATIO = 0.10
CHAPTER_SINGLE_PASS_THRESHOLD = 60_000  # 章节正文字符数低于此值时尝试单次处理


def _count_tokens_approx(text: str) -> int:
    """粗略估算 token 数（中文约 1 字=1 token，英文约 4 字符=1 token），避免为估算再发一次 API 请求。"""
    cjk = len(re.findall(r"[一-鿿]", text))
    other = len(text) - cjk
    return cjk + other // 4


def _split_into_chunks(text: str, target_chars: int, overlap_ratio: float = CHUNK_OVERLAP_RATIO) -> List[str]:
    overlap = int(target_chars * overlap_ratio)
    step = target_chars - overlap
    chunks = []
    if len(text) <= target_chars:
        return [text]
    start = 0
    while start < len(text):
        end = min(start + target_chars, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += step
    return chunks


def _repair_json_str(text: str) -> str:
    """尽力修复模型输出里不合法的 JSON 字符串：
    - 字符串值内部的裸换行/裸制表符（JSON 字符串内不允许未转义控制字符）
    仅处理引号内部的换行，不动结构性的换行/缩进，尽量不破坏合法 JSON。
    """
    out = []
    in_str = False
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_str = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)


def _extract_json(text: str) -> dict:
    """从模型输出里提取 JSON，容忍 ```json 包裹或前后多余文字。
    依次尝试：直接解析 -> 修复字符串内裸控制字符后解析。
    """
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_repair_json_str(text))


class Distiller:
    """章节级 + 书级蒸馏。后端可插拔（Anthropic 官方 / OpenAI 兼容接口）。

    默认按环境变量自动选择后端（LLM_PROVIDER）；也可显式传入 provider/model。
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        max_output_tokens: int = 8192,
        backend: Optional[LLMBackend] = None,
    ):
        self.backend = backend or get_backend(provider=provider, model=model, max_output_tokens=max_output_tokens)
        self.model = self.backend.model
        self.provider = self.backend.provider

    def _call(self, system: str, user: str) -> str:
        return self.backend.complete(system, user)

    def _extract_entries_with_retry(self, system: str, user: str, ctx: str) -> list:
        """调 API 抽条目，JSON 解析失败时强调"严格合法 JSON"重试一次。"""
        for attempt in (1, 2):
            prompt = user if attempt == 1 else (
                user + "\n\n（重要：上一次输出不是合法 JSON，请重新输出，"
                       "确保是单一、完整、可直接 json.loads 的对象，"
                       "字符串内的换行和引号必须正确转义，不要输出任何 JSON 之外的文字。）"
            )
            raw = self._call(system, prompt)
            try:
                data = _extract_json(raw)
                return data.get("entries", [])
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("%s 第 %d 次 JSON 解析失败: %s", ctx, attempt, e)
        logger.error("%s 重试后仍失败，该块无条目", ctx)
        return []

    # ---------- 章节级 ----------

    def distill_chapter(self, book_title: str, book_author: str, chapter: Chapter) -> List[dict]:
        chunk_target_chars = CHUNK_TOKEN_TARGET  # 粗略 1 字≈1 token，直接用字符数当目标
        chunks = _split_into_chunks(chapter.text, chunk_target_chars)
        logger.info("chapter %r: %d chars -> %d chunk(s)", chapter.title, len(chapter.text), len(chunks))

        all_entries = []
        for i, chunk in enumerate(chunks):
            page_info = f"（原文页码：第 {chapter.page_start} 页起）" if chapter.page_start else ""
            user_prompt = CHAPTER_USER_PROMPT_TEMPLATE.format(
                book_title=book_title,
                book_author=book_author,
                chapter_title=chapter.title,
                page_info=page_info,
                chunk_text=chunk,
            )
            all_entries.extend(self._extract_entries_with_retry(
                CHAPTER_SYSTEM_PROMPT, user_prompt, f"章节{chapter.title!r} 块{i}"))

        if len(chunks) > 1:
            # 多块抽取后再合并去重一次
            merged_raw = self._call(
                MERGE_CHAPTER_SYSTEM_PROMPT,
                json.dumps({"entries": all_entries}, ensure_ascii=False),
            )
            try:
                data = _extract_json(merged_raw)
                all_entries = data.get("entries", all_entries)
            except (json.JSONDecodeError, KeyError):
                logger.warning("chapter %r: 合并阶段 JSON 解析失败，使用未合并结果", chapter.title)

        # 给每条论点标注具体页码（仅 PDF 且该章有逐页文本时）
        if chapter.source_format == "pdf" and chapter.pages:
            self._annotate_entry_pages(book_title, chapter, all_entries)

        return all_entries

    # ---------- 论据 -> 具体页码 ----------

    def _annotate_entry_pages(self, book_title: str, chapter: Chapter, entries: List[dict]):
        """对章节的每条论点，用 LLM 判断其主要依据出现在该章的哪一页，
        映射成 PDF 绝对物理页，写入 entry['source_page']。失败则留空（退化为章节起始页）。"""
        if not entries:
            return
        ctx = f"章节{chapter.title!r} 页码标注"
        # 该章逐页原文（物理页号 + 文本）
        pages = chapter.pages  # [(1-indexed 物理页, 文本)]
        # 若单章页数/字符过多，分批标注（每批最多 ~8 页或 ~15000 字）
        MAX_CHARS = 15000
        batches = []
        cur, cur_len = [], 0
        for pnum, ptext in pages:
            if cur and cur_len + len(ptext) > MAX_CHARS:
                batches.append(cur); cur, cur_len = [], 0
            cur.append((pnum, ptext)); cur_len += len(ptext)
        if cur:
            batches.append(cur)

        # 每个论点先给个默认页：章节起始页（找不到依据时兜底）
        for e in entries:
            e["source_page"] = chapter.page_start

        def annotate_batch(batch):
            backend = get_backend(provider=self.provider, model=self.model,
                                  max_output_tokens=self.backend.max_output_tokens)
            pages_text = "\n\n".join("[第%d页]\n%s" % (p, t) for p, t in batch)
            entries_list = "\n".join("%d. %s" % (i, e.get("核心论点", ""))
                                     for i, e in enumerate(entries, 1))
            user = PAGE_LOCATE_USER_PROMPT_TEMPLATE.format(
                book_title=book_title,
                chapter_title=chapter.title,
                entries_list=entries_list,
                pages_text=pages_text,
            )
            raw = backend.complete(PAGE_LOCATE_SYSTEM_PROMPT, user)
            data = _extract_json(raw)
            page_map = data.get("pages", {})
            # LLM 返回的页码即 PDF 绝对物理页（[第N页] 标记用的是物理页）。
            # 兼容 key/value 为字符串或整数的情况。
            pm = {}
            for k, v in page_map.items():
                try:
                    ki, vi = int(k), int(v)
                except (ValueError, TypeError):
                    continue
                if ki >= 1 and vi >= 1:
                    pm[ki] = vi
            return pm

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(annotate_batch, b) for b in batches]
            for fu in futures:
                try:
                    pm = fu.result()
                    for idx, phys_page in pm.items():
                        if 1 <= idx <= len(entries):
                            entries[idx - 1]["source_page"] = phys_page
                except Exception as e:
                    logger.warning("%s 批次标注失败（保留章节起始页兜底）: %s", ctx, e)
        n_located = sum(1 for e in entries if e.get("source_page"))
        logger.info("%s: %d/%d 条已标注具体页", ctx, n_located, len(entries))

    # ---------- 书级 ----------

    def distill_book_summary(
        self,
        book_title: str,
        book_author: str,
        chapter_entries: List[tuple],  # [(Chapter, [entries])]
    ) -> dict:
        digest_lines = []
        for chapter, entries in chapter_entries:
            digest_lines.append(f"## {chapter.title}")
            for j, e in enumerate(entries, 1):
                digest_lines.append(f"{j}. " + json.dumps(e, ensure_ascii=False))
            digest_lines.append("")
        digest = "\n".join(digest_lines)

        # 章节条目太多时同样分块（避免超长输入）
        if _count_tokens_approx(digest) > 80_000:
            raise RuntimeError("全书条目合并输入过长，暂未实现书级条目再分块，建议减少章节数或增大 CHUNK_TOKEN_TARGET")

        user_prompt = BOOK_SUMMARY_USER_PROMPT_TEMPLATE.format(
            book_title=book_title, book_author=book_author, chapters_digest=digest
        )
        raw = self._call(BOOK_SUMMARY_SYSTEM_PROMPT, user_prompt)
        return _extract_json(raw)
