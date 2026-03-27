"""
PDF 全量导出模块
================
生成带封面、目录、正文的完整项目 PDF。

目录通过以下流程生成：
1. 先将正文渲染为 PDF 并 dump-outline（wkhtmltopdf 生成 XML）
2. 解析 outline XML，构建目录 HTML
3. 分别渲染封面、目录、正文 PDF，最后合并

关键修复：
- 章节 HTML 的原有 h1/h2/h3 标签会干扰 outline 层级，
  故在合并前先将章节内容里的 h1-h6 降格为 div.sec-hX，
  仅保留由 _build_hidden_outline_heading 注入的隐藏 outline 锚点标题，
  以确保 wkhtmltopdf 的 outline 层级与文件名层级一致。
- _parse_outline_items 遍历时从 level=1 起算，
  _build_toc_html 构建时归一化偏移，保证第一级条目缩进为 lvl-1。
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote
from xml.etree import ElementTree as ET
import html
import json
import os
import re
import shutil
import subprocess

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover - runtime fallback
    from PyPDF2 import PdfReader, PdfWriter


_LEGACY_ROOT = "/home/public/haifeng/develop_git_merge"
_DEPLOY_ROOT = "/opt/AIHaiFeng_task6/develop"
_DEFAULT_WKHTMLTOPDF = "/usr/local/bin/wkhtmltopdf"
_OUTLINE_NS = {"outline": "http://wkhtmltopdf.org/outline"}

router = APIRouter()
try:
    from project_manager import project_manager  # noqa: F401
except ImportError:
    project_manager = None  # type: ignore[assignment]


# ===========================================================================
# 工具函数
# ===========================================================================

def natural_sort_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _extract_body_content(html_text: str) -> str:
    """提取 <body> 内的片段，丢弃 <head>/<html> 壳。"""
    m = re.search(r"<body[^>]*>(.*?)</body>", html_text, flags=re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else html_text


def _strip_inner_page_breaks(html_text: str) -> str:
    """删除章节 HTML 自带的强制分页指令，避免与合并后的分页冲突。"""
    html_text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"page-break-before\s*:\s*always\s*;?", "", html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"page-break-after\s*:\s*always\s*;?", "", html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"break-before\s*:\s*page\s*;?", "", html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"break-after\s*:\s*page\s*;?", "", html_text, flags=re.IGNORECASE)
    return html_text


def _demote_content_headings(html_text: str) -> str:
    """
    将章节内容里原有的 <h1>-<h6> 标签替换为视觉等效的 <div class="sec-hX">。

    原因：wkhtmltopdf 以页面内所有 <h1>-<h6> 为节点构建 outline（目录树）。
    若章节文件自带 <h1> 正文标题，会与我们注入的隐藏锚点标题冲突，
    导致目录层级混乱（多出条目、缩进不对）。

    转换后的 div.sec-hX 通过 CSS 保持相同字体大小/粗细，
    不会被 wkhtmltopdf 的 outline 引擎识别。
    """
    def replace_heading(m: re.Match) -> str:
        tag_open = m.group(1)   # e.g. "h2" or "h3 class='foo'"
        tag_name = re.match(r"h([1-6])", tag_open, re.IGNORECASE).group(0)  # "h2"
        level = tag_name[1]
        inner = m.group(2)
        existing_class = re.search(r'class=["\']([^"\']*)["\']', tag_open)
        if existing_class:
            classes = existing_class.group(1) + f" sec-h{level}"
            new_open = re.sub(r'class=["\'][^"\']*["\']', f'class="{classes}"', tag_open)
        else:
            new_open = tag_open + f' class="sec-h{level}"'
        new_open = re.sub(r"^h[1-6]", "div", new_open, flags=re.IGNORECASE)
        return f"<{new_open}>{inner}</div>"

    return re.sub(
        r"<(h[1-6](?:\s[^>]*)?)\s*>(.*?)</h[1-6]\s*>",
        replace_heading,
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _top_chapter_key(fname: str) -> str:
    """
    提取顶层章号，用于检测章间切换以插入分页。
    Chapter10-2-1_result.html -> "Chapter10"
    Chapter10_result.html -> "Chapter10"
    """
    m = re.match(r"^(Chapter\d+)(?:-|_result\.html)", fname)
    return m.group(1) if m else "UNKNOWN"


def _rewrite_legacy_abs_paths(html_text: str) -> str:
    return html_text.replace(_LEGACY_ROOT + "/", _DEPLOY_ROOT + "/")


def _candidate_paths(raw_path: str) -> list[str]:
    candidates = [raw_path]
    if raw_path.startswith(_LEGACY_ROOT):
        candidates.append(raw_path.replace(_LEGACY_ROOT, _DEPLOY_ROOT, 1))
    elif raw_path.startswith(_DEPLOY_ROOT):
        candidates.append(raw_path.replace(_DEPLOY_ROOT, _LEGACY_ROOT, 1))
    return candidates


def _resolve_existing_path(*raw_paths: str) -> str | None:
    for raw_path in raw_paths:
        for candidate in _candidate_paths(raw_path):
            if candidate and os.path.exists(candidate):
                return candidate
    return None


def _normalize_src_href_to_file_uri(html_text: str, base_dir: str) -> str:
    """将相对路径与本机绝对路径统一转换为 file:// URI。"""

    def repl(m):
        attr, url = m.group(1), m.group(2).strip()
        if url.startswith(("http://", "https://", "file://", "data:", "#", "mailto:", "javascript:")):
            return m.group(0)
        abs_path = url if os.path.isabs(url) else os.path.abspath(os.path.join(base_dir, url))
        abs_path = _resolve_existing_path(abs_path) or abs_path
        if os.path.exists(abs_path):
            return f'{attr}="{Path(abs_path).resolve().as_uri()}"'
        return m.group(0)

    return re.sub(r'(src|href)\s*=\s*["\']([^"\']+)["\']', repl, html_text, flags=re.IGNORECASE)


