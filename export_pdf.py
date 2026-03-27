"""
PDF 全量导出模块
================
生成带封面、目录、正文的完整项目 PDF。

架构（两步渲染）：
  Step 1. 单独渲染正文 HTML → 正文 PDF + dump-outline XML
          从 XML 获取每个标题的真实页码
  Step 2. 将 TOC（含正确页码和 href="#hN" 跳转链接）与正文合并为
          一个 HTML，一次渲染 → 目录+正文 PDF
          同一次渲染内 href="#hN" 天然有效，wkhtmltopdf 自动建立 PDF 内部链接
  Step 3. 合并封面 PDF + 目录+正文 PDF

虚线：用 Unicode 全角点字符（·）重复填满行，父容器 overflow:hidden 截断，
      页码用 position:absolute; right:0; background:#fff 叠于最上层，
      彻底消除点字符与页码重叠，任何版本 wkhtmltopdf 均可靠渲染。

heading-shift：各章节文件按文件名深度对 h1-h6 做层级偏移，使 wkhtmltopdf
               outline 树的层级与文档结构一致，dump-outline 页码准确。
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote
from xml.etree import ElementTree as ET
import html
import html as html_module
import json
import os
import re
import shutil
import subprocess

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover
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
    m = re.search(r"<body[^>]*>(.*?)</body>", html_text, flags=re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else html_text


def _strip_inner_page_breaks(html_text: str) -> str:
    html_text = re.sub(r"<style[^>]*>.*?</style>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    html_text = re.sub(r"page-break-before\s*:\s*always\s*;?", "", html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"page-break-after\s*:\s*always\s*;?", "", html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"break-before\s*:\s*page\s*;?", "", html_text, flags=re.IGNORECASE)
    html_text = re.sub(r"break-after\s*:\s*page\s*;?", "", html_text, flags=re.IGNORECASE)
    return html_text


def _shift_headings(html_text: str, shift: int) -> str:
    """
    将内容里的 h1-h6 向下偏移 shift 级（上限 h3）。
    shift=0 时不处理，直接返回。
    """
    if shift <= 0:
        return html_text

    def replace_tag(m: re.Match) -> str:
        orig_level = int(m.group(1))
        new_level = min(orig_level + shift, 3)
        attrs = m.group(2)
        inner = m.group(3)
        return f"<h{new_level}{attrs}>{inner}</h{new_level}>"

    return re.sub(
        r"<h([1-6])((?:\s[^>]*)?)\s*>(.*?)</h[1-6]\s*>",
        replace_tag,
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _inject_heading_ids(html_text: str, counter: list) -> tuple[str, list[dict]]:
    """
    给 h1/h2/h3 注入 id="hN"，同时收集标题信息。
    返回 (modified_html, [{'id','title','level'}, ...])
    """
    headings: list[dict] = []

    def repl(m: re.Match) -> str:
        num = m.group(1)
        attrs = m.group(2)
        inner = m.group(3)
        hid = f"h{counter[0]}"
        counter[0] += 1
        title = re.sub(r"<[^>]+>", "", inner).strip()
        title = re.sub(r"\s+", " ", title)
        headings.append({"id": hid, "title": title, "level": int(num)})
        if not re.search(r"\bid\s*=", attrs, re.IGNORECASE):
            attrs = f' id="{hid}"' + attrs
        return f"<h{num}{attrs}>{inner}</h{num}>"

    result = re.sub(
        r"<h([1-3])((?:\s[^>]*)?)\s*>(.*?)</h[1-3]\s*>",
        repl,
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return result, headings


def _top_chapter_key(fname: str) -> str:
    m = re.match(r"^(Chapter\d+)(?:-|_result\.html)", fname)
    return m.group(1) if m else "UNKNOWN"


def _determine_depth(fname: str) -> int:
    base = fname.replace("_result.html", "")
    return min(len(base.split("-")), 3)


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


def clean_latex_safe(html_text: str) -> str:
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


def _sanitize_stray_numeric_lines(content: str) -> str:
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


def _html_shell(title: str, body_html: str, extra_css: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>{html_module.escape(title)}</title>
  {extra_css}
</head>
<body>
{body_html}
</body>
</html>"""


