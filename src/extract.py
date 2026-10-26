"""书籍原文文本提取。

输入：raw/ 下的 .pdf 或 .epub 文件
输出：统一的 ChapterList = [{"title": str, "text": str, "page_start": int|None}]

EPUB：ebooklib 按 toc/spine 顺序遍历章节，BeautifulSoup 去标签。
PDF：pymupdf 拿大纲（get_toc），有大纲则按大纲切章节并用 pymupdf4llm 提取正文；
    无大纲则退化为按固定页数切块，标题标记为"未识别章节 N"。
"""
import re
from dataclasses import dataclass
from typing import List, Optional

from bs4 import BeautifulSoup


@dataclass
class Chapter:
    title: str
    text: str
    page_start: Optional[int] = None
    # 该章按页拆分的文本（仅 PDF 有；EPUB 无页码概念，保持 None）
    # 元素为 (page_number_1indexed, page_text)
    pages: Optional[list] = None
    source_format: str = ""  # "pdf" / "epub"


ChapterList = List[Chapter]


# ---------- EPUB ----------

def extract_epub(path: str) -> ChapterList:
    import ebooklib
    from ebooklib import epub

    try:
        book = epub.read_epub(path)
    except ValueError as e:
        raise RuntimeError(f"无法解析 EPUB（可能是 DRM 或损坏文件）: {e}") from e

    chapters = []
    # 建立 文件名 -> 内部item_id 的映射，toc 的 href 是文件名，get_item_with_id 认的是内部 id
    fname_to_id = {}
    for item in book.get_items():
        try:
            fname_to_id[item.file_name] = item.get_id()
        except Exception:
            pass

    items = []
    # 优先用 toc（可能嵌套），退化为 spine
    toc = book.toc
    if toc:
        def _norm_href(href):
            """toc 的 href 可能是字符串、list（多链接）或 None，归一成第一个非空字符串文件名。"""
            if href is None:
                return None
            if isinstance(href, (list, tuple)):
                for h in href:
                    if h:
                        return str(h).split("#")[0]
                return None
            return str(href).split("#")[0] or None

        def flatten(node_list):
            for node in node_list:
                if isinstance(node, tuple):
                    # (title, href, subitems...)；href 可能是 list
                    href = _norm_href(node[1])
                    if href:
                        yield node[0], href
                    if len(node) > 2 and node[2]:
                        yield from flatten(node[2])
                else:
                    # ebooklib Link 对象：title + href（href 是文件名，可能带 #anchor）
                    href = _norm_href(getattr(node, "href", None))
                    if href:
                        yield node.title, href
        items = list(flatten(toc))
    else:
        items = [(getattr(item, "title", None) or f"第 {i+1} 章", item.get_id())
                 for i, item in enumerate(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))]

    seen_ids = set()
    for title, ref in items:
        # 防御：ref 必须是可哈希的字符串（文件名或内部 id），否则跳过该项
        if not isinstance(ref, str) or not ref:
            continue
        # toc 路径下 ref 是文件名，spine 路径下 ref 就是内部 id
        item_id = fname_to_id.get(ref, ref)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        html = item.get_content().decode("utf-8", errors="ignore")
        text = BeautifulSoup(html, "html.parser").get_text("\n")
        text = _clean_text(text)
        if not text.strip():
            continue
        chapters.append(Chapter(title=str(title).strip(), text=text, page_start=None, source_format="epub"))

    if not chapters:
        # 兜底：直接按 spine 顺序全部拼接为一个"未识别章节"
        texts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            html = item.get_content().decode("utf-8", errors="ignore")
            text = BeautifulSoup(html, "html.parser").get_text("\n")
            text = _clean_text(text)
            if text.strip():
                texts.append(text)
        combined = "\n\n".join(texts)
        chapters = [Chapter(title="未识别章节", text=combined, page_start=None, source_format="epub")]
    return chapters


# ---------- PDF ----------