def _rewrite_spic_urls_to_local_file(html_text: str) -> str:
    """将内网/生产域名资源链接替换为本地 file:// 路径，找不到则置空。"""
    allowed_hosts = {"aiconstructionplan.spic.com.cn", "172.16.12.1"}
    search_roots = [_DEPLOY_ROOT, _LEGACY_ROOT, str(Path(__file__).resolve().parent)]

    def repl(m):
        attr = m.group(1)
        raw_url = m.group(2).strip()
        url = "https:" + raw_url if raw_url.startswith("//") else raw_url
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host not in allowed_hosts or not p.path.startswith("/agent/"):
            return m.group(0)

        rel_path = unquote(p.path.lstrip("/"))
        for root in search_roots:
            abs_path = os.path.join(root, rel_path)
            if os.path.exists(abs_path):
                return f'{attr}="{Path(abs_path).resolve().as_uri()}"'
        return f'{attr}=""'

    return re.sub(r'(src|href)\s*=\s*["\']([^"\']+)["\']', repl, html_text, flags=re.IGNORECASE)


def _strip_html_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def clean_latex_safe(html_text: str) -> str:
    """清理 KaTeX 渲染残留，还原纯文本数学符号。"""

    def extract_tex(match):
        tex_m = re.search(r"<annotation[^>]*>(.*?)</annotation>", match.group(0), re.DOTALL)
        return tex_m.group(1) if tex_m else ""

    html_text = re.sub(r'<span class="katex">.*?</span>', extract_tex, html_text, flags=re.DOTALL)
    html_text = re.sub(r"\^?\\circ", "°", html_text)
    html_text = re.sub(r"\^?\{\\circ\}", "°", html_text)
    html_text = (
        html_text.replace(r"^{\circ}", "°")
        .replace(r"\%", "%")
        .replace(r"\mathrm", "")
        .replace(r"\text", "")
    )
    for _ in range(5):
        html_text = re.sub(r"\{([^{}]*)\}", r"\1", html_text)
    html_text = html_text.replace("\\", "").replace("{", "").replace("}", "").replace("^", "")
    return re.sub(r"\s+", " ", html_text).strip()


