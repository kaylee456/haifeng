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
  /* Box-sizing reset that actually cascades to all children */
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

  /*
   * Cover container fills exactly one page height.
   * Using flex column lets margin-top: auto on the footer
   * push it to the bottom — safer for print/PDF renderers
   * than relying on fixed positioning.
   */
  .cover-page {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 3% 5%;
  }

  /* Badge stays in flow as the first flex child, aligned to the left. */
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

  /*
   * .cover-content takes all remaining vertical space below the badge
   * (flex: 1) and centers its children as a group on the vertical axis.
   * This shifts the title + image + footer block to the visual centre
   * of the page rather than leaving it clustered at the top.
   */
  .cover-content {
    flex: 1;
    width: 100%;
    max-width: 900px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
  }

  /* Shared constraints for the three content blocks */
  .cover-title-box,
  .cover-image-box,
  .cover-footer-box {
    width: 100%;
  }

  .cover-title-box {
    padding: 1rem 1rem 1.5rem;
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

  /*
   * Footer sits directly below the image as part of the centred group.
   * margin-top: auto pushes it away from the image to the bottom edge
   * of .cover-content, so the group reads: [title][image]…[footer].
   */
  .cover-footer-box {
    margin-top: auto;
    width: 100%;
    text-align: center;
    padding: 1rem 0 0;
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
  <div class="cover-content">
    <div class="cover-title-box">
      <div class="cover-title-main">{html_module.escape(project_name)}</div>
      <div class="cover-title-sub">海上工程施工组织总设计</div>
    </div>
    <div class="cover-image-box">
      <img src="{html_module.escape(cover_image_uri)}" alt="封面大图" />
    </div>
    <div class="cover-footer-box">
      <div class="cover-company">山东电力工程咨询院有限公司</div>
      <div class="cover-date">{html_module.escape(date_text)}</div>
    </div>
  </div>
</div>
"""
    return _html_shell("封面", body_html, cover_css)
