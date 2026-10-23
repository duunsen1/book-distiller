"""按页提取 + 定位工具（为"论据 -> 具体页码"功能服务）。

两种取页文本的方式：
- fast（默认）：fitz 原生 text，快但不处理图片/复杂版式（很多书够用了）
- ocr：pymupdf4llm，慢（整本书只 OCR 一次，结果缓存到盘上），版式好

locate_char 把"按页拼接串里的字符位置"映射回"具体页码"。
"""
import hashlib
import json
import os
import re

def _norm(s: str) -> str:
    """去掉所有空白，便于稳健匹配（页码映射不受空白差异影响）。"""
    return re.sub(r"\s+", "", s or "")


def _sha(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path(pdf_path: str, mode: str) -> str:
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_cache")
    os.makedirs(cache_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    return os.path.join(cache_dir, f"{base}__{mode}__{_sha(pdf_path)[:12]}.json")


def _extract_pages_fast(pdf_path: str) -> list:
    import pymupdf  # fitz
    doc = pymupdf.open(pdf_path)
    pages = [doc[i].get_text() or "" for i in range(doc.page_count)]
    doc.close()
    return pages


def _extract_pages_ocr(pdf_path: str) -> list:
    import pymupdf
    import pymupdf4llm
    doc = pymupdf.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        pages.append(pymupdf4llm.to_markdown(doc, pages=range(i, i + 1)) or "")
    doc.close()
    return pages


def get_pages(pdf_path: str, mode: str = "fast", use_cache: bool = True) -> list:
    """返回 [每页文本]。ocr 模式带磁盘缓存，避免重复 OCR。"""
    cp = _cache_path(pdf_path, mode)
    if use_cache and os.path.exists(cp):
        with open(cp, "r", encoding="utf-8") as f:
            return json.load(f)
    pages = _extract_pages_fast(pdf_path) if mode == "fast" else _extract_pages_ocr(pdf_path)
    if use_cache:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(pages, f, ensure_ascii=False)
    return pages


def page_boundaries(pages: list) -> list:
    """返回每页在"归一化拼接串"里的结束偏移，用于字符位置 -> 页码。"""
    starts = []
    pos = 0
    for p in pages:
        pos += len(_norm(p))
        starts.append(pos)
    return starts  # starts[i] = 第 i 页(0-indexed)结束处的字符位置


def locate_char(pages: list, char_idx_in_norm: int) -> int:
    """把归一化拼接串里的字符位置映射成 0-indexed 页码。"""
    for i, end in enumerate(page_boundaries(pages)):
        if char_idx_in_norm < end:
            return i
    return len(pages) - 1


def find_substring_page(pdf_path: str, phrase: str, mode: str = "fast") -> dict:
    """在整本书里定位一个短语出现在哪一页（用于验证映射质量）。"""
    pages = get_pages(pdf_path, mode=mode)
    joined = _norm("\n".join(pages))
    idx = joined.find(_norm(phrase))
    if idx < 0:
        return {"phrase": phrase, "found": False}
    p = locate_char(pages, idx)
    return {"phrase": phrase, "found": True, "page_0indexed": p, "page_1indexed": p + 1}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python -m src.paged <file.pdf> <phrase> [fast|ocr]")
        sys.exit(1)
    print(find_substring_page(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "fast"))
