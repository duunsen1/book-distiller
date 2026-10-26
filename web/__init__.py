"""Web 壳：把 pipeline 包成 HTTP API + 单文件前端。

演示目标（形态验证）：
- 上传文件 -> 蒸馏（B）
- zlib 搜索下载 -> 蒸馏（C，登录态用服务器本地 ~/.config/zlib/）
- LLM key 由用户在网页填写，只存浏览器 localStorage，不入库
- 每本书一个独立页面 /book/<slug>

运行（项目根目录）：
  .venv/Scripts/python -m uvicorn web.app:app --port 8000 --reload
"""
