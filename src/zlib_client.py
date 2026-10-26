"""zlib（https://github.com/heartleo/zlib）CLI 封装：搜索书籍 + 下载。

原 Anna's Archive 在线客户端已下线，改走 Z-Library 的 zlib CLI：
- 搜索：zlib search "<书名> <作者>" --json
- 下载：zlib download "id:hash" --dir <目录> --json

CLI 输出的是 UTF-8 字节（Go 程序不经过 Windows 控制台转码），所以这里用
decode("utf-8") 而不是 decode("mbcs")。JSON 输出（--json）里不含 ANSI 颜色码，
解析用正则兜底剥掉万一混入的转义序列。

前置条件（一次性，在真实终端里做）：
1. 安装 CLI：winget install --id heartleo.zlib --exact
2. zlib doctor     # 找一个可达域名
3. zlib login      # 登录 Z-Library 账号（CLI 会保存会话；
                    # 也可把浏览器登录后的 Cookie 导入 CLI）
登录状态由 zlib 自己保存在 ~/.config/zlib/，本项目不碰凭证。
"""
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass
class BookMatch:
    # 注意：与旧 AA 版本的 BookMatch 不同。这里没有 md5（zlib 下载不返回 md5），
    # 多了 zlib_id（zlib download 的入参，形如 "id:hash"）。
    # pipeline 只把原始文件复制到书目录（不重命名为 md5），所以这个字段只用于日志。
    zlib_id: str
    title: str
    author: str
    year: Optional[str] = None
    publisher: Optional[str] = None
    language: Optional[str] = None
    size: Optional[int] = None  # 字节
    formats: list = field(default_factory=list)  # Z-Library 一本记录对应一个格式，通常长度为 1
    isbn: Optional[str] = None
    source_url: Optional[str] = None  # Z-Library 详情页链接，便于人工核对

    def to_dict(self) -> dict:
        return {
            "zlib_id": self.zlib_id,
            "title": self.title,
            "author": self.author,
            "year": self.year,
            "publisher": self.publisher,
            "language": self.language,
            "size": self.size,
            "formats": self.formats,
            "isbn": self.isbn,
            "source_url": self.source_url,
        }


class ZlibError(Exception):
    pass


def _parse_size(size: str) -> Optional[int]:
    """'12.3 MB' -> 12902918 字节；解析不了返回 None。"""
    if not size:
        return None
    match = re.match(r"^\s*([\d.]+)\s*([KMGT]B)?", size.strip(), re.I)
    if not match:
        return None
    value = float(match.group(1))
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    unit = (match.group(2) or "B").upper()
    return int(value * mult.get(unit, 1))


