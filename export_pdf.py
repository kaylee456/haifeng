"""
PDF 全量导出模块
================
生成带可点击目录（标书格式）的完整项目 PDF。

目录格式：
  第一章 XXX ........... 1
    1.1 YYY ........ 2
      1.1.1 ZZZ ... 3

• 目录条目可点击跳转（wkhtmltopdf outline + XSL <a> 链接）
• 一级/二级/三级标题依次缩进
• 不出现重复章节名称或孤立数字行
• 新顶层章节另起一页

TOC 布局使用真实 HTML <table> 元素而非 CSS display:table，
因为 wkhtmltopdf 内置的旧版 WebKit 对 display:table-cell 支持不完善，
会将 title / dots / page 竖向堆叠而非横向排列。
"""

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, unquote
import os
import re

import pdfkit
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

# ---------------------------------------------------------------------------
# 路由与项目管理（按实际项目注入）
# ---------------------------------------------------------------------------
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


def _extract_body_content(html: str) -> str:
    """提取 <body> 内的片段，丢弃 <head>/<html> 壳。"""
    m = re.search(r"<body[^>]*>(.*?)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else html


def _strip_inner_page_breaks(html: str) -> str:
    """删除章节 HTML 自带的强制分页指令，避免与合并后的分页冲突。"""
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"page-break-before\s*:\s*always\s*;?", "", html, flags=re.IGNORECASE)
    html = re.sub(r"page-break-after\s*:\s*always\s*;?", "", html, flags=re.IGNORECASE)
    html = re.sub(r"break-before\s*:\s*page\s*;?", "", html, flags=re.IGNORECASE)
    html = re.sub(r"break-after\s*:\s*page\s*;?", "", html, flags=re.IGNORECASE)
    return html


def _top_chapter_key(fname: str) -> str:
    """
    提取顶层章号，用于检测章间切换以插入分页。
    Chapter10-2-1_result.html -> "Chapter10"
    """
    m = re.match(r"^(Chapter\d+)-", fname)
    return m.group(1) if m else "UNKNOWN"


def _rewrite_legacy_abs_paths(html: str) -> str:
    return html.replace(
        "/home/public/haifeng/develop_git_merge/",
        "/opt/AIHaiFeng_task6/develop/",
    )


def _normalize_src_href_to_file_uri(html: str, base_dir: str) -> str:
    """将相对路径与本机绝对路径统一转换为 file:// URI。"""
    def repl(m):
        attr, url = m.group(1), m.group(2).strip()
        if url.startswith(("http://", "https://", "file://", "data:", "#", "mailto:", "javascript:")):
            return m.group(0)
        abs_path = url if os.path.isabs(url) else os.path.abspath(os.path.join(base_dir, url))
        if os.path.exists(abs_path):
            return f'{attr}="{Path(abs_path).as_uri()}"'
        return m.group(0)

    return re.sub(r'(src|href)\s*=\s*["\']([^"\']+)["\']', repl, html, flags=re.IGNORECASE)


