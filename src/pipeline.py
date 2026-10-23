"""主入口：串起 search -> download -> extract -> distill。

用法：
  python -m src.pipeline --book "Deep Work" --author "Cal Newport"
  python -m src.pipeline --book "深度工作" --format pdf --skip-download --raw-file raw/<md5>.pdf
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

from src.zlib_client import ZlibClient
from src.download import RAW_DIR, download_book, pick_format
from src.extract import extract
from src.distill import Distiller
from src.search import print_results, search_books
from src.render import load_book, render_book_html

logger = logging.getLogger(__name__)

NOTES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "notes")


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return re.sub(r"\s+", " ", name)[:80] or "未命名"


def run_pipeline(book_title: str, author: str, file_format: str = "epub",
                 skip_download: bool = False, raw_file: str = None,
                 model: str = None, provider: str = None) -> str:
    # 返回生成的书级 README.md 路径。
    # 1. 搜索
    if skip_download and raw_file:
        raw_path = raw_file if os.path.isabs(raw_file) else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), raw_file)
        source_url = None
        print(f"[1/4] 跳过搜索/下载，直接使用: {raw_path}")
    else:
        print(f"[1/4] 搜索: {book_title} {author or ''}")
        client = ZlibClient()
        books = search_books(book_title, author, client)
        if not books:
            raise RuntimeError("没搜索到这本书，换个书名或作者试试")
        print_results(books)
        if len(books) == 1:
            chosen = books[0]
        else:
            idx = int(input(f"请输入要下载的书的序号 (1-{len(books)}): ")) - 1
            chosen = books[idx]
        print(f"选定: {chosen.title} | {chosen.author} | 格式={chosen.formats}")
        chosen_format = pick_format(chosen, file_format)
        print(f"[2/4] 下载 ({chosen_format}) ...")
        raw_path = download_book(chosen, chosen_format)
        source_url = chosen.source_url
        # 保留元信息，稍后写进书级 README
        _meta = {"title": chosen.title, "author": chosen.author, "isbn": chosen.isbn,
                 "source_url": chosen.source_url, "download_time": datetime.now().isoformat(timespec="seconds")}
    if skip_download and raw_file:
        _meta = {"title": book_title, "author": author, "isbn": None, "source_url": None,
                 "download_time": "(离线模式，未下载)"}

    # 2. 提取
    print(f"[3/4] 提取章节 ...")
    chapters = extract(raw_path)
    print(f"共 {len(chapters)} 章：")
    for c in chapters:
        print(f"  - {c.title} | {len(c.text)} 字 | 页码起: {c.page_start}")

    # 3. 蒸馏
    distiller = Distiller(provider=provider, model=model)
    print(f"[4/4] 调用 LLM 蒸馏 (provider={distiller.provider}, model={distiller.model}) ...")
    chapter_entries = []
    for i, chapter in enumerate(chapters, 1):
        print(f"  蒸馏第 {i}/{len(chapters)} 章: {chapter.title}")
        entries = distiller.distill_chapter(_meta["title"], _meta["author"], chapter)
        chapter_entries.append((chapter, entries))
        print(f"    -> {len(entries)} 条")

    summary = distiller.distill_book_summary(_meta["title"], _meta["author"], chapter_entries)

    # 4. 写出文件
    book_dir_name = _safe_filename(_meta["title"])
    book_dir = os.path.join(NOTES_DIR, book_dir_name)
    os.makedirs(book_dir, exist_ok=True)

    # 把原文文件复制进书目录，让笔记文件夹自包含，且"跳转到原文"按钮能定位到它
    raw_copy = None
    if os.path.exists(raw_path):
        ext = os.path.splitext(raw_path)[1].lower()
        if ext in (".pdf", ".epub"):
            raw_copy = os.path.join(book_dir, book_dir_name + ext)
            try:
                import shutil
                shutil.copyfile(raw_path, raw_copy)
            except Exception as e:
                logger.warning("复制原文文件到书目录失败（不影响笔记）: %s", e)
                raw_copy = None

    chapter_files = []
    for i, (chapter, entries) in enumerate(chapter_entries, 1):
        chap_dir_name = f"{i:02d}-{_safe_filename(chapter.title)}"
        lines = [f"# {i}. {chapter.title}", ""]
        page_note = f"（原文页码：第 {chapter.page_start} 页起）" if chapter.page_start else ""
        lines.append(f"本章共提炼 {len(entries)} 条。{page_note}".strip())
        lines.append("")
        for j, e in enumerate(entries, 1):
            lines.append(f"### {j}. {e.get('核心论点', '(无标题)')}")
            tags = " ".join(x for x in [e.get('类型', ''), e.get('强度', '')] if x)
            if tags:
                lines.append(f"- 标签：{tags}")
            lines.append(f"- 关键证据：{e.get('关键证据', '')}")
            lines.append(f"- 反例或争议：{e.get('反例或争议', '')}")
            lines.append(f"- 适用边界：{e.get('适用边界', '')}")
            lines.append(f"- 一句话总结：{e.get('一句话总结', '')}")
            if e.get("source_page"):
                lines.append(f"- 原文页码：第 {e['source_page']} 页")
            lines.append("")
        page_ref = f"第{chapter.page_start}页起" if chapter.page_start else "页码未识别"
        lines.append(f"<!-- 来源: {chapter.title}, {page_ref} -->")
        chap_path = os.path.join(book_dir, f"{chap_dir_name}.md")
        with open(chap_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        chapter_files.append((f"{i}. {chapter.title}", chap_dir_name))

    readme_lines = [
        f"# {_meta['title']}",
        "",
        f"作者：{_meta['author'] or '未知'}",
    ]
    if _meta.get("isbn"):
        readme_lines.append(f"ISBN：{_meta['isbn']}")
    if _meta.get("source_url"):
        readme_lines.append(f"来源：{_meta['source_url']}")
    readme_lines.append(f"下载时间：{_meta.get('download_time')}")
    readme_lines.append(f"蒸馏时间：{datetime.now().isoformat(timespec='seconds')}")
    readme_lines.append(f"蒸馏模型：{distiller.model}")
    readme_lines.append("")
    readme_lines.append("## 全书概览")
    readme_lines.append("")
    readme_lines.append(summary.get("overview", ""))
    readme_lines.append("")
    if summary.get("recurring_core") and summary["recurring_core"] != "无":
        readme_lines.append("## 全书反复强调的核心")
        readme_lines.append("")
        readme_lines.append(summary["recurring_core"])
        readme_lines.append("")
    readme_lines.append("## 全书分组导览")
    readme_lines.append("")
    for g in summary.get("topic_groups", []):
        description = g.get("description", "").strip()
        chapters = g.get("chapters", [])
        chapters_str = "、".join(chapters)
        # 双保险：若 description 里已经包含了 chapters 中的章节名，就不再重复拼接
        already_lists_chapters = any(ch in description for ch in chapters)
        if chapters and not already_lists_chapters:
            readme_lines.append(f"**{g.get('topic', '')}**：{description}（涉及：{chapters_str}）")
        else:
            readme_lines.append(f"**{g.get('topic', '')}**：{description}")
        readme_lines.append("")
    readme_lines.append("## 章节目录")
    readme_lines.append("")
    for title, fname in chapter_files:
        readme_lines.append(f"- [{title}](./{fname}.md)")
    readme_lines.append("")
    readme_path = os.path.join(book_dir, "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write("\n".join(readme_lines))

    # 渲染成单文件交互式 HTML（双击即可打开）
    html_path = None
    try:
        book_data = load_book(book_dir)
        # 与目录名一致，用安全文件名（去逗号/非法字符），避免出现 "富爸爸,穷爸爸.html" 这种带逗号的怪名字
        html_path = os.path.join(book_dir, book_dir_name + ".html")
        render_book_html(book_data, html_path)
    except Exception as e:
        logger.warning("HTML 渲染失败（不影响 md 输出）: %s", e)

    print(f"\n完成！输出目录：{book_dir}")
    print(f"书级概览：{readme_path}")
    if raw_copy:
        print(f"原文文件：{raw_copy}（供笔记里“跳转到原文”使用）")
    if html_path:
        print(f"交互式网页：{html_path}（双击用浏览器打开）")
    return readme_path


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # 加载 book-config（已设置的环境变量优先，不覆盖）
    from src.config import load_config
    if load_config():
        logger.info("已加载 book-config 配置")

    parser = argparse.ArgumentParser(description="书籍蒸馏 pipeline")
    parser.add_argument("--book", required=True, help="书名")
    parser.add_argument("--author", default=None, help="作者")
    parser.add_argument("--format", default="epub", help="偏好下载格式 (epub/pdf/...)，默认 epub")
    parser.add_argument("--provider", default=None,
                        help="LLM 提供商: anthropic(默认) 或 openai(兼容 OpenAI/DeepSeek/GLM/Ollama 等)")
    parser.add_argument("--model", default=None, help="覆盖蒸馏模型名")
    parser.add_argument("--skip-download", action="store_true", help="跳过搜索/下载，直接用 --raw-file")
    parser.add_argument("--raw-file", default=None, help="配合 --skip-download，raw/ 下已有的文件路径")
    args = parser.parse_args()

    if args.skip_download and not args.raw_file:
        parser.error("--skip-download 需要同时指定 --raw-file")

    run_pipeline(
        book_title=args.book,
        author=args.author,
        file_format=args.format,
        skip_download=args.skip_download,
        raw_file=args.raw_file,
        model=args.model,
        provider=args.provider,
    )


if __name__ == "__main__":
    main()
