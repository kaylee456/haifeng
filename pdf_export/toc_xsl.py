"""
wkhtmltopdf 目录 XSL 生成模块。

布局：真实 HTML <table> 三列横排（标题 | 虚线 | 页码）。
wkhtmltopdf 内嵌的旧版 WebKit（Qt 4.x）对 CSS display:table-cell
支持有缺陷，会将各列竖向堆叠；真实 <table> 标签可彻底规避该问题。

遍历策略（关键！避免重复条目）：
  根模板只 select 根 outline:item 的直接子节点（outline:item/outline:item），
  赋予 mode="l1"。l1 模板渲染自身后，递归 select 子节点 mode="l2"，
  l2 再递归 mode="l3"。

  **切勿使用 //outline:item**：
  //（descendant-or-self 轴）会把所有深度的节点一次性拉平匹配，
  然后模板内再递归子节点，导致每个嵌套条目被渲染 N 次。
"""


def write_toc_xsl(xsl_path: str) -> None:
    """
    生成标书风格目录样式表。

    布局核心：
      单个 <table width="100%"> 承载所有条目，每个条目一个 <tr>：
        td.toc-title — 标题文字（nowrap，自适应宽度）
        td.toc-dots  — 虚线填充（border-bottom: dotted，width:100% 占满）
        td.toc-page  — 页码（nowrap，右对齐）

      层级缩进通过 td.toc-title 的 padding-left 控制：
        一级 0, 二级 2em, 三级 4em

    过滤：
      - 空标题和 "Chapter..." 开头的 outline 条目被跳过。
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
            padding: 2cm 2.5cm;
            line-height: 1.8;
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
          <!-- 只选根节点的直接子项，不用 // -->
          <xsl:apply-templates select="outline:item/outline:item" mode="l1"/>
        </table>
      </body>
    </html>
  </xsl:template>

  <!-- 一级条目：渲染自身，然后递归直接子节点为二级 -->
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

  <!-- 二级条目：渲染自身，然后递归直接子节点为三级 -->
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

  <!-- 三级条目：叶节点，不再递归 -->
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


_write_toc_xsl = write_toc_xsl

__all__ = ["write_toc_xsl", "_write_toc_xsl"]