def _build_section_title(fname: str, content: str) -> str:
    """
    从章节 HTML 内的第一个 h1/h2/h3 提取可读标题。
    若找不到则从文件名生成：Chapter10-2-1_result.html -> "第10章 2-1节"
    """
    m = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", content, flags=re.IGNORECASE | re.DOTALL)
    if m:
        t = _strip_html_tags(m.group(1))
        if t:
            return t

    base = fname.replace("_result.html", "")
    m2 = re.match(r"^Chapter(\d+)-(.+)$", base)
    if m2:
        return f"第{m2.group(1)}章 {m2.group(2)}节"

    m3 = re.match(r"^Chapter(\d+)$", base)
    if m3:
        return f"第{m3.group(1)}章"

    return base


def _determine_heading_level(fname: str) -> int:
    """
    根据文件名中连字符数量判断标题层级：
    Chapter10_result.html -> 1
    Chapter10-2_result.html -> 2
    Chapter10-2-1_result.html -> 3
    """
    base = fname.replace("_result.html", "")
    parts = base.split("-")
    return min(len(parts), 3)


def _sanitize_stray_numeric_lines(content: str) -> str:
    """
    去除章节体内孤立的纯数字段落（目录残留序号）。
    匹配：<p> 12 </p> 或 <p> 2.3 </p> 等。
    """
    return re.sub(r"<p>\s*\d+(\.\d+)?\s*</p>", "", content, flags=re.IGNORECASE)


def _read_project_name(project_id: str) -> str:
    template_path = _resolve_existing_path(
        f"{_LEGACY_ROOT}/config/target_project_template.json",
        f"{_DEPLOY_ROOT}/config/target_project_template.json",
    )
    if not template_path:
        return f"项目{project_id}"

    try:
        with open(template_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        project_name = ((data or {}).get("basic_info") or {}).get("project_name")
        return str(project_name).strip() if project_name else f"项目{project_id}"
    except Exception:
        return f"项目{project_id}"


def _current_year_month() -> str:
    now = datetime.now()
    return f"{now.year}年{now.month}月"


import html as html_module


def _html_shell(title: str, body_html: str, extra_css: str = "") -> str:
    """Wrap body_html in a minimal HTML5 shell."""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html_module.escape(title)}</title>
  {extra_css}
</head>
<body>
{body_html}
</body>
</html>"""


def _build_cover_html(
    project_name: str,
    cover_image_uri: str,
    badge_image_uri: str,
    date_text: str,
) -> str:
    cover_css = """
<style>
  *, *::before, *::after {
    box-sizing: border-box;
  }

  html, body {
    width: 100%;
    height: 100%;
    margin: 0;
    padding: 0;
    background: #ffffff;
    color: #111;
    font-family: "Microsoft YaHei", "SimSun", "宋体", sans-serif;
  }

  .cover-page {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 3% 5%;
  }

  .cover-badge {
    width: 100%;
    text-align: left;
    flex-shrink: 0;
  }
  .cover-badge img {
    max-width: 220px;
    width: 30%;
    height: auto;
    display: block;
  }

  .cover-title-box,
  .cover-image-box,
  .cover-footer-box {
    width: 100%;
    max-width: 900px;
  }

  .cover-title-box {
    padding: 3rem 1rem 2rem;
    text-align: center;
  }
  .cover-title-main,
  .cover-title-sub {
    font-size: 32px;
    line-height: 1.8;
    font-weight: 700;
    word-break: break-word;
    margin: 0.5rem 0;
  }

  .cover-image-box {
    margin-top: 1rem;
    padding: 1rem;
    text-align: center;
  }
  .cover-image-box img {
    max-width: 100%;
    width: 90%;
    height: auto;
    display: block;
    margin: 0 auto;
  }

  .cover-footer-box {
    margin-top: auto;
    width: 100%;
    text-align: center;
    padding: 1rem 0;
  }

  .cover-company {
    font-size: 20px;
    font-weight: 700;
    line-height: 1.8;
  }
  .cover-date {
    margin-top: 0.5rem;
    font-size: 18px;
    font-weight: 700;
    line-height: 1.6;
  }
</style>
"""

    body_html = f"""