# ===========================================================================
# 封面
# ===========================================================================

def _build_cover_html(project_name: str, cover_image_uri: str,
                      badge_image_uri: str, date_text: str) -> str:
    cover_css = """
<style>
  *, *::before, *::after { box-sizing: border-box; }
  html, body {
    width: 100%; height: 100%; margin: 0; padding: 0;
    background: #fff; color: #111;
    font-family: "Microsoft YaHei", "SimSun", "宋体", sans-serif;
  }
  .cover-page {
    min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; padding: 3% 5%;
  }
  .cover-badge { width: 100%; text-align: left; flex-shrink: 0; }
  .cover-badge img { max-width: 220px; width: 30%; height: auto; display: block; }
  .cover-title-box, .cover-image-box, .cover-footer-box {
    width: 100%; max-width: 900px;
  }
  .cover-title-box { padding: 3rem 1rem 2rem; text-align: center; }
  .cover-title-main, .cover-title-sub {
    font-size: 32px; line-height: 1.8; font-weight: 700;
    word-break: break-word; margin: 0.5rem 0;
  }
  .cover-image-box { margin-top: 1rem; padding: 1rem; text-align: center; }
  .cover-image-box img { max-width: 100%; width: 90%; height: auto; display: block; margin: 0 auto; }
  .cover-footer-box { margin-top: auto; width: 100%; text-align: center; padding: 1rem 0; }
  .cover-company { font-size: 20px; font-weight: 700; line-height: 1.8; }
  .cover-date { margin-top: 0.5rem; font-size: 18px; font-weight: 700; line-height: 1.6; }
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


# ===========================================================================
# dump-outline 解析（仅用于获取页码）
# ===========================================================================

def _parse_outline_items(xml_path: str) -> list[dict]:
    """
    解析 wkhtmltopdf dump-outline XML，返回 {title, page, level} 列表。
    level 由 XML 中的嵌套深度决定，是唯一可靠的层级来源——
    与文件名 depth 或 h 标签序号无关。
    从根的直接子节点出发（level=1），深度优先遍历。
    """
    if not os.path.exists(xml_path):
        return []

    tree = ET.parse(xml_path)
    root = tree.getroot()
    items: list[dict] = []

    def walk(node, level: int) -> None:
        for child in node.findall("outline:item", _OUTLINE_NS):
            title = (child.attrib.get("title") or "").strip()
            page_str = (child.attrib.get("page") or "0").strip()
            if title:
                try:
                    page = int(page_str)
                except ValueError:
                    page = 0
                items.append({"title": title, "page": page, "level": level})
            walk(child, level + 1)

    walk(root, 1)
    return items


# ===========================================================================
# 目录 HTML 构建
# ===========================================================================

# 足够长的点字符串，用于填满任意行宽；父容器 overflow:hidden 截断多余部分
_DOTS = "·" * 300


def _toc_css() -> str:
    """
    目录专用 CSS。

    虚线布局原理（position:absolute 页码 + padding-right 占位）：
      1. 父 div 设置 position:relative; overflow:hidden; white-space:nowrap
      2. 页码 span 设置 position:absolute; right:0; background:#fff
         使页码文字叠在最上层并用白底遮住下方溢出的点字符
      3. 父 div 设置 padding-right 留出与页码等宽的空间，
         使点字符自然在页码左侧截断（依赖父级 overflow:hidden）

    这样点字符从标题末尾延伸，被 overflow:hidden 截断于行右边界，
    页码用白底绝对定位叠在最上层，彻底消除重叠问题，
    在所有版本 wkhtmltopdf 中均可靠渲染。
    """
    return """
  h1.toc-heading {
    text-align: center;
    font-size: 18pt;
    font-weight: 700;
    letter-spacing: 0.4em;
    margin: 0 0 10mm 0;
  }
  .toc-row {
    display: block;
    position: relative;
    overflow: hidden;
    white-space: nowrap;
    line-height: 2.2;
    width: 100%;
    font-size: 12pt;
    font-family: "SimSun", "宋体", "Microsoft YaHei", sans-serif;
    padding-right: 3em;
  }
  .toc-pg {
    position: absolute;
    right: -1mm;
    top: 0;
    background: #fff;
    padding-left: 6px;
    font-weight: normal;
  }
  .toc-dots {
    color: #666;
  }
  .lvl-1 .toc-ti { font-weight: 700; }
  .lvl-2 .toc-ti { font-weight: normal; }
  .lvl-3 .toc-ti { font-weight: normal; }