def extract_pdf(path: str, max_pages_per_chunk: int = 15) -> ChapterList:
    import fitz
    import pymupdf4llm

    doc = fitz.open(path)
    toc = doc.get_toc()  # [[level, title, page], ...]

    # 全书按页取文本（fitz 原生，快；供 per-page 定位用）。缓存整本一次，各章切片复用。
    all_page_texts = [doc[i].get_text() or "" for i in range(doc.page_count)]

    def slice_pages(start_page: int, end_page: int):
        """返回该章的 [(1-indexed 物理页, 页文本)]，页文本已清洗。"""
        out = []
        for pi in range(start_page, min(end_page, doc.page_count)):
            t = _clean_text(all_page_texts[pi])
            if t:
                out.append((pi + 1, t))
        return out

    chapters = []
    if toc:
        # 按大纲切分：每个顶层（level==1）条目算一章，覆盖到下一同级/上级条目之前
        entries = [(lvl, title, page) for lvl, title, page in toc if page and page >= 1]
        for i, (lvl, title, page) in enumerate(entries):
            if lvl > 1:
                continue  # 只取顶层作为章节
            start_page = page - 1  # 0-indexed
            if i + 1 < len(entries):
                next_entry = entries[i + 1]
                end_page = (next_entry[2] - 1) if next_entry[0] <= 1 else min(start_page + max_pages_per_chunk, doc.page_count)
            else:
                end_page = doc.page_count
            end_page = min(end_page, start_page + max(max_pages_per_chunk, end_page - start_page))
            end_page = min(end_page, doc.page_count)
            if end_page <= start_page:
                end_page = min(start_page + max_pages_per_chunk, doc.page_count)
            text = _extract_pdf_range(doc, pymupdf4llm, start_page, end_page)
            if text.strip():
                chapters.append(Chapter(title=str(title).strip(), text=text, page_start=page,
                                        pages=slice_pages(start_page, end_page), source_format="pdf"))
    else:
        # 无大纲：按固定页数切块
        total = doc.page_count
        for start in range(0, total, max_pages_per_chunk):
            end = min(start + max_pages_per_chunk, total)
            text = _extract_pdf_range(doc, pymupdf4llm, start, end)
            n = start // max_pages_per_chunk + 1
            chapters.append(Chapter(title=f"未识别章节 {n}", text=text, page_start=start + 1,
                                    pages=slice_pages(start, end), source_format="pdf"))

    doc.close()
    if not chapters:
        raise RuntimeError("PDF 未提取到任何文字，可能是纯扫描版（需要 OCR，本期范围外）")
    return chapters


def _extract_pdf_range(doc, pymupdf4llm, start_page: int, end_page: int) -> str:
    try:
        md = pymupdf4llm.to_markdown(doc, pages=range(start_page, end_page))
        return _clean_text(md)
    except Exception:
        # pymupdf4llm 失败时退化为 fitz 直接取文本
        pages = [doc[p] for p in range(start_page, end_page) if p < doc.page_count]
        text = "\n\n".join(page.get_text() for page in pages)
        return _clean_text(text)


# ---------- 通用文本清洗 ----------

def _clean_text(text: str) -> str:
    # 去掉连续多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract(path: str, max_pages_per_chunk: int = 15) -> ChapterList:
    path_lower = path.lower()
    if path_lower.endswith(".epub"):
        return extract_epub(path)
    elif path_lower.endswith(".pdf"):
        return extract_pdf(path, max_pages_per_chunk=max_pages_per_chunk)
    else:
        raise ValueError(f"不支持的文件格式: {path}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m src.extract <file.pdf|file.epub>")
        sys.exit(1)
    chapters = extract(sys.argv[1])
    print(f"extracted {len(chapters)} chapters from {sys.argv[1]}")
    for c in chapters:
        print(f"- {c.title} | {len(c.text)} chars | page_start={c.page_start}")
