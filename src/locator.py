"""把"章节内某论点依据的原文"定位到 PDF 的具体物理页码。

配合 per-page 标注（distill 里对逐页原文做标注）使用：
- 章节起始页（1-indexed，来自 PDF 大纲）已知
- LLM 对逐页原文标注出某论点依据的页（chapter-physical page，1-indexed）
- 本模块把两者映射成 PDF 绝对物理页 = 章节起始页 - 1 + 标注页
"""
import re
from typing import Optional


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


class PageLocator:
    """给定原文 PDF 路径，提供 字符位置->页 的映射。"""

    def __init__(self, pdf_path: str, pages: list):
        self.pdf_path = pdf_path
        self.pages = pages  # [每页文本]
        self._joined = _norm("\n".join(pages))
        # 每页在归一化拼接串里的结束偏移
        self._bounds = []
        pos = 0
        for p in pages:
            pos += len(_norm(p))
            self._bounds.append(pos)

    def _locate(self, char_idx: int) -> Optional[int]:
        for i, end in enumerate(self._bounds):
            if char_idx < end:
                return i  # 0-indexed
        return len(self._pages) - 1 if self.pages else None

    def find_phrase_page(self, phrase: str) -> Optional[int]:
        """短语首次出现的 PDF 物理页（1-indexed）。找不到返回 None。"""
        i = self._joined.find(_norm(phrase))
        if i < 0:
            return None
        p = self._locate(i)
        return p + 1 if p is not None else None

    def chapter_entry_page(self, chapter_start_page: Optional[int],
                           entry_page: Optional[int]) -> Optional[int]:
        """论点的 PDF 物理页（1-indexed）。

        entry_page：LLM 逐页标注的、章节内的物理页（1-indexed，即该章从第 1 页起算）。
        chapter_start_page：该章在 PDF 里的起始页（1-indexed）。
        映射：绝对页 = (chapter_start_page - 1) + entry_page。
        """
        if not entry_page:
            return None
        if not chapter_start_page:
            # 章节起始页未知（如无大纲）：只能返回标注页作为近似
            return int(entry_page)
        return (chapter_start_page - 1) + int(entry_page)
