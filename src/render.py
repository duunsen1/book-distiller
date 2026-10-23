"""把某本书的 notes/<书名>/*.md 渲染成一个单文件自包含 HTML。

风格参考 HowToLiveBetter 的检索页：左侧章节目录 + 主区条目卡片（可展开）
+ 顶部搜索框 + 明暗模式切换。字段适配书籍笔记（核心论点/关键证据/反例或争议/
适用边界/一句话总结），不硬套"性价比"框架。

数据内联进 HTML（const BOOK_DATA），双击即可在浏览器打开，无需服务器。

用法：
  python -m src.render notes/<书名> [-o out.html]
  python -m src.render "notes/富爸爸,穷爸爸"
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

logger = logging.getLogger(__name__)

NOTES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "notes")

# 卡片正文的字段表格行 -> 展示标签（顺序即展示顺序）
FIELDS = [
    ("关键证据", "关键证据"),
    ("反例或争议", "反例或争议"),
    ("适用边界", "适用边界"),
]


def parse_chapter_file(path: str) -> dict:
    """解析单个章节 md，返回 {title, page, entries:[{title, 类型, 强度, 关键证据, ...}]}。"""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    chapter_title = ""
    entries = []
    cur = None
    page = None
    # 每条初始字段（含标签 类型/强度 + 正文四字段 + 具体页码）
    base = {
        "title": "", "类型": "", "强度": "",
        "关键证据": "", "反例或争议": "", "适用边界": "", "一句话总结": "",
        "source_page": None,
    }

    for raw in lines:
        line = raw.rstrip()
        m = re.match(r"^# \d+\.\s*(.+)$", line)
        if m:
            chapter_title = m.group(1).strip()
            continue
        m = re.match(r"^### \d+\.\s*(.+)$", line)
        if m:
            cur = dict(base)
            cur["title"] = m.group(1).strip()
            entries.append(cur)
            continue
        # 页码/来源注释可能在任何条目之前出现（如章节首行），先于 cur 判断处理
        m = re.search(r"原文页码：第 (\d+) 页起", line)
        if m:
            page = int(m.group(1))
            continue
        m = re.search(r"<!-- 来源: (.*?), (.*?) -->", line)
        if m:
            page = page or (int(m.group(2).split("页")[0]) if m.group(2).isdigit() else page)
            continue
        if cur is None:
            continue
        m = re.match(r"^- (类型|强度)：(.*)$", line)
        if m:
            # 标签行形如 "- 标签：核心观点 强"，按字段名取对应部分
            cur[m.group(1)] = m.group(2).strip()
            continue
        m = re.match(r"^- (关键证据|反例或争议|适用边界|一句话总结)：(.*)$", line)
        if m:
            cur[m.group(1)] = m.group(2).strip()
            continue
        # 每条论据的具体页码（PDF 精确到页）
        m = re.match(r"^- 原文页码：第 (\d+) 页", line)
        if m:
            cur["source_page"] = int(m.group(1))
            continue

    return {"title": chapter_title, "page": page, "entries": entries}


def load_book(book_dir: str) -> dict:
    """读整个 notes/<书名>/ 目录，返回 {meta, chapters:[...]}。"""
    files = sorted(f for f in os.listdir(book_dir) if f.endswith(".md") and f != "README.md")
    chapters = []
    for fn in files:
        chap = parse_chapter_file(os.path.join(book_dir, fn))
        chap["file"] = fn
        chapters.append(chap)
    chapters.sort(key=lambda c: c["file"])

    # 从书级 README 提取 meta
    meta = {"title": os.path.basename(book_dir), "author": "", "overview": "", "core": "",
            "source": None, "toc": []}
    readme = os.path.join(book_dir, "README.md")
    if os.path.exists(readme):
        with open(readme, "r", encoding="utf-8") as f:
            rtxt = f.read()
        m = re.search(r"^作者：(.+)$", rtxt, re.M)
        if m:
            meta["author"] = m.group(1).strip()
        m = re.search(r"^## 全书概览\n+([\s\S]*?)(?=\n## )", rtxt, re.M)
        if m:
            meta["overview"] = m.group(1).strip()
        m = re.search(r"^## 全书反复强调的核心\n+([\s\S]*?)(?=\n## )", rtxt, re.M)
        if m:
            meta["core"] = m.group(1).strip()
    meta["generated_at"] = datetime.now().isoformat(timespec="seconds")

    # 定位书目录里随附的原文文件（<书名>.pdf/.epub 或任意一个），用于"跳转到原文"
    srcs = [f for f in os.listdir(book_dir) if f.lower().endswith((".pdf", ".epub"))]
    if srcs:
        # 优先和书名同名的，其次按名字排序取第一个
        pref = [f for f in srcs if f.lower().endswith(".pdf")] or \
               [f for f in srcs if f.lower().endswith(".epub")] or srcs
        meta["source"] = pref[0]

    # 原文目录（标题 + 起始页），供 EPUB 按章节跳转；PDF 页码已由每章 page 提供
    for c in chapters:
        meta["toc"].append({"title": c["title"], "page": c["page"]})

    return {"meta": meta, "chapters": chapters}


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@ · 结构化笔记</title>
<script>(function(){try{var s=localStorage.getItem('theme');var d=s?s==='dark':matchMedia('(prefers-color-scheme: dark)').matches;if(d)document.documentElement.classList.add('dark')}catch(e){}})();</script>
<style>
:root{
  --bg:#ffffff; --bg-alt:#f6f6f7; --bg-elv:#ffffff;
  --divider:#e2e2e3; --t1:rgba(60,60,67,1); --t2:rgba(60,60,67,.78); --t3:rgba(60,60,67,.56);
  --brand-1:#3451b2; --brand-2:#3a5ccc; --brand-soft:rgba(100,108,255,.14);
  --green-1:#18794e; --green-soft:rgba(16,185,129,.14);
  --red-1:#b8272c; --red-soft:rgba(244,63,94,.14);
  --shadow:0 1px 2px rgba(0,0,0,.04),0 1px 2px rgba(0,0,0,.06);
  --font:"Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  --nav-h:64px; --side-w:288px;
  color-scheme:light;
}
html.dark{
  --bg:#1b1b1f; --bg-alt:#161618; --bg-elv:#202127;
  --divider:#2e3237; --t1:rgba(255,255,245,.86); --t2:rgba(235,235,245,.6); --t3:rgba(235,235,245,.38);
  --brand-1:#a8b1ff; --brand-2:#5c73e7; --brand-soft:rgba(100,108,255,.16);
  --green-1:#3dd68c; --green-soft:rgba(16,185,129,.16);
  --red-1:#f66f81; --red-soft:rgba(244,63,94,.16);
  --shadow:0 1px 2px rgba(0,0,0,.3);
  color-scheme:dark;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:calc(var(--nav-h) + 16px)}
body{margin:0;background:var(--bg);color:var(--t1);font:14px/1.7 var(--font);-webkit-font-smoothing:antialiased}
a{color:var(--brand-1);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
button{font:inherit;color:inherit;background:none;border:0;padding:0;cursor:pointer}
mark{background:rgba(234,179,8,.4);color:inherit;border-radius:2px;padding:0 1px}

/* 顶部导航 */
nav{position:sticky;top:0;z-index:20;height:var(--nav-h);display:flex;align-items:center;gap:12px;
  padding:0 20px;background:var(--bg);border-bottom:1px solid var(--divider)}
nav .logo{font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40vw}
nav .spacer{flex:1}
.search{position:relative;flex:1;max-width:420px}
.search input{width:100%;padding:8px 12px;border:1px solid var(--divider);border-radius:8px;
  background:var(--bg-alt);color:var(--t1);font:inherit;outline:none}
.search input:focus{border-color:var(--brand-1);box-shadow:0 0 0 3px var(--brand-soft)}
.icon-btn{width:36px;height:36px;display:flex;align-items:center;justify-content:center;border-radius:8px;color:var(--t2)}
.icon-btn:hover{background:var(--brand-soft);color:var(--brand-1)}

/* 布局 */
.wrap{display:flex;align-items:flex-start}
aside{position:sticky;top:var(--nav-h);width:var(--side-w);flex-shrink:0;height:calc(100vh - var(--nav-h));
  overflow-y:auto;border-right:1px solid var(--divider);padding:16px 8px 40px}
main{flex:1;min-width:0;padding:24px clamp(16px,4vw,56px) 80px;max-width:900px}

/* 侧边栏目录 */
.side-title{font-size:12px;color:var(--t3);font-weight:600;text-transform:uppercase;letter-spacing:.04em;padding:0 12px 8px}
.side a{display:block;padding:7px 12px;border-radius:8px;color:var(--t2);font-size:13.5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.side a:hover{background:var(--brand-soft);color:var(--brand-1);text-decoration:none}
.side a.active{background:var(--brand-soft);color:var(--brand-1);font-weight:600}
.side a .cnt{float:right;color:var(--t3);font-size:12px;font-variant-numeric:tabular-nums}

/* 书概览 */
.overview{background:var(--bg-elv);border:1px solid var(--divider);border-radius:14px;padding:20px 22px;box-shadow:var(--shadow);margin-bottom:28px}
.overview h1{margin:0 0 4px;font-size:22px;line-height:1.3}
.overview .author{color:var(--t3);font-size:13px;margin-bottom:14px}
.overview .ov{color:var(--t2)}
.overview .core{margin-top:14px;padding:12px 14px;background:var(--green-soft);border-radius:10px;color:var(--t1);font-size:13.5px}
.overview .core b{color:var(--green-1)}

/* 章节 */
section.chapter{margin-bottom:40px;scroll-margin-top:calc(var(--nav-h) + 16px)}
.chapter-head{position:sticky;top:var(--nav-h);z-index:10;background:var(--bg);
  padding:10px 0 12px;border-bottom:2px solid var(--brand-1);margin-bottom:16px}
.chapter-head h2{margin:0;font-size:17px;display:flex;align-items:baseline;gap:10px}
.chapter-head .pg{color:var(--t3);font-size:12.5px;font-weight:400}

/* 条目卡片（仿 HowToLiveBetter：标题+彩色标签 / 引用块 / 字段表格 / 来源折叠） */
.card{border:1px solid var(--divider);border-radius:12px;background:var(--bg-elv);margin-bottom:12px;overflow:hidden;transition:box-shadow .2s}
.card:hover{box-shadow:var(--shadow)}
.card-head{display:flex;gap:12px;align-items:flex-start;padding:14px 16px 12px;cursor:pointer}
.card-head .num{color:var(--t3);font-variant-numeric:tabular-nums;font-size:13px;min-width:22px;padding-top:3px}
.card-head .ttl{font-weight:600;font-size:15px;line-height:1.5;flex:1}
.card-head .chev{color:var(--t3);transition:transform .2s;flex-shrink:0;padding-top:3px}
.card.open .card-head .chev{transform:rotate(90deg)}
.card-body{display:none;padding:0 16px 16px 50px}
.card.open .card-body{display:block}

/* 标题下的标签行 */
.tags{display:flex;flex-wrap:wrap;gap:6px;padding:0 16px 12px 50px}
.tag{font-size:11.5px;font-weight:600;padding:2px 9px;border-radius:20px;line-height:1.6;white-space:nowrap}
.tag-type-核心观点{background:var(--brand-soft);color:var(--brand-1)}
.tag-type-方法建议{background:var(--green-soft);color:var(--green-1)}
.tag-type-案例故事{background:rgba(245,158,11,.16);color:#b45309}
.tag-type-数据事实{background:rgba(6,182,212,.16);color:#0e7490}
.tag-type-背景知识{background:var(--bg-alt);color:var(--t2);border:1px solid var(--divider)}
html.dark .tag-type-案例故事{color:#fbbf24}
html.dark .tag-type-数据事实{color:#22d3ee}
.tag-strength-强{background:var(--green-soft);color:var(--green-1)}
.tag-strength-中{background:rgba(245,158,11,.16);color:#b45309}
.tag-strength-存疑{background:var(--red-soft);color:var(--red-1)}
html.dark .tag-strength-中{color:#fbbf24}

/* 一句话总结：蓝色左边框引用块 */
.quote{margin:2px 0 14px 50px;padding:10px 14px;border-left:3px solid var(--brand-1);
  background:var(--brand-soft);border-radius:0 8px 8px 0;color:var(--t1);font-size:14px;line-height:1.75}

/* 字段表格行（关键证据/反例或争议/适用边界） */
.fields{margin:0 0 0 50px;border-top:1px solid var(--divider)}
.field-row{display:grid;grid-template-columns:88px 1fr;gap:14px;padding:10px 0;border-bottom:1px solid var(--divider)}
.field-row .k{color:var(--t3);font-size:13px;padding-top:1px}
.field-row .v{color:var(--t2);font-size:13.5px;line-height:1.75}

/* 来源 / 原文页码 */
.src{margin:14px 0 0 50px;color:var(--t3);font-size:12px;padding-top:10px;border-top:1px dashed var(--divider)}
.refs{margin:12px 0 0 50px}
.refs>summary{cursor:pointer;color:var(--t3);font-size:12.5px;list-style:none;user-select:none}
.refs>summary::-webkit-details-marker{display:none}
.refs>summary::before{content:'▸ ';color:var(--t3)}
.refs[open]>summary::before{content:'▾ '}
.refs ul{margin:8px 0 0;padding-left:18px;color:var(--t2);font-size:12.5px;line-height:1.7}
.empty{color:var(--t3);font-style:italic;padding:6px 0}
.hidden{display:none !important}

/* “跳转到原文”按钮 */
.jump{margin-top:12px;display:inline-flex;align-items:center;gap:6px;padding:5px 12px;
  border:1px solid var(--divider);border-radius:8px;background:var(--bg-alt);color:var(--brand-1);
  font-size:12.5px;font-weight:500;transition:background .15s,border-color .15s}
.jump:hover{background:var(--brand-soft);border-color:var(--brand-1);text-decoration:none}
.jump .ico{font-size:13px;line-height:1}

/* 原文查看器（全屏覆盖，内嵌 PDF/EPUB） */
.reader{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.6);backdrop-filter:blur(2px);
  display:none;flex-direction:column;align-items:center;justify-content:center;padding:16px}
.reader.open{display:flex}
.reader .bar{width:100%;max-width:1100px;display:flex;align-items:center;gap:12px;margin-bottom:10px;color:#fff}
.reader .bar .t{font-size:14px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.reader .bar .p{color:rgba(255,255,255,.8);font-size:13px}
.reader .bar button{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;
  color:#fff;background:rgba(255,255,255,.14)}
.reader .bar button:hover{background:rgba(255,255,255,.28)}
.reader .frame{width:100%;height:calc(100vh - 90px);max-width:1100px;background:#fff;border-radius:10px;overflow:hidden}
.reader object,.reader iframe{width:100%;height:100%;border:0}
.reader .fallback{width:100%;height:calc(100vh - 90px);max-width:1100px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:14px;color:#333;text-align:center;padding:20px}
.reader .fallback .msg{font-size:14px;color:#666;max-width:440px;line-height:1.7}
.reader .fallback a{color:var(--brand-1);font-weight:600}

/* 统计 & 筛选 */
.toolbar{display:flex;align-items:center;gap:14px;margin-bottom:18px;flex-wrap:wrap;color:var(--t2);font-size:13px}
.toolbar .stat b{color:var(--t1)}
.chip{padding:5px 12px;border:1px solid var(--divider);border-radius:20px;font-size:12.5px;color:var(--t2)}
.chip.on{background:var(--brand-1);color:#fff;border-color:var(--brand-1)}
.chip:hover{border-color:var(--brand-1);color:var(--brand-1)}
.chip.on:hover{color:#fff}
.noresult{display:none;padding:40px;text-align:center;color:var(--t3)}
footer{max-width:900px;margin:0 auto;padding:20px clamp(16px,4vw,56px) 60px;color:var(--t3);font-size:12px;border-top:1px solid var(--divider)}

@media (max-width:820px){
  aside{position:fixed;left:0;top:0;bottom:0;z-index:30;transform:translateX(-100%);background:var(--bg);width:80%;max-width:320px;transition:transform .25s}
  body.nav-open aside{transform:translateX(0)}
  .wrap{display:block}
  .menu-btn{display:flex !important}
  main{padding:16px 16px 60px}
}
.menu-btn{display:none}
body.nav-open::after{content:'';position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:25}
</style>
</head>
<body>
<nav>
  <button class="icon-btn menu-btn" onclick="document.body.classList.toggle('nav-open')" aria-label="目录">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
  </button>
  <div class="logo">@@TITLE@@</div>
  <div class="spacer"></div>
  <div class="search"><input id="q" type="search" placeholder="搜索论点、证据、页码…" autocomplete="off"></div>
  <button class="icon-btn" id="themeBtn" aria-label="切换明暗">
    <svg id="iconSun" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5"/></svg>
    <svg id="iconMoon" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>
  </button>
</nav>

<div class="wrap">
  <aside class="side" id="side">
    <div class="side-title">目录</div>
    <div id="toc"></div>
  </aside>
  <main>
    <div class="overview">
      <h1>@@TITLE@@</h1>
      <div class="author">作者：@@AUTHOR@@　·　@@GEN_DATE@@ 蒸馏</div>
      <div class="ov">@@OVERVIEW@@</div>
      <div class="core" id="coreBox" style="display:none"></div>
    </div>
    <div class="toolbar">
      <span class="stat">共 <b id="statTotal">0</b> 条论点 · <b id="statChap">0</b> 章</span>
      <button class="chip" id="chipEvidence" data-on="0">只看有原文引用的</button>
      <button class="chip" id="chipAllOpen">全部展开</button>
      <button class="chip" id="chipAllClose">全部收起</button>
    </div>
    <div id="content"></div>
    <div class="noresult" id="noresult">没有匹配的条目。</div>
  </main>
</div>
<div class="reader" id="reader">
  <div class="bar">
    <div class="t" id="readerTitle">原文</div>
    <div class="p" id="readerPage"></div>
    <button id="readerPrev" title="上一页" aria-label="上一页">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 6l-6 6 6 6"/></svg>
    </button>
    <button id="readerNext" title="下一页" aria-label="下一页">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg>
    </button>
    <button id="readerOpenExt" title="新标签打开原文件" aria-label="新标签打开">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3"/></svg>
    </button>
    <button id="readerClose" title="关闭 (Esc)" aria-label="关闭">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="frame" id="readerFrame"></div>
</div>
<footer>由书籍蒸馏工具生成 · 数据内联，单文件可离线打开</footer>

<script>
const BOOK_DATA = __DATA__;

/* 转义 HTML */
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

/* 简单 Markdown 渲染：**粗体**、[文字](链接)、裸 URL */
function renderMd(text){
  let s = esc(text || '');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(https?:\/\/[^\s>]+)/g, m => '<a href="'+m+'" target="_blank" rel="noopener" style="word-break:break-all">'+m.replace(/^https?:\/\//,'')+'</a>');
  return s;
}

/* 建目录 */
function buildToc(){
  const toc = document.getElementById('toc');
  BOOK_DATA.chapters.forEach((c,i)=>{
    const a = document.createElement('a');
    a.href = '#chap'+i;
    a.innerHTML = esc(c.title) + '<span class="cnt">'+c.entries.length+'</span>';
    a.addEventListener('click',()=>document.body.classList.remove('nav-open'));
    toc.appendChild(a);
  });
}

/* 建正文 */
function buildContent(){
  const box = document.getElementById('content');
  const FIELDS = [["关键证据","关键证据"],["反例或争议","反例或争议"],["适用边界","适用边界"]];
  BOOK_DATA.chapters.forEach((c,ci)=>{
    const sec = document.createElement('section');
    sec.className = 'chapter'; sec.id = 'chap'+ci;
    const pg = c.page ? '<span class="pg">第 '+c.page+' 页起</span>' : '';
    sec.innerHTML = '<div class="chapter-head"><h2>'+esc(c.title)+pg+'</h2></div>';
    const list = document.createElement('div');
    c.entries.forEach((e,ei)=>{
      const card = document.createElement('div');
      card.className = 'card';
      // 全文搜索用（含标签与全部字段）
      const searchable = [e.title, e['类型'], e['强度']]
        .concat(FIELDS.map(f=>e[f[0]]||'')).concat(e['一句话总结']||'').join(' ');
      card.dataset.hay = searchable.toLowerCase();
      const hasEvidence = !!(e['关键证据'] && e['关键证据'].length);
      if(!hasEvidence) card.dataset.ev = '0';

      // 标题下的彩色标签
      let tags = '';
      if(e['类型']) tags += '<span class="tag tag-type-'+esc(e['类型'])+'">'+esc(e['类型'])+'</span>';
      if(e['强度']) tags += '<span class="tag tag-strength-'+esc(e['强度'])+'">'+esc(e['强度'])+'</span>';
      const tagsHtml = tags ? '<div class="tags">'+tags+'</div>' : '';

      // 一句话总结 -> 蓝色引用块
      const quote = e['一句话总结'] ? '<div class="quote">'+renderMd(e['一句话总结'])+'</div>' : '';

      // 字段表格行
      let rows = '';
      FIELDS.forEach(f=>{
        const val = e[f[0]];
        if(!val || val==='') return;
        rows += '<div class="field-row"><span class="k">'+f[1]+'</span><div class="v">'+renderMd(val)+'</div></div>';
      });
      const fieldsHtml = rows ? '<div class="fields">'+rows+'</div>' : '';

      // 来源
      const src = c.page ? '<div class="src">来源：'+esc(c.title)+' · 第 '+c.page+' 页起</div>' : '';

      // “跳转到原文”按钮：优先用该论据的具体页码（source_page），否则退回章节起始页
      let jump = '';
      if(BOOK_DATA.meta.source){
        const pg = e['source_page'] || c.page;
        const ptxt = pg ? '第 '+pg+' 页' : '本章节';
        jump = '<button class="jump" data-chap="'+ci+'" data-page="'+(pg||'')+'">'+
               '<span class="ico">📄</span> 原文 · '+ptxt+'</button>';
      }

      card.innerHTML =
        '<div class="card-head"><span class="num">'+(ei+1)+'</span><span class="ttl">'+esc(e.title)+'</span>'+
        '<span class="chev"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 6l6 6-6 6"/></svg></span></div>'+
        tagsHtml +
        '<div class="card-body">'+(quote||'')+(fieldsHtml||'<div class="empty">（无内容）</div>')+src+jump+'</div>';
      card.querySelector('.card-head').addEventListener('click',()=>card.classList.toggle('open'));
      const jb = card.querySelector('.jump');
      if(jb) jb.addEventListener('click',ev=>{ev.stopPropagation();openReader(ci, jb.dataset.page, e['source_page']?pg:c.page);});
      list.appendChild(card);
    });
    sec.appendChild(list);
    box.appendChild(sec);
  });
}

/* 概览核心 */
function buildOverview(){
  if(BOOK_DATA.meta.core && BOOK_DATA.meta.core!=='无'){
    const b = document.getElementById('coreBox');
    b.style.display='block';
    b.innerHTML = '<b>全书反复强调的核心</b>　'+renderMd(BOOK_DATA.meta.core);
  }
  const total = BOOK_DATA.chapters.reduce((s,c)=>s+c.entries.length,0);
  document.getElementById('statTotal').textContent = total;
  document.getElementById('statChap').textContent = BOOK_DATA.chapters.length;
}

/* 搜索 */
function doSearch(q){
  q = q.trim().toLowerCase();
  let any = false;
  document.querySelectorAll('.card').forEach(card=>{
    const hit = !q || card.dataset.hay.includes(q);
    card.classList.toggle('hidden', !hit);
    if(hit) any = true;
  });
  // 章节：里面没可见条目就隐藏
  document.querySelectorAll('section.chapter').forEach(sec=>{
    const visible = sec.querySelectorAll('.card:not(.hidden)').length;
    sec.classList.toggle('hidden', visible===0);
  });
  document.getElementById('noresult').style.display = any ? 'none':'block';
}
let t;
document.getElementById('q').addEventListener('input',e=>{clearTimeout(t);t=setTimeout(()=>doSearch(e.target.value),150);});

/* 只看有原文引用 */
const chipEv = document.getElementById('chipEvidence');
chipEv.addEventListener('click',()=>{
  const on = chipEv.dataset.on==='0';
  chipEv.dataset.on = on?'1':'0';
  chipEv.classList.toggle('on',on);
  document.querySelectorAll('.card').forEach(card=>{
    if(on && card.dataset.ev==='0') card.classList.add('hidden');
    else if(!on) card.classList.remove('hidden');
  });
  doSearch(document.getElementById('q').value);
});

/* 全部展开/收起 */
document.getElementById('chipAllOpen').addEventListener('click',()=>document.querySelectorAll('.card').forEach(c=>c.classList.add('open')));
document.getElementById('chipAllClose').addEventListener('click',()=>document.querySelectorAll('.card').forEach(c=>c.classList.remove('open')));

/* 明暗切换 */
const themeBtn = document.getElementById('themeBtn');
const iconSun = document.getElementById('iconSun'), iconMoon = document.getElementById('iconMoon');
function syncIcon(){const d=document.documentElement.classList.contains('dark');iconSun.style.display=d?'block':'none';iconMoon.style.display=d?'none':'block';}
themeBtn.addEventListener('click',()=>{
  const d=document.documentElement.classList.toggle('dark');
  try{localStorage.setItem('theme',d?'dark':'light');}catch(e){}
  syncIcon();
});
syncIcon();

/* 滚动高亮当前章节 */
function spy(){
  const y = scrollY + 90;
  let cur = 0;
  document.querySelectorAll('section.chapter').forEach((sec,i)=>{if(sec.offsetTop<=y)cur=i;});
  document.querySelectorAll('#toc a').forEach((a,i)=>a.classList.toggle('active',i===cur));
}
let st;
addEventListener('scroll',()=>{clearTimeout(st);st=setTimeout(spy,80);});
addEventListener('load',spy);

/* ===== 原文查看器：内嵌 PDF/EPUB，跳到指定页/章节 ===== */
let readerState = {page: 1};
function readerSrcUrl(page){
  const src = BOOK_DATA.meta.source;
  const base = src.replace(/#.*$/, '');
  const isPdf = /\.pdf$/i.test(src);
  if(isPdf && page) return base + '#page=' + page;   // Chrome/Chromium 的 PDF viewer 支持 #page=N
  return base;
}
function showReaderFallback(chapTitle, page){
  const frame = document.getElementById('readerFrame');
  frame.innerHTML =
    '<div class="fallback">'+
      '<div style="font-size:40px">📖</div>'+
      '<div class="msg">当前浏览器无法内嵌预览这份原文文件'+(page?'（第 '+page+' 页）':'')+'。<br>点击下方按钮，用浏览器自带的方式打开原文件'+(page?'，再翻到第 '+page+' 页':'')+'。</div>'+
      '<a id="readerExtLink" href="'+readerSrcUrl(page)+'" target="_blank" rel="noopener">打开原文件 '+esc(BOOK_DATA.meta.source||'')+(page?'（第 '+page+' 页）':'')+'</a>'+
    '</div>';
}
function openReader(ci, pageStr){
  const c = BOOK_DATA.chapters[ci];
  const page = pageStr ? parseInt(pageStr, 10) : (c.page || 1);
  readerState = {page: page};
  const reader = document.getElementById('reader');
  const frame = document.getElementById('readerFrame');
  document.getElementById('readerTitle').textContent = c.title;
  // 若页码等于章节起始页，说明是"章节起"；否则是该论据的具体页
  document.getElementById('readerPage').textContent =
    (c.page && page === c.page) ? '第 ' + page + ' 页起' : '第 ' + page + ' 页';
  const isPdf = /\.pdf$/i.test(BOOK_DATA.meta.source || '');
  frame.innerHTML = '<object data="'+readerSrcUrl(page)+'" type="'+(isPdf?'application/pdf':'')+'" '+
    'style="width:100%;height:100%;border:0"></object>';
  reader.classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeReader(){
  const reader = document.getElementById('reader');
  reader.classList.remove('open');
  document.getElementById('readerFrame').innerHTML = '';
  document.body.style.overflow = '';
}
function readerStep(delta){
  readerState.page = Math.max(1, (readerState.page||1) + delta);
  const base = (BOOK_DATA.meta.source||'').replace(/#.*$/, '');
  const isPdf = /\.pdf$/i.test(BOOK_DATA.meta.source || '');
  if(isPdf){
    // 仅对 PDF 尝试翻页（重设 data 会重载；部分浏览器可用）
    const obj = document.querySelector('#readerFrame object');
    if(obj) obj.data = base + '#page=' + readerState.page;
    document.getElementById('readerPage').textContent = '第 ' + readerState.page + ' 页';
  }
}
(function initReader(){
  const src = BOOK_DATA.meta.source;
  document.getElementById('readerClose').addEventListener('click', closeReader);
  document.getElementById('readerPrev').addEventListener('click', ()=>readerStep(-1));
  document.getElementById('readerNext').addEventListener('click', ()=>readerStep(1));
  document.getElementById('readerOpenExt').addEventListener('click', ()=>{
    window.open(readerSrcUrl(readerState.page), '_blank');
  });
  addEventListener('keydown', e=>{ if(e.key==='Escape') closeReader(); });
  // 点击遮罩空白处关闭
  document.getElementById('reader').addEventListener('click', e=>{ if(e.target.id==='reader') closeReader(); });
  if(!src){ /* 无原文文件：按钮不会渲染，无需绑定 */ }
})();

buildToc(); buildContent(); buildOverview();
</script>
</body>
</html>
"""


