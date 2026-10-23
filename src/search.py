"""搜索模块：输入书名+作者，输出候选书列表。是 zlib_client.search 的薄封装，
方便 pipeline 单独调用、单独测试。"""
import sys
from src.zlib_client import ZlibClient, ZlibError


def search_books(title: str, author: str = None, client: ZlibClient = None, limit: int = 10) -> list:
    client = client or ZlibClient()
    return client.search(title, author, limit=limit)


def print_results(books: list):
    if not books:
        print("没有找到结果。")
        return
    print(f"共找到 {len(books)} 本书：\n")
    for i, b in enumerate(books):
        size_mb = f"{b.size / 1024 / 1024:.1f}MB" if b.size else "大小未知"
        print(f"{i + 1}. {b.title}")
        print(f"   作者: {b.author or '未知'} | 年份: {b.year or '未知'} | 格式: {','.join(b.formats) or '未知'} | {size_mb}")
        if b.isbn:
            print(f"   ISBN: {b.isbn}")
        if b.source_url:
            print(f"   详情页: {b.source_url}")
        print(f"   zlib id: {b.zlib_id}")
        print()


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("usage: python -m src.search <书名> [作者]")
        sys.exit(1)
    title = sys.argv[1]
    author = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        books = search_books(title, author)
    except ZlibError as e:
        print(f"搜索失败：{e}")
        sys.exit(1)
    print_results(books)