class ZlibClient:
    def __init__(self, binary: str = None, timeout: int = 30, home: Optional[str] = None):
        self._binary = binary or os.environ.get("ZLIB_BIN") or "zlib"
        self._timeout = timeout
        # home：按用户隔离 zlib 凭证。设为某目录后，zlib 子进程以该目录为 HOME，
        # 就读/写 <home>/.config/zlib/ 下的 session.json 等，实现多用户互不干扰。
        self._home = home

    def _subprocess_env(self) -> Optional[dict]:
        if not self._home:
            return None
        env = os.environ.copy()
        env["HOME"] = self._home
        return env

    # ---------- 底层 ----------

    def _check_binary(self):
        if shutil.which(self._binary) is None:
            raise ZlibError(
                f"找不到 zlib CLI（'{self._binary}'）。请先安装："
                f"winget install --id heartleo.zlib --exact\n"
                f"或下载 release 二进制加入 PATH（也可用环境变量 ZLIB_BIN 指定路径）。"
                f"安装后还需运行 `zlib doctor` 找可达域名、`zlib login` 登录账号。"
            )

    def _run_json(self, args: list, timeout: int = None) -> dict:
        """跑 zlib 子命令并解析 stdout 的 JSON（进度信息在 stderr，不影响解析）。"""
        self._check_binary()
        logger.info("zlib %s", " ".join(args))
        try:
            proc = subprocess.run(
                [self._binary] + args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self._timeout,
                env=self._subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            raise ZlibError(f"zlib 命令超时（{timeout or self._timeout}s）: zlib {' '.join(args)}")
        out = _strip_ansi(proc.stdout.decode("utf-8", errors="replace"))
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            hint = ""
            if "login" in err.lower() or "cookie" in err.lower() or "unauthorized" in err.lower():
                hint = "\n提示：看起来未登录或会话过期，请先在终端运行 `zlib login`。"
            raise ZlibError(f"zlib {' '.join(args)} 失败（exit {proc.returncode}）：\n{err}{hint}")
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end <= start:
            raise ZlibError(f"zlib 输出里没有 JSON（前 300 字符）：{out[:300]!r}")
        try:
            return json.loads(out[start:end + 1])
        except json.JSONDecodeError as e:
            raise ZlibError(f"zlib 输出 JSON 解析失败：{e}\n{out[:300]!r}")

    # ---------- 搜索 ----------

    def search(self, title: str, author: Optional[str] = None, limit: int = 10) -> list:
        query = title if not author else f"{title} {author}"
        data = self._run_json(["search", query, "--json", "--count", str(limit)])
        books = []
        for r in data.get("books", []):
            if not r.get("id"):
                continue
            ext = (r.get("extension") or "").lower()
            books.append(BookMatch(
                zlib_id=r["id"],
                title=r.get("name", ""),
                author=", ".join(r.get("authors") or []) or "",
                year=r.get("year") or None,
                publisher=r.get("publisher") or None,
                language=r.get("language") or None,
                size=_parse_size(r.get("size", "")),
                formats=[ext] if ext else [],
                isbn=r.get("isbn13") or r.get("isbn10") or r.get("isbn") or None,
                source_url=r.get("url") or None,
            ))
        return books

    # ---------- 下载 ----------

    def download(self, book: BookMatch, dest_dir: str, timeout: int = 600) -> str:
        """zlib download <id:hash> --dir <dest_dir>，返回落盘文件路径。"""
        os.makedirs(dest_dir, exist_ok=True)
        data = self._run_json(
            ["download", book.zlib_id, "--dir", dest_dir, "--json"],
            timeout=timeout,
        )
        path = data.get("path")
        if not path or not os.path.exists(path):
            raise ZlibError(f"zlib 报告下载成功但找不到文件：{path!r}")
        return path


# ---------- 用户凭证（多用户隔离） ----------

def user_zlib_dir(home: str) -> str:
    """某用户私有 HOME 下 zlib 的配置目录。"""
    return os.path.join(home, ".config", "zlib")


def write_user_session(home: str, cookies: dict, domain: str = None, mode: str = "eapi") -> str:
    """把用户的 Z-Library 凭证写进 <home>/.config/zlib/session.json。

    cookies 至少含 remix_userid / remix_userkey（Z-Library 网页登录后
    浏览器 DevTools → Application → Cookies 里可取）。返回写入的文件路径。
    """
    d = user_zlib_dir(home)
    os.makedirs(d, exist_ok=True)
    session = {
        "cookies": cookies,
        "domain": domain or "https://z-lib.gd",
        "mode": mode,
    }
    path = os.path.join(d, "session.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)
    return path


def user_has_session(home: str) -> bool:
    """该用户私有 HOME 里是否已配置 zlib 凭证。"""
    return os.path.exists(os.path.join(user_zlib_dir(home), "session.json"))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    title = sys.argv[1] if len(sys.argv) > 1 else "Deep Work"
    author = sys.argv[2] if len(sys.argv) > 2 else None
    client = ZlibClient()
    books = client.search(title, author)
    print(f"found {len(books)} results for {title!r} {author!r}")
    for i, b in enumerate(books):
        print(f"{i}: {b.title} | {b.author} | {b.year} | {b.formats} | {b.size} | {b.zlib_id}")