def render_book_html(book_data: dict, out_path: str):
    meta = book_data["meta"]
    data_json = json.dumps(book_data, ensure_ascii=False)
    # 防止 </script> 出现在 JSON 里破坏脚本
    data_json = data_json.replace("</", "<\\/")

    html = HTML_TEMPLATE
    html = html.replace("@@TITLE@@", esc_for_html(meta["title"]))
    html = html.replace("@@AUTHOR@@", esc_for_html(meta["author"] or "未知"))
    html = html.replace("@@GEN_DATE@@", meta["generated_at"][:10])
    html = html.replace("@@OVERVIEW@@", render_md_to_html_simple(meta["overview"]))
    html = html.replace("__DATA__", data_json)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def esc_for_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_md_to_html_simple(text: str) -> str:
    """概览/核心文本的极简 HTML 转义（只转义 + 换行）。"""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("\n", "<br>")


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="把 notes/<书名> 渲染成单文件 HTML")
    parser.add_argument("book_dir", help="notes/ 下的书目录，如 'notes/富爸爸,穷爸爸'")
    parser.add_argument("-o", "--out", default=None, help="输出 HTML 路径，默认 <书名>.html")
    args = parser.parse_args()

    book_dir = args.book_dir
    if not os.path.isabs(book_dir):
        book_dir = os.path.join(NOTES_DIR, os.path.basename(book_dir)) if not book_dir.startswith("notes") else os.path.join(os.path.dirname(NOTES_DIR), book_dir)
    if not os.path.isdir(book_dir):
        # 允许直接传目录名
        alt = os.path.join(NOTES_DIR, args.book_dir)
        if os.path.isdir(alt):
            book_dir = alt
        else:
            parser.error(f"找不到书目录: {book_dir}")

    logger.info("读取 %s", book_dir)
    book_data = load_book(book_dir)
    total = sum(len(c["entries"]) for c in book_data["chapters"])
    logger.info("共 %d 章 %d 条", len(book_data["chapters"]), total)

    out = args.out or os.path.join(book_dir, book_data["meta"]["title"] + ".html")
    render_book_html(book_data, out)
    print(f"\n完成！输出: {out}")
    print(f"双击该文件即可在浏览器打开。")


if __name__ == "__main__":
    main()
