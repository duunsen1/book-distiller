"""下载模块：根据 BookMatch（zlib id:hash + 目标格式）下载到 raw/ 目录。"""
import logging
import os
import sys

from src.zlib_client import BookMatch, ZlibClient

logger = logging.getLogger(__name__)

RAW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "raw")


def download_book(book: BookMatch, file_format: str = "epub", raw_dir: str = RAW_DIR) -> str:
    os.makedirs(raw_dir, exist_ok=True)
    client = ZlibClient()
    logger.info("downloading %s (%s) -> %s", book.title, file_format, raw_dir)
    return client.download(book, raw_dir)


def pick_format(book: BookMatch, preferred: str = "epub") -> str:
    """优先选用户偏好格式；Z-Library 一条搜索结果通常只对应一个格式，
    没有偏好格式时就用它唯一有的格式，并提示用户（蒸馏只支持 pdf/epub/txt）。"""
    formats = [f.lower() for f in book.formats]
    if preferred in formats:
        return preferred
    if len(formats) == 1:
        logger.warning("该书只有 %s 格式（非偏好格式 %s），继续下载", formats[0], preferred)
        return formats[0]
    raise RuntimeError(f"该书没有可用格式，仅有: {book.formats}")


if __name__ == "__main__":
    import logging as lg
    lg.basicConfig(level=lg.INFO)
    print("建议直接运行 src.pipeline，它会串起搜索和下载")
