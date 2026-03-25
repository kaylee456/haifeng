"""
wkhtmltopdf 目录 XSL 生成：扁平左对齐、标题与页码间虚线、页码右对齐。
可与 export-full-pdf 路由中的 _write_toc_xsl 合并使用。
"""


def write_toc_xsl(xsl_path: str) -> None:
    """
    生成标书风格目录样式表：
    - 不区分层级：所有条目左对齐、无缩进
    - 标题与页码之间虚线填充
    - 页码在页面右侧对齐
    - 过滤掉原始 "Chapter..." 形式的 outline 条目（防止重复）
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
          /* 扁平目录：任意嵌套 ul 均不缩进 */
          ul.toc-list {
            list-style: none;
            padding-left: 0 !important;
            margin: 0;
          }
          li { margin: 0; }

          /*
           * 勿对标题列用 width:1% + 宽 100% 中间列：旧版 WebKit/wkhtmltopdf 会把标题压成极窄，
           * 中文出现「一字一行」；勿用 word-break:break-word，易逐字断开。
           */
          .toc-row {
            display: table;
            width: 100%;
            table-layout: fixed;
            border-spacing: 0;
            padding: 3px 0;
            font-weight: normal;
            font-size: 11pt;
            color: #111;
          }
          .toc-title {
            display: table-cell;
            width: 52%;
            white-space: normal;
            word-break: normal;
            vertical-align: baseline;
          }
          .toc-dots {
            display: table-cell;
            width: 38%;
            vertical-align: baseline;
            padding: 0 6px;
            background-image: radial-gradient(circle, #333 1px, transparent 1px);
            background-size: 6px 1px;
            background-repeat: repeat-x;
            background-position: 0 80%;
          }
          .toc-page {
            display: table-cell;
            width: 10%;
            white-space: nowrap;
            text-align: right;
            vertical-align: baseline;
            padding-left: 4px;
          }

          a { color: inherit; text-decoration: none; }
          a:hover { text-decoration: underline; }
        </style>
      </head>
      <body>
        <h1 class="toc-heading">目　　录</h1>
        <ul class="toc-list">
          <xsl:apply-templates select="outline:item/outline:item" mode="entry"/>
        </ul>
      </body>
    </html>
  </xsl:template>

  <!-- 一级：渲染自身并递归子级（样式与二三级相同，无缩进） -->
  <xsl:template match="outline:item" mode="entry">
    <xsl:if test="normalize-space(@title) != '' and not(starts-with(@title, 'Chapter'))">
      <li class="toc-item">
        <div class="toc-row">
          <span class="toc-title">
            <a>
              <xsl:attribute name="href"><xsl:value-of select="@link"/></xsl:attribute>
              <xsl:value-of select="@title"/>
            </a>
          </span>
          <span class="toc-dots"></span>
          <span class="toc-page"><xsl:value-of select="@page"/></span>
        </div>
        <xsl:if test="count(outline:item) &gt; 0">
          <ul class="toc-list">
            <xsl:apply-templates select="outline:item" mode="entry"/>
          </ul>
        </xsl:if>
      </li>
    </xsl:if>
  </xsl:template>

</xsl:stylesheet>
"""
    with open(xsl_path, "w", encoding="utf-8") as f:
        f.write(xsl)


# 与历史代码中的私有函数名兼容
_write_toc_xsl = write_toc_xsl

__all__ = ["write_toc_xsl", "_write_toc_xsl"]