<div class="cover-page">
  <div class="cover-badge">
    <img src="{html_module.escape(badge_image_uri)}" alt="封面角标" />
  </div>

  <br><br><br>
  <div class="cover-title-box">
    <div class="cover-title-main">{html_module.escape(project_name)}</div>
    <div class="cover-title-sub">海上工程施工组织总设计</div>
  </div>

  <div class="cover-image-box">
    <img src="{html_module.escape(cover_image_uri)}" alt="封面大图" />
  </div>

  <br><br><br><br><br><br>
  <div class="cover-footer-box">
    <div class="cover-company">山东电力工程咨询院有限公司</div>
    <div class="cover-date">{html_module.escape(date_text)}</div>
  </div>
</div>
"""
    return _html_shell("封面", body_html, cover_css)


def _build_hidden_outline_heading(title: str, level: int) -> str:
    """
    注入一个对用户不可见、但会被 wkhtmltopdf outline 引擎识别的标题锚点。
    使用真实的 <h1>/<h2>/<h3> 标签，依靠 CSS 将其视觉隐藏（高度0、字号0）。
    这是 outline 层级的唯一来源；章节内容里的原生标题已被 _demote_content_headings 替换。
    """
    tag = f"h{max(1, min(level, 3))}"
    safe_title = html.escape(title)
    return (
        f'<{tag} class="outline-anchor outline-level-{level}">{safe_title}</{tag}>'
    )


def _parse_outline_items(xml_path: str) -> list[dict]:
    """
    解析 wkhtmltopdf dump-outline 生成的 XML，返回扁平化的目录条目列表。

    遍历策略：从 outline 根的直接子 outline:item 出发，逐级递归，
    level 从 1 起算，避免 //outline:item 导致的重复。
    """
    if not os.path.exists(xml_path):
        return []

    tree = ET.parse(xml_path)
    root = tree.getroot()

    def walk(node, level: int) -> list[dict]:
        items: list[dict] = []
        for child in node.findall("outline:item", _OUTLINE_NS):
            title = (child.attrib.get("title") or "").strip()
            page = (child.attrib.get("page") or "").strip()
            if title:
                items.append({"title": title, "page": page, "level": level})
            items.extend(walk(child, level + 1))
        return items

    # 从 outline 根的直接子 item 开始（level=1），不使用 //
    return walk(root, 1)


def _build_toc_html(items: list[dict]) -> str:
    toc_css = """
<style>
  html, body {
    margin: 0;
    padding: 0;
    color: #111;
    font-family: "SimSun", "宋体", "Microsoft YaHei", sans-serif;
    font-size: 12pt;
    background: #fff;
  }
  body {
    padding: 18mm 20mm;
    box-sizing: border-box;
  }
  .toc-title {
    margin: 0 0 12mm;
    text-align: center;
    font-size: 18pt;
    font-weight: 700;
    letter-spacing: 0.4em;
  }
  .toc-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .toc-item {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin: 4px 0;
    line-height: 1.8;
  }
  .toc-text {
    background: #fff;
    position: relative;
    z-index: 1;
    padding-right: 6px;
    white-space: nowrap;
  }
  .toc-dots {
    flex: 1;
    border-bottom: 1px dotted #444;
    transform: translateY(-2px);
  }
  .toc-page {
    min-width: 22px;
    text-align: right;
    background: #fff;
    position: relative;
    z-index: 1;
    padding-left: 6px;
  }
  /* 一级：章标题，不缩进，加粗 */
  .lvl-1 { padding-left: 0;    font-weight: 700; }
  /* 二级：缩进 1.5em */
  .lvl-2 { padding-left: 1.5em; font-weight: normal; }
  /* 三级：缩进 3em */
  .lvl-3 { padding-left: 3em;   font-weight: normal; }
</style>
"""

    if not items:
        body_html = """
