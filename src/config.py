"""读取项目根目录下的 book-config 文件，把 KEY=VALUE 写入 os.environ。

规则：
- 只设置 os.environ 里还没有的键（已设置的环境变量优先，便于临时覆盖）。
- 忽略空行和以 # 开头的注释行。
- 文件不存在时静默跳过（允许纯靠环境变量运行）。
- 强制 UTF-8 读取，与编码假设一致（避免 Windows 代码页问题）。
"""
import os

_CONFIG_NAME = "book-config"


def load_config(root: str = None) -> bool:
    """加载 book-config，返回是否找到并读取了文件。"""
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, _CONFIG_NAME)
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    return True
