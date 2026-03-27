"""
PDF 全量导出模块
================
生成带封面、目录、正文的完整项目 PDF。

目录生成流程：
1. 将正文渲染为 PDF 并 dump-outline（wkhtmltopdf 生成 XML）
2. 解析 outline XML，构建目录 HTML
3. 分别渲染封面、目录、正文 PDF，最后合并

outline 层级策略（heading-shift）：
  wkhtmltopdf 以页面内所有 <h1>-<h6> 为节点构建 outline。
  各章节文件的深度由文件名决定：
    Chapter1_result.html     -> depth=1（顶层章）
    Chapter1-2_result.html   -> depth=2（节）
    Chapter1-2-1_result.html -> depth=3（子节）
  合并时对每个文件的标题做 shift = depth-1：
    depth=1: h1→h1, h2→h2, h3→h3  （不偏移）
    depth=2: h1→h2, h2→h3, h3→h3  （+1，最大h3）
    depth=3: h1→h3, h2→h3, h3→h3  （+2，最大h3）
  这样合并后所有标题的层级直接反映文档结构，
  wkhtmltopdf 自然生成正确的嵌套 outline，无需注入或隐藏任何元素。
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote
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
except ImportError:  # pragma: no cover - runtime fallback
    from PyPDF2 import PdfReader, PdfWriter


_LEGACY_ROOT = "/home/public/haifeng/develop_git_merge"
_DEPLOY_ROOT = "/opt/AIHaiFeng_task6/develop"
_DEFAULT_WKHTMLTOPDF = "/usr/local/bin/wkhtmltopdf"

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


def _shift_headings(html_text: str, shift: int) -> str:
    """
    将内容里的 <h1>-<h6> 统一向下偏移 shift 级，上限为 <h3>。

    例：shift=1 时 h1→h2, h2→h3, h3→h3；
        shift=2 时 h1→h3, h2→h3, h3→h3。

    shift=0 时不做任何替换，直接返回原文。
    """
    if shift <= 0:
        return html_text

    def replace_tag(m: re.Match) -> str:
        orig_level = int(m.group(1))
        new_level = min(orig_level + shift, 3)
        attrs = m.group(2)      # 原标签属性（含空格前缀）
        inner = m.group(3)
        return f"<h{new_level}{attrs}>{inner}</h{new_level}>"

    return re.sub(
        r"<h([1-6])((?:\s[^>]*)?)\s*>(.*?)</h[1-6]\s*>",
        replace_tag,
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


def _determine_depth(fname: str) -> int:
    """
    根据文件名中连字符数量判断深度（1-based）：
    Chapter10_result.html    -> 1
    Chapter10-2_result.html  -> 2
    Chapter10-2-1_result.html -> 3
    """
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
  *, *::before, *::after { box-sizing: border-box; }

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


def _write_toc_xsl(xsl_path: str) -> None:
    """
    生成供 wkhtmltopdf 原生 toc 子命令使用的 XSL 样式表。

    为什么用 toc 子命令而不是独立渲染 TOC HTML 再合并：
    1. 独立渲染的 TOC PDF 里 href="#anchor" 锚点跳转在合并后无效，
       因为目标锚点在另一个 PDF 文件里。
    2. CSS background-image/radial-gradient 在独立 HTML 打印渲染时
       需要 -webkit-print-color-adjust:exact 才能输出，容易被遗漏。
    toc 子命令在同一次渲染里生成目录+正文，wkhtmltopdf 自动建立
    真正的 PDF 内部跳转链接，border-bottom:dotted 也完全可靠。

    遍历策略（避免重复）：
    - 根模板仅 select outline:item/outline:item（根节点的直接子项）
    - 每级模板渲染自身后递归下一级的直接子节点
    - 切勿使用 //outline:item（会导致每个条目被多次渲染）
    """
    xsl = r"""<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:outline="http://wkhtmltopdf.org/outline">
  <xsl:output method="html" encoding="UTF-8" indent="no"/>

  <xsl:template match="outline:outline">
    <html>
      <head>
        <meta charset="utf-8"/>
        <style>
          * { box-sizing: border-box; margin: 0; padding: 0; }
          body {
            font-family: "SimSun", "&#23435;&#20307;", "Microsoft YaHei", serif;
            font-size: 12pt;
            color: #000;
            padding: 2cm 2.5cm;
            line-height: 1.8;
          }
          h1.toc-heading {
            text-align: center;
            font-size: 16pt;
            font-weight: bold;
            letter-spacing: 6px;
            margin-bottom: 20px;
          }
          table.toc-table {
            width: 100%;
            border-collapse: collapse;
            border: none;
            table-layout: fixed;
          }
          table.toc-table td {
            border: none;
            padding: 2px 0;
            vertical-align: bottom;
            line-height: 2;
          }
          td.toc-title {
            width: 60%;
            white-space: normal;
            word-break: normal;
            padding-right: 4px;
          }
          td.toc-dots {
            width: 30%;
            border-bottom: 1px dotted #555;
          }
          td.toc-page {
            width: 10%;
            white-space: nowrap;
            text-align: right;
            padding-left: 4px;
          }
          tr.l1 td.toc-title { font-weight: bold;   font-size: 12pt;   padding-left: 0; }
          tr.l2 td.toc-title { font-weight: normal;  font-size: 11pt;   padding-left: 2em; }
          tr.l3 td.toc-title { font-weight: normal;  font-size: 10.5pt; padding-left: 4em; color: #333; }
          a { color: inherit; text-decoration: none; }
        </style>
      </head>
      <body>
        <h1 class="toc-heading">&#x76EE;&#x3000;&#x3000;&#x5F55;</h1>
        <table class="toc-table">
          <xsl:apply-templates select="outline:item/outline:item" mode="l1"/>
        </table>
      </body>
    </html>
  </xsl:template>

  <xsl:template match="outline:item" mode="l1">
    <xsl:if test="normalize-space(@title) != ''">
      <tr class="l1">
        <td class="toc-title">
          <a><xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@title"/>
          </a>
        </td>
        <td class="toc-dots"></td>
        <td class="toc-page">
          <a><xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@page"/>
          </a>
        </td>
      </tr>
      <xsl:apply-templates select="outline:item" mode="l2"/>
    </xsl:if>
  </xsl:template>

  <xsl:template match="outline:item" mode="l2">
    <xsl:if test="normalize-space(@title) != ''">
      <tr class="l2">
        <td class="toc-title">
          <a><xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@title"/>
          </a>
        </td>
        <td class="toc-dots"></td>
        <td class="toc-page">
          <a><xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@page"/>
          </a>
        </td>
      </tr>
      <xsl:apply-templates select="outline:item" mode="l3"/>
    </xsl:if>
  </xsl:template>

  <xsl:template match="outline:item" mode="l3">
    <xsl:if test="normalize-space(@title) != ''">
      <tr class="l3">
        <td class="toc-title">
          <a><xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@title"/>
          </a>
        </td>
        <td class="toc-dots"></td>
        <td class="toc-page">
          <a><xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@page"/>
          </a>
        </td>
      </tr>
    </xsl:if>
  </xsl:template>

</xsl:stylesheet>
"""
    with open(xsl_path, "w", encoding="utf-8") as fh:
        fh.write(xsl)


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
    toc_xsl: str | None = None,
) -> None:
    """
    调用 wkhtmltopdf 将 HTML 转为 PDF。

    若传入 toc_xsl，则在全局选项之后、正文 input_html 之前
    插入 toc 子命令，使 wkhtmltopdf 在同一次渲染里生成目录：
        wkhtmltopdf [global-options] toc --xsl-style-sheet <xsl> <html> <pdf>
    这样目录中的链接是真正的 PDF 内部跳转，虚线也在同一渲染里输出。
    """
    wkhtmltopdf_bin = _find_wkhtmltopdf_bin()
    cmd = [wkhtmltopdf_bin]

    for key, value in options.items():
        cmd.append(f"--{key}")
        if value != "":
            cmd.append(str(value))

    if toc_xsl:
        cmd.extend(["toc", "--xsl-style-sheet", toc_xsl])

    cmd.extend([input_html, output_pdf])

    result = subprocess.run(cmd, capture_output=True, text=True)
    # wkhtmltopdf 正常完成时 exit code 可能是 0 或 1（有警告但 PDF 已生成）。
    # 只有当输出文件不存在或为空时才视为真正失败。
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
  h1 {
    font-size: 16pt;
    font-weight: bold;
    margin: 32px 0 14px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }
  h2 {
    font-size: 13pt;
    font-weight: bold;
    margin: 20px 0 10px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
  }
  h3 {
    font-size: 12pt;
    font-weight: bold;
    margin: 14px 0 8px 0;
    text-indent: 0 !important;
    page-break-after: avoid;
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

            content = _extract_body_content(raw_html)
            content = _strip_inner_page_breaks(content)
            content = clean_latex_safe(content)
            content = _rewrite_legacy_abs_paths(content)
            content = _rewrite_spic_urls_to_local_file(content)
            content = _normalize_src_href_to_file_uri(content, output_dir)
            content = _sanitize_stray_numeric_lines(content)

            # heading-shift：按文件深度将标题层级下移，使合并后 outline 层级正确
            depth = _determine_depth(fname)
            content = _shift_headings(content, shift=depth - 1)

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

            merged_sections.append(
                f'<section data-source="{html.escape(fname)}">{content}</section>'
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
    body_html_path  = os.path.join(project_export_dir, f"body_{ts}.html")
    toc_xsl_path    = os.path.join(project_export_dir, f"toc_{ts}.xsl")

    cover_pdf_path  = os.path.join(project_export_dir, f"cover_{ts}.pdf")
    body_toc_pdf_path = os.path.join(project_export_dir, f"body_toc_{ts}.pdf")

    temp_files = [
        cover_html_path,
        body_html_path,
        toc_xsl_path,
        cover_pdf_path,
        body_toc_pdf_path,
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
        _write_toc_xsl(toc_xsl_path)

        cover_options = {
            **_common_pdf_options(),
            "margin-top": "8mm",
            "margin-bottom": "18mm",
            "margin-left": "10mm",
            "margin-right": "10mm",
        }
        # 正文 + 目录在同一次渲染里完成：
        #   wkhtmltopdf [options] toc --xsl-style-sheet toc.xsl body.html output.pdf
        # --outline / --outline-depth 让 wkhtmltopdf 构建书签树，toc 子命令依赖它
        body_options = {
            **_common_pdf_options(),
            "margin-top": "18mm",
            "margin-bottom": "18mm",
            "margin-left": "20mm",
            "margin-right": "20mm",
            "outline": "",
            "outline-depth": "3",
        }

        _run_wkhtmltopdf(cover_html_path, cover_pdf_path, cover_options)
        _run_wkhtmltopdf(body_html_path, body_toc_pdf_path, body_options, toc_xsl=toc_xsl_path)

        _merge_pdfs(final_pdf, [cover_pdf_path, body_toc_pdf_path])
        return FileResponse(final_pdf, filename=final_name, media_type="application/pdf")
    finally:
        for path in temp_files:
            _safe_unlink(path)