<h1 class="toc-title">目 录</h1>
<p style="text-align:center;margin-top:20mm;">未生成目录条目</p>
"""
        return _html_shell("目录", body_html, toc_css)

    # 归一化层级偏移：让实际最小层级映射到 lvl-1，避免所有条目都从 lvl-2 开始
    min_level = min(int(item["level"]) for item in items)
    level_offset = min_level - 1

    rows = []
    for item in items:
        raw_level = max(1, int(item["level"]) - level_offset)
        display_level = min(raw_level, 3)
        rows.append(
            "<li class='toc-item lvl-{level}'>"
            "<span class='toc-text'>{title}</span>"
            "<span class='toc-dots'></span>"
            "<span class='toc-page'>{page}</span>"
            "</li>".format(
                level=display_level,
                title=html.escape(str(item["title"])),
                page=html.escape(str(item["page"])),
            )
        )

    body_html = (
        "<h1 class='toc-title'>目 录</h1>"
        "<ul class='toc-list'>"
        + "".join(rows)
        + "</ul>"
    )
    return _html_shell("目录", body_html, toc_css)


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _find_wkhtmltopdf_bin() -> str:
    env_path = os.getenv("WKHTMLTOPDF_BIN")
    for candidate in [env_path, _DEFAULT_WKHTMLTOPDF, shutil.which("wkhtmltopdf")]:
        if candidate and os.path.exists(candidate):
            return candidate
    raise HTTPException(status_code=500, detail="wkhtmltopdf 不存在，无法导出 PDF")


def _run_wkhtmltopdf(
    input_html: str,
    output_pdf: str,
    options: dict[str, str],
    dump_outline: str | None = None,
) -> None:
    wkhtmltopdf_bin = _find_wkhtmltopdf_bin()
    cmd = [wkhtmltopdf_bin]

    for key, value in options.items():
        cmd.append(f"--{key}")
        if value != "":
            cmd.append(str(value))

    if dump_outline:
        cmd.extend(["--dump-outline", dump_outline])

    cmd.extend([input_html, output_pdf])

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - depends on runtime binary
        stderr = (exc.stderr or "").strip()
        raise HTTPException(
            status_code=500,
            detail=f"wkhtmltopdf 执行失败: {stderr or exc}",
        ) from exc


def _merge_pdfs(output_pdf: str, input_pdfs: list[str]) -> None:
    writer = PdfWriter()
    for pdf_path in input_pdfs:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            writer.add_page(page)

    with open(output_pdf, "wb") as fh:
        writer.write(fh)


def _safe_unlink(path: str) -> None:
    if path and os.path.exists(path):
        os.remove(path)


_BODY_CSS = """
<style>
  body {
    font-family: "SimSun", "宋体", "Microsoft YaHei", sans-serif;
    font-size: 12pt;
    line-height: 1.9;
    color: #111;
    padding: 0 24px;
  }
  p {
    margin: 8px 0;
    text-align: justify;
    text-indent: 2em;
  }

  /* 章节内容里原生 h1-h6 已被 _demote_content_headings 转为 div.sec-hX，
     样式与原标题一致，但不会被 outline 引擎识别。 */
  div.sec-h1 {
    font-size: 16pt;
    font-weight: bold;
    margin: 32px 0 14px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }
  div.sec-h2 {
    font-size: 13pt;
    font-weight: bold;
    margin: 20px 0 10px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }
  div.sec-h3 {
    font-size: 12pt;
    font-weight: bold;
    margin: 14px 0 8px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }
  div.sec-h4, div.sec-h5, div.sec-h6 {
    font-size: 11.5pt;
    font-weight: bold;
    margin: 10px 0 6px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }

  /* 兼容旧版保留的 .sec-h1/h2/h3 类名写在 <h> 标签上的情况 */
  h1.sec-h1 {
    font-size: 16pt;
    font-weight: bold;
    margin: 32px 0 14px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }
  h2.sec-h2 {
    font-size: 13pt;
    font-weight: bold;
    margin: 20px 0 10px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }
  h3.sec-h3 {
    font-size: 12pt;
    font-weight: bold;
    margin: 14px 0 8px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }

  /*
   * outline 锚点：视觉隐藏，但 wkhtmltopdf outline 引擎仍可识别。
   * 注意：不能设置 font-size:0，否则 wkhtmltopdf 会跳过该标题，
   * 导致 dump-outline XML 为空、目录无条目。
   * 用 height:0 + overflow:hidden + color:transparent 做视觉隐藏。
   */
  .outline-anchor {
    display: block !important;
    margin: 0 !important;
    padding: 0 !important;
    height: 0 !important;
    line-height: 0 !important;
    overflow: hidden !important;
    color: transparent !important;
    border: 0 !important;
  }

  .force-center {
    text-indent: 0 !important;
    text-align: center !important;
    display: block !important;
    width: 100% !important;
    margin: 10px 0 !important;
  }
  .caption-text {
    font-weight: bold;
    font-size: 11pt;
    color: #333;
    display: block;
    margin: 4px 0;
    text-indent: 0 !important;
    text-align: center !important;
  }
  img {
    max-width: 90% !important;
    height: auto !important;
    display: block;
    margin: 0 auto;
  }
  table {
    width: 100% !important;
    margin: 12px auto !important;
    border-collapse: collapse;
    border: 1.5px solid #333;
    text-indent: 0 !important;
    page-break-inside: avoid;
  }
  td, th {
    border: 1px solid #555 !important;
    padding: 6px 10px;
    text-align: center;
  }
  th { background: #f0f0f0; }
  .chapter-break {
    display: block;
    page-break-before: always;
    break-before: page;
    height: 0;
    margin: 0;
    padding: 0;
  }
</style>
"""


def _build_body_html(output_dir: str, html_files: list[str]) -> str:
    merged_sections: list[str] = []
    prev_top_chapter: str | None = None

    for fname in html_files:
        try:
            file_path = os.path.join(output_dir, fname)
            with open(file_path, "r", encoding="utf-8") as fh:
                raw_html = fh.read()

            # 在提取标题前先拿到原始内容（含 h1-h6），用于 _build_section_title
            pre_demote = _extract_body_content(raw_html)
            section_title = _build_section_title(fname, pre_demote)

            content = _strip_inner_page_breaks(pre_demote)
            content = clean_latex_safe(content)
            content = _rewrite_legacy_abs_paths(content)
            content = _rewrite_spic_urls_to_local_file(content)
            content = _normalize_src_href_to_file_uri(content, output_dir)
            content = _sanitize_stray_numeric_lines(content)

            # 将章节内原生 h1-h6 降格为 div.sec-hX，避免干扰 outline 层级
            content = _demote_content_headings(content)

            content = re.sub(
                r"<p>\s*(表\s*\d+(\.\d+)?[-\s]\d+[^<]*)\s*</p>",
                r'<p class="force-center caption-text">\1</p>',
                content,
                flags=re.IGNORECASE,
            )
            content = re.sub(
                r"(<br\s*/?>)\s*(图\s*\d+(\.\d+)?[-\s]\d+[^<]*)",
                r'\1<span class="caption-text">\2</span>',
                content,
                flags=re.IGNORECASE,
            )
            content = re.sub(
                r"<p(?![^>]*class=)([^>]*)>(\s*<img)",
                r'<p class="force-center"\1>\2',
                content,
                flags=re.IGNORECASE,
            )

            cur_top = _top_chapter_key(fname)
            if prev_top_chapter is not None and cur_top != prev_top_chapter:
                merged_sections.append('<div class="chapter-break"></div>')
            prev_top_chapter = cur_top

            heading_level = _determine_heading_level(fname)
            outline_heading = _build_hidden_outline_heading(section_title, heading_level)
            merged_sections.append(
                f'<section data-source="{html.escape(fname)}">{outline_heading}{content}</section>'
            )
        except Exception as exc:
            print(f"[export_pdf] Error processing {fname}: {exc}")

    if not merged_sections:
        raise HTTPException(status_code=500, detail="所有章节处理失败，无法导出 PDF")

    base_href = Path(output_dir).resolve().as_uri() + "/"
    return (
        "<!DOCTYPE html><html><head>"
        "<meta charset='utf-8'>"
        f"<base href='{base_href}'>"
        f"{_BODY_CSS}"
        "</head><body>"
        f"{''.join(merged_sections)}"
        "</body></html>"
    )


def _common_pdf_options() -> dict[str, str]:
    return {
        "encoding": "UTF-8",
        "enable-local-file-access": "",
        "load-error-handling": "ignore",
        "load-media-error-handling": "ignore",
        "quiet": "",
        "footer-center": "第 [page] / [topage] 页",
        "footer-font-name": "Microsoft YaHei",
        "footer-font-size": "9",
        "footer-spacing": "4",
    }


@router.get("/{project_id}/export-full-pdf")
def export_full_project_pdf(project_id: str):
    if project_manager is None:
        raise HTTPException(status_code=500, detail="project_manager 未配置")

    output_dir = project_manager.get_project_output_dir(project_id)
    if not output_dir or not os.path.exists(output_dir):
        raise HTTPException(status_code=404, detail="项目输出目录不存在")

    project_export_dir = os.path.join(os.getcwd(), "pdf_exports", project_id)
    os.makedirs(project_export_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_name = f"项目{project_id}_完整报告_{ts}.pdf"
    final_pdf = os.path.join(project_export_dir, final_name)

    html_files = [
        f for f in os.listdir(output_dir)
        if f.startswith("Chapter") and f.endswith("_result.html")
    ]
    html_files.sort(key=natural_sort_key)

    if not html_files:
        raise HTTPException(
            status_code=404,
            detail=f"未找到可导出的章节 HTML 文件，扫描目录: {output_dir}",
        )

    cover_image_path = _resolve_existing_path(f"{_LEGACY_ROOT}/agent/face/封面大图.png")
    badge_image_path = _resolve_existing_path(f"{_LEGACY_ROOT}/agent/face/封面角标.png")
    if not cover_image_path or not badge_image_path:
        raise HTTPException(status_code=500, detail="封面图片不存在，无法生成封面")

    project_name = _read_project_name(project_id)
    date_text = _current_year_month()

    cover_html_path = os.path.join(project_export_dir, f"cover_{ts}.html")
    toc_html_path = os.path.join(project_export_dir, f"toc_{ts}.html")
    body_html_path = os.path.join(project_export_dir, f"body_{ts}.html")
    outline_xml_path = os.path.join(project_export_dir, f"outline_{ts}.xml")

    cover_pdf_path = os.path.join(project_export_dir, f"cover_{ts}.pdf")
    toc_pdf_path = os.path.join(project_export_dir, f"toc_{ts}.pdf")
    body_pdf_path = os.path.join(project_export_dir, f"body_{ts}.pdf")

    temp_files = [
        cover_html_path,
        toc_html_path,
        body_html_path,
        outline_xml_path,
        cover_pdf_path,
        toc_pdf_path,
        body_pdf_path,
    ]

    try:
        cover_html = _build_cover_html(
            project_name=project_name,
            cover_image_uri=Path(cover_image_path).resolve().as_uri(),
            badge_image_uri=Path(badge_image_path).resolve().as_uri(),
            date_text=date_text,
        )
        body_html = _build_body_html(output_dir, html_files)

        _write_text(cover_html_path, cover_html)
        _write_text(body_html_path, body_html)

        cover_options = {
            **_common_pdf_options(),
            "margin-top": "8mm",
            "margin-bottom": "18mm",
            "margin-left": "10mm",
            "margin-right": "10mm",
        }
        toc_options = {
            **_common_pdf_options(),
            "margin-top": "16mm",
            "margin-bottom": "18mm",
            "margin-left": "20mm",
            "margin-right": "20mm",
        }
        body_options = {
            **_common_pdf_options(),
            "margin-top": "18mm",
            "margin-bottom": "18mm",
            "margin-left": "20mm",
            "margin-right": "20mm",
        }

        _run_wkhtmltopdf(cover_html_path, cover_pdf_path, cover_options)
        _run_wkhtmltopdf(body_html_path, body_pdf_path, body_options, dump_outline=outline_xml_path)

        toc_items = _parse_outline_items(outline_xml_path)
        toc_html = _build_toc_html(toc_items)
        _write_text(toc_html_path, toc_html)
        _run_wkhtmltopdf(toc_html_path, toc_pdf_path, toc_options)

        _merge_pdfs(final_pdf, [cover_pdf_path, toc_pdf_path, body_pdf_path])
        return FileResponse(final_pdf, filename=final_name, media_type="application/pdf")
    finally:
        for path in temp_files:
            _safe_unlink(path)