def _rewrite_spic_urls_to_local_file(html: str) -> str:
    """将内网/生产域名资源链接替换为本地 file:// 路径，找不到则置空。"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    allowed_hosts = {"aiconstructionplan.spic.com.cn", "172.16.12.1"}

    def repl(m):
        attr = m.group(1)
        raw_url = m.group(2).strip()
        url = "https:" + raw_url if raw_url.startswith("//") else raw_url
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if host not in allowed_hosts:
            return m.group(0)
        if not p.path.startswith("/agent/"):
            return m.group(0)
        rel_path = unquote(p.path.lstrip("/"))
        abs_path = os.path.join(project_root, rel_path)
        if os.path.exists(abs_path):
            return f'{attr}="{Path(abs_path).as_uri()}"'
        return f'{attr}=""'

    return re.sub(r'(src|href)\s*=\s*["\']([^"\']+)["\']', repl, html, flags=re.IGNORECASE)


def _strip_html_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def clean_latex_safe(html: str) -> str:
    """清理 KaTeX 渲染残留，还原纯文本数学符号。"""
    def extract_tex(match):
        tex_m = re.search(r"<annotation[^>]*>(.*?)</annotation>", match.group(0), re.DOTALL)
        return tex_m.group(1) if tex_m else ""

    html = re.sub(r'<span class="katex">.*?</span>', extract_tex, html, flags=re.DOTALL)
    html = re.sub(r"\^?\\circ", "°", html)
    html = re.sub(r"\^?\{\\circ\}", "°", html)
    html = (
        html.replace(r"^{\circ}", "°")
        .replace(r"\%", "%")
        .replace(r"\mathrm", "")
        .replace(r"\text", "")
    )
    for _ in range(5):
        html = re.sub(r"\{([^{}]*)\}", r"\1", html)
    html = html.replace("\\", "").replace("{", "").replace("}", "").replace("^", "")
    return re.sub(r"\s+", " ", html).strip()


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
    return base


def _determine_heading_level(fname: str) -> int:
    """
    根据文件名中连字符数量判断标题层级：
    Chapter10_result.html   -> 1
    Chapter10-2_result.html -> 2
    Chapter10-2-1_result.html -> 3
    """
    base = fname.replace("_result.html", "")
    parts = base.split("-")
    return min(len(parts), 3)


def _sanitize_stray_numeric_lines(content: str) -> str:
    """
    去除章节体内孤立的纯数字段落（目录残留序号）。
    匹配：<p>  12  </p> 或 <p>  2.3  </p> 等。
    """
    return re.sub(r"<p>\s*\d+(\.\d+)?\s*</p>", "", content, flags=re.IGNORECASE)


# ===========================================================================
# TOC XSL 生成
# ===========================================================================

def _write_toc_xsl(xsl_path: str) -> None:
    """
    生成标书风格目录样式表。

    使用真实 HTML <table> 而非 CSS display:table-cell 来实现
    「标题 ……… 页码」的三列横向布局。wkhtmltopdf 内嵌的旧版
    WebKit（Qt 4.x）对 display:table-cell 支持有缺陷，会将
    各 <span> 竖向堆叠；改用 <table><tr><td> 可彻底规避该问题。

    布局方案：
      <table width="100%">
        <col style="width:auto"/>           ← 标题（自适应收缩）
        <col style="width:100%"/>           ← 虚线（拉伸填满剩余）
        <col style="width:1px"/>            ← 页码（收缩到内容）
      </table>

    三级标题通过 padding-left 实现左侧缩进。
    """
    xsl = r"""<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:outline="http://wkhtmltopdf.org/outline">
  <xsl:output method="html" encoding="UTF-8" indent="no"/>

  <xsl:template match="/outline:outline">
    <html>
      <head>
        <meta charset="utf-8"/>
        <style>
          * { box-sizing: border-box; margin: 0; padding: 0; }
          body {
            font-family: "SimSun", "宋体", "Microsoft YaHei", serif;
            font-size: 12pt;
            color: #000;
            padding: 28px 36px;
          }
          h1.toc-heading {
            text-align: center;
            font-size: 16pt;
            font-weight: bold;
            letter-spacing: 6px;
            margin-bottom: 24px;
          }

          table.toc-table {
            width: 100%;
            border-collapse: collapse;
            border: none;
          }
          table.toc-table td {
            border: none;
            padding: 0;
            margin: 0;
            vertical-align: bottom;
          }

          td.toc-title {
            white-space: nowrap;
            padding-right: 4px;
            line-height: 2;
          }
          td.toc-dots {
            width: 100%;
            border-bottom: 1px dotted #444;
            line-height: 2;
          }
          td.toc-page {
            white-space: nowrap;
            text-align: right;
            padding-left: 4px;
            line-height: 2;
          }

          .l1 td.toc-title { font-weight: bold;   font-size: 12pt;   padding-left: 0; }
          .l2 td.toc-title { font-weight: normal;  font-size: 11pt;   padding-left: 2em; }
          .l3 td.toc-title { font-weight: normal;  font-size: 10.5pt; padding-left: 4em; color: #222; }

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

  <!-- 一级条目 -->
  <xsl:template match="outline:item" mode="l1">
    <xsl:if test="normalize-space(@title) != '' and not(starts-with(@title, 'Chapter'))">
      <tr class="l1">
        <td class="toc-title">
          <a>
            <xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@title"/>
          </a>
        </td>
        <td class="toc-dots"></td>
        <td class="toc-page"><xsl:value-of select="@page"/></td>
      </tr>
      <xsl:apply-templates select="outline:item" mode="l2"/>
    </xsl:if>
  </xsl:template>

  <!-- 二级条目 -->
  <xsl:template match="outline:item" mode="l2">
    <xsl:if test="normalize-space(@title) != '' and not(starts-with(@title, 'Chapter'))">
      <tr class="l2">
        <td class="toc-title">
          <a>
            <xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@title"/>
          </a>
        </td>
        <td class="toc-dots"></td>
        <td class="toc-page"><xsl:value-of select="@page"/></td>
      </tr>
      <xsl:apply-templates select="outline:item" mode="l3"/>
    </xsl:if>
  </xsl:template>

  <!-- 三级条目 -->
  <xsl:template match="outline:item" mode="l3">
    <xsl:if test="normalize-space(@title) != '' and not(starts-with(@title, 'Chapter'))">
      <tr class="l3">
        <td class="toc-title">
          <a>
            <xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
            <xsl:value-of select="@title"/>
          </a>
        </td>
        <td class="toc-dots"></td>
        <td class="toc-page"><xsl:value-of select="@page"/></td>
      </tr>
    </xsl:if>
  </xsl:template>

</xsl:stylesheet>
"""
    with open(xsl_path, "w", encoding="utf-8") as f:
        f.write(xsl)


# ===========================================================================
# 全局 CSS
# ===========================================================================

_CSS = """
<style>
  /* ---- 基础正文 ---- */
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

  /* ---- 标题层级（供 wkhtmltopdf outline 识别） ---- */
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

  /* ---- 强制居中（图表注释） ---- */
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

  /* ---- 图片 ---- */
  img {
    max-width: 90% !important;
    height: auto !important;
    display: block;
    margin: 0 auto;
  }

  /* ---- 表格 ---- */
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

  /* ---- 章间分页 ---- */
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

    # ---- 收集并自然排序章节文件 ----
    html_files = [
        f for f in os.listdir(output_dir)
        if f.startswith("Chapter") and f.endswith("_result.html")
    ]
    html_files.sort(key=natural_sort_key)

    if not html_files:
        raise HTTPException(
            status_code=404,
            detail=f"未找到可导出的章节HTML文件，扫描目录: {output_dir}",
        )

    # ---- 构建合并 HTML ----
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

            content = re.sub(
                r"<p>\s*(表\s*\d+(\.\d+)?[-\s]\d+[^<]*)\s*</p>",
                r'<p class="force-center caption-text">\1</p>',
                content,
            )
            content = re.sub(
                r"(<br\s*/?>)\s*(图\s*\d+(\.\d+)?[-\s]\d+[^<]*)",
                r'\1<span class="caption-text">\2</span>',
                content,
            )
            content = re.sub(
                r"<p(?![^>]*class=)([^>]*)>(\s*<img)",
                r'<p class="force-center"\1>\2',
                content,
            )

            cur_top = _top_chapter_key(fname)
            if prev_top_chapter is not None and cur_top != prev_top_chapter:
                merged_sections.append('<div class="chapter-break"></div>')
            prev_top_chapter = cur_top

            title = _build_section_title(fname, content)
            level = _determine_heading_level(fname)
            heading_class = f"sec-h{level}"

            anchor_id = fname.replace("_result.html", "").replace(".", "-")
            merged_sections.append(
                f'<h{level} class="{heading_class}" id="{anchor_id}">{title}</h{level}>'
            )

            first_hx = re.search(
                r"<h[1-3][^>]*>.*?</h[1-3]>", content, flags=re.IGNORECASE | re.DOTALL
            )
            if first_hx:
                inner_title = _strip_html_tags(first_hx.group(0))
                if inner_title.strip() == title.strip():
                    content = content[: first_hx.start()] + content[first_hx.end() :]

            merged_sections.append(f'<section data-source="{fname}">{content}</section>')

        except Exception as exc:
            print(f"[export_pdf] Error processing {fname}: {exc}")

    if not merged_sections:
        raise HTTPException(status_code=500, detail="所有章节处理失败，无法导出PDF")

    # ---- 组装完整 HTML ----
    base_href = Path(output_dir).resolve().as_uri() + "/"
    full_html = (
        f"<html><head>"
        f"<meta charset='utf-8'>"
        f"<base href='{base_href}'>"
        f"{_CSS}"
        f"</head><body>"
        f"{''.join(merged_sections)}"
        f"</body></html>"
    )

    # ---- TOC XSL ----
    toc_xsl_path = os.path.join(project_export_dir, f"toc_{ts}.xsl")
    _write_toc_xsl(toc_xsl_path)

    # ---- wkhtmltopdf 配置 ----
    config = pdfkit.configuration(wkhtmltopdf="/usr/local/bin/wkhtmltopdf")

    options = {
        "encoding": "UTF-8",
        "enable-local-file-access": "",
        "load-error-handling": "ignore",
        "load-media-error-handling": "ignore",
        "quiet": "",
        "margin-top": "18mm",
        "margin-bottom": "18mm",
        "margin-left": "20mm",
        "margin-right": "20mm",
        "footer-center": "第 [page] / [topage] 页",
        "footer-font-name": "Microsoft YaHei",
        "footer-font-size": "9",
        "footer-spacing": "4",
        "outline": "",
        "outline-depth": "3",
    }

    toc = {
        "toc-header-text": "目录",
        "xsl-style-sheet": toc_xsl_path,
    }

    pdfkit.from_string(
        full_html,
        final_pdf,
        configuration=config,
        options=options,
        toc=toc,
    )

    return FileResponse(final_pdf, filename=final_name, media_type="application/pdf")