"""


def _toc_display_level(title: str) -> int:
    """
    从标题文字推断目录显示层级（1/2/3）。

    规则（按优先级）：
      1. 以"第X章"或"第X节"开头 → lvl-1（章级）
      2. 编号含两个及以上小数点，如 6.1.1 → lvl-3
      3. 编号含一个小数点，如 6.1 / 2.1 → lvl-2
      4. 纯数字编号开头（1. / 2. / 3.）→ lvl-2
      5. 其他 → lvl-2（保底）
    """
    t = title.strip()
    if re.match(r'^第[〇一二三四五六七八九十百千\d]+[章节篇]', t):
        return 1
    if re.match(r'^\d+\.\d+\.\d+', t):
        return 3
    if re.match(r'^\d+\.\d+', t):
        return 2
    if re.match(r'^\d+[.．、]', t):
        return 2
    return 2


def _build_toc_entries(headings: list[dict]) -> str:
    """
    构建目录条目 HTML。

    每行结构（position:absolute 页码 + padding-right 占位）：
      <div class="toc-row lvl-N">
        [缩进空格]<span class="toc-ti">标题</span><span class="toc-dots">···</span>
        <span class="toc-pg">页码</span>   <!-- position:absolute; right:0; background:#fff -->
      </div>

    父 div padding-right:3em 为页码预留空间，overflow:hidden 截断超出行宽的点字符，
    页码用白底绝对定位叠于最上层，彻底消除点字符与页码重叠问题。

    缩进使用全角空格 &#x3000;（宽度 = 1em），可靠渲染于所有 WebKit 版本：
      lvl-1：无缩进
      lvl-2：2 个全角空格（≈ 2em）
      lvl-3：4 个全角空格（≈ 4em）
    """
    if not headings:
        return "<p style='text-align:center;padding-top:20mm;'>未生成目录条目</p>"

    _INDENT = {1: "", 2: "&#x3000;&#x3000;", 3: "&#x3000;&#x3000;&#x3000;&#x3000;"}

    rows = []
    for h in headings:
        display_level = _toc_display_level(h["title"])
        title_escaped = html.escape(h["title"])
        page_escaped  = html.escape(str(h.get("page", "")))
        indent_html   = _INDENT.get(display_level, "")

        rows.append(
            f'<div class="toc-row lvl-{display_level}">'
            f'{indent_html}<span class="toc-ti">{title_escaped}</span>'
            f'<span class="toc-dots">{_DOTS}</span>'
            f'<span class="toc-pg">{page_escaped}</span>'
            f'</div>'
        )

    return "".join(rows)


# ===========================================================================
# 正文 HTML 构建（含 heading ID 注入）
# ===========================================================================

# 标准 A4 中文工程文档字号规范：
#   一级标题 h1 = 18pt（三号黑体）
#   二级标题 h2 = 15pt（小三号黑体）
#   三级标题 h3 = 13pt（四号黑体）
#   正文     p  = 12pt（小四宋体）
_BODY_CSS = """
<style>
  body {
    font-family: "SimSun", "宋体", "Microsoft YaHei", sans-serif;
    font-size: 12pt;
    line-height: 1.5;
    color: #111;
    padding: 0 24px;
  }
  p { margin: 8px 0; text-align: justify; text-indent: 2em; }
  h1 { font-size: 18pt; font-weight: bold; margin: 36px 0 16px 0; text-indent: 0 !important; page-break-after: avoid; }
  h2 { font-size: 15pt; font-weight: bold; margin: 24px 0 12px 0; text-indent: 0 !important; page-break-after: avoid; }
  h3 { font-size: 13pt; font-weight: bold; margin: 16px 0 8px 0; text-indent: 0 !important; page-break-after: avoid; }
  .force-center { text-indent: 0 !important; text-align: center !important; display: block !important; width: 100% !important; margin: 10px 0 !important; }
  .caption-text { font-weight: bold; font-size: 11pt; color: #333; display: block; margin: 4px 0; text-indent: 0 !important; text-align: center !important; }
  img { max-width: 90% !important; height: auto !important; display: block; margin: 0 auto; }
  table { width: 100% !important; margin: 12px auto !important; border-collapse: collapse; border: 1.5px solid #333; text-indent: 0 !important; page-break-inside: avoid; }
  td, th { border: 1px solid #555 !important; padding: 6px 10px; text-align: center; }
  th { background: #f0f0f0; }
  .chapter-break { display: block; page-break-before: always; break-before: page; height: 0; margin: 0; padding: 0; }
</style>
"""


def _build_body_sections(output_dir: str, html_files: list[str]) -> tuple[str, list[dict]]:
    """
    处理所有章节 HTML，返回：
    - body_html: 合并后的正文 HTML（h1/h2/h3 已注入 id 属性）
    - all_headings: 按出现顺序排列的标题列表，每项含 id/title/level
    """
    merged_sections: list[str] = []
    all_headings: list[dict] = []
    prev_top_chapter: str | None = None
    heading_counter = [0]

    for fname in html_files:
        try:
            file_path = os.path.join(output_dir, fname)
            with open(file_path, "r", encoding="utf-8") as fh:
                raw_html = fh.read()

            content = _extract_body_content(raw_html)
            content = _strip_inner_page_breaks(content)
            content = clean_latex_safe(content)
            content = _rewrite_legacy_abs_paths(content)
            content = _rewrite_spic_urls_to_local_file(content)
            content = _normalize_src_href_to_file_uri(content, output_dir)
            content = _sanitize_stray_numeric_lines(content)

            depth = _determine_depth(fname)
            content = _shift_headings(content, shift=depth - 1)

            content, section_headings = _inject_heading_ids(content, heading_counter)
            all_headings.extend(section_headings)

            content = re.sub(
                r"<p>\s*(表\s*\d+(\.\d+)?[-\s]\d+[^<]*)\s*</p>",
                r'<p class="force-center caption-text">\1</p>',
                content, flags=re.IGNORECASE,
            )
            content = re.sub(
                r"(<br\s*/?>)\s*(图\s*\d+(\.\d+)?[-\s]\d+[^<]*)",
                r'\1<span class="caption-text">\2</span>',
                content, flags=re.IGNORECASE,
            )
            content = re.sub(
                r"<p(?![^>]*class=)([^>]*)>(\s*<img)",
                r'<p class="force-center"\1>\2',
                content, flags=re.IGNORECASE,
            )

            cur_top = _top_chapter_key(fname)
            if prev_top_chapter is not None and cur_top != prev_top_chapter:
                merged_sections.append('<div class="chapter-break"></div>')
            prev_top_chapter = cur_top

            merged_sections.append(
                f'<section data-source="{html.escape(fname)}">{content}</section>'
            )
        except Exception as exc:
            print(f"[export_pdf] Error processing {fname}: {exc}")

    if not merged_sections:
        raise HTTPException(status_code=500, detail="所有章节处理失败，无法导出 PDF")

    base_href = Path(output_dir).resolve().as_uri() + "/"
    body_html = (
        "<!DOCTYPE html><html><head>"
        "<meta charset='utf-8'>"
        f"<base href='{base_href}'>"
        f"{_BODY_CSS}"
        "</head><body>"
        + "".join(merged_sections)
        + "</body></html>"
    )
    return body_html, all_headings


def _build_combined_html(output_dir: str, headings_with_pages: list[dict], body_sections_html: str) -> str:
    """
    构建目录+正文合并为单一 HTML 文档。

    所有 CSS 统一放在 <head> 内，目录与正文在同一文档内，href="#hN" 天然有效。
    """
    body_content = _extract_body_content(body_sections_html)

    base_href_m = re.search(r"<base href='([^']+)'", body_sections_html)
    base_href = base_href_m.group(1) if base_href_m else ""

    toc_entries = _build_toc_entries(headings_with_pages)

    head_css = "<style>\n" + (
        "  * { box-sizing: border-box; }\n"
        "  html, body {\n"
        "    margin: 0; padding: 0;\n"
        "    font-family: \"SimSun\", \"宋体\", \"Microsoft YaHei\", sans-serif;\n"
        "    font-size: 12pt; color: #111; background: #fff;\n"
        "  }\n"
        "  a { color: inherit; text-decoration: none; }\n"
    ) + _toc_css() + (
        "  p { margin: 8px 0; text-align: justify; text-indent: 2em; }\n"
        "  h1 { font-size: 18pt; font-weight: bold; margin: 36px 0 16px 0; text-indent: 0 !important; page-break-after: avoid; }\n"
        "  h2 { font-size: 15pt; font-weight: bold; margin: 24px 0 12px 0; text-indent: 0 !important; page-break-after: avoid; }\n"
        "  h3 { font-size: 13pt; font-weight: bold; margin: 16px 0 8px 0; text-indent: 0 !important; page-break-after: avoid; }\n"
        "  .force-center { text-indent: 0 !important; text-align: center !important; display: block !important; width: 100% !important; margin: 10px 0 !important; }\n"
        "  .caption-text { font-weight: bold; font-size: 11pt; color: #333; display: block; margin: 4px 0; text-indent: 0 !important; text-align: center !important; }\n"
        "  img { max-width: 90% !important; height: auto !important; display: block; margin: 0 auto; }\n"
        "  table { width: 100% !important; margin: 12px auto !important; border-collapse: collapse; border: 1.5px solid #333; text-indent: 0 !important; page-break-inside: avoid; }\n"
        "  td, th { border: 1px solid #555 !important; padding: 6px 10px; text-align: center; }\n"
        "  th { background: #f0f0f0; }\n"
        "  .chapter-break { display: block; page-break-before: always; break-before: page; height: 0; margin: 0; padding: 0; }\n"
        "  .toc-body-sep { display: block; page-break-before: always; break-before: page; height: 0; margin: 0; padding: 0; }\n"
        "</style>"
    )

    toc_block = (
        "<h1 class='toc-heading'>目&#x3000;&#x3000;录</h1>"
        + toc_entries
    )

    return (
        "<!DOCTYPE html><html><head>"
        "<meta charset='utf-8'>"
        + (f"<base href='{base_href}'>" if base_href else "")
        + head_css
        + "</head><body>"
        + toc_block
        + "<div class='toc-body-sep'></div>"
        + body_content
        + "</body></html>"
    )


# ===========================================================================
# wkhtmltopdf 调用
# ===========================================================================

def _find_wkhtmltopdf_bin() -> str:
    env_path = os.getenv("WKHTMLTOPDF_BIN")
    for candidate in [env_path, _DEFAULT_WKHTMLTOPDF, shutil.which("wkhtmltopdf")]:
        if candidate and os.path.exists(candidate):
            return candidate
    raise HTTPException(status_code=500, detail="wkhtmltopdf 不存在，无法导出 PDF")


def _run_wkhtmltopdf(
    input_html: str,
    output_pdf: str,
    options: dict[str, str | None],
    dump_outline: str | None = None,
) -> None:
    wkhtmltopdf_bin = _find_wkhtmltopdf_bin()
    cmd = [wkhtmltopdf_bin]

    for key, value in options.items():
        # None means "omit this flag entirely" (used to cancel inherited keys)
        if value is None:
            continue
        cmd.append(f"--{key}")
        # Empty string means "flag only, no argument" (e.g. --quiet, --enable-local-file-access)
        if value != "":
            cmd.append(str(value))

    if dump_outline:
        cmd.extend(["--dump-outline", dump_outline])

    cmd.extend([input_html, output_pdf])

    result = subprocess.run(cmd, capture_output=True, text=True)
    # wkhtmltopdf 有警告时 exit code 为 1，但 PDF 已正常生成
    if result.returncode not in (0, 1) or not os.path.exists(output_pdf) or os.path.getsize(output_pdf) == 0:
        stderr = (result.stderr or "").strip()
        raise HTTPException(
            status_code=500,
            detail=f"wkhtmltopdf 执行失败 (exit={result.returncode}): {stderr}",
        )


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


def _common_pdf_options() -> dict[str, str]:
    return {
        "page-size": "A4",
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


# ===========================================================================
# 主导出接口
# ===========================================================================

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

    cover_html_path    = os.path.join(project_export_dir, f"cover_{ts}.html")
    body_html_path     = os.path.join(project_export_dir, f"body_{ts}.html")
    combined_html_path = os.path.join(project_export_dir, f"combined_{ts}.html")
    outline_xml_path   = os.path.join(project_export_dir, f"outline_{ts}.xml")

    cover_pdf_path    = os.path.join(project_export_dir, f"cover_{ts}.pdf")
    body_pdf_path     = os.path.join(project_export_dir, f"body_{ts}.pdf")
    combined_pdf_path = os.path.join(project_export_dir, f"combined_{ts}.pdf")

    temp_files = [
        cover_html_path, body_html_path, combined_html_path, outline_xml_path,
        cover_pdf_path, body_pdf_path, combined_pdf_path,
    ]

    try:
        # ── Step 0: 封面 ──────────────────────────────────────────────
        cover_html = _build_cover_html(
            project_name=project_name,
            cover_image_uri=Path(cover_image_path).resolve().as_uri(),
            badge_image_uri=Path(badge_image_path).resolve().as_uri(),
            date_text=date_text,
        )
        _write_text(cover_html_path, cover_html)

        # Cover uses zero margins and no footer.
        # Set footer-* keys to None so _run_wkhtmltopdf skips them entirely;
        # an empty-string value would emit --footer-center with no argument
        # and cause wkhtmltopdf to mis-parse all subsequent flags.
        cover_options = {
            **_common_pdf_options(),
            "margin-top": "0mm", "margin-bottom": "0mm",
            "margin-left": "0mm", "margin-right": "0mm",
            "footer-center": None,
            "footer-font-name": None,
            "footer-font-size": None,
            "footer-spacing": None,
        }
        _run_wkhtmltopdf(cover_html_path, cover_pdf_path, cover_options)

        # ── Step 1: 处理正文，注入 heading id ─────────────────────────
        body_html, all_headings = _build_body_sections(output_dir, html_files)
        _write_text(body_html_path, body_html)

        # 正文与合并 HTML 必须使用完全相同的页边距，保证 dump-outline 页码一致
        content_options = {
            **_common_pdf_options(),
            "margin-top": "25mm", "margin-bottom": "20mm",
            "margin-left": "30mm", "margin-right": "20mm",
        }

        # ── Step 2: 渲染正文（仅用于 dump-outline 获取页码）────────────
        _run_wkhtmltopdf(body_html_path, body_pdf_path, content_options, dump_outline=outline_xml_path)

        # ── Step 3: 从 dump-outline 获取层级+页码（以 XML 嵌套为准）──
        toc_headings = _parse_outline_items(outline_xml_path)

        # ── Step 4: 构建目录+正文合并 HTML ─────────────────────────────
        combined_html = _build_combined_html(output_dir, toc_headings, body_html)
        _write_text(combined_html_path, combined_html)

        _run_wkhtmltopdf(combined_html_path, combined_pdf_path, content_options)

        # ── Step 5: 合并封面 + 目录正文 ───────────────────────────────
        _merge_pdfs(final_pdf, [cover_pdf_path, combined_pdf_path])
        return FileResponse(final_pdf, filename=final_name, media_type="application/pdf")
    finally:
        for path in temp_files:
            _safe_unlink(path)


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
