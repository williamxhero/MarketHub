from __future__ import annotations

from html import escape
from pathlib import Path
import re

import markdown
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pygments.formatters import HtmlFormatter

from core.config import INTEGRATION_DOCS_ROOT, STATIC_INDEX_PATH
from docs_all import PLACEHOLDER_SUMMARY_RE, SUMMARY_BY_API_PATH, build_all_doc_payload
from docs_paths import iter_relative_doc_candidates, to_public_doc_path
from search_engine import RESULT_LIMIT, build_index, search_docs


router = APIRouter()
MARKDOWN_EXTENSIONS = [
    "admonition",
    "codehilite",
    "fenced_code",
    "md_in_html",
    "sane_lists",
    "tables",
    "toc",
]
MARKDOWN_EXTENSION_CONFIGS = {
    "codehilite": {
        "css_class": "codehilite",
        "guess_lang": False,
        "use_pygments": True,
    },
    "toc": {
        "permalink": False,
    },
}
CODE_FORMATTER = HtmlFormatter(style="monokai")

API_TITLE_RE = re.compile(r"^#\s+(?P<api_path>/api/\S+)\s*$", re.MULTILINE)
DOCS_PATH_RE = re.compile(r"(?<![\w-])/docs(?=/|`|\s|$)")
DOC_VIEW_GET_CODE_RE = re.compile(r"`GET (?P<path>/doc-view[^`\s]*)`")
DOC_VIEW_CODE_RE = re.compile(r"`(?P<path>/doc-view[^`\s]*)`")
DOC_VIEW_TEXT_RE = re.compile(r"(?<![\"'=/>`])(?P<path>/doc-view[^\s<)`]*)")
HTTP_URL_CODE_RE = re.compile(r"`(?P<url>https?://[^`\s<>]+)`")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
STANDARD_RESPONSE_PLACEHOLDER = "Returns the standard MarketHub payload for this API route."


def read_text_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    return normalize_doc_content(text)


def normalize_doc_content(text: str) -> str:
    title_match = API_TITLE_RE.search(text)
    if title_match is None:
        return text
    api_path = title_match.group("api_path")
    summary = SUMMARY_BY_API_PATH.get(api_path, "返回该接口对应的数据。")
    lines = text.splitlines()
    title_seen = False
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not title_seen:
            if line.startswith("# "):
                title_seen = True
            continue
        if line == "":
            continue
        if line.startswith("## "):
            break
        plain_line = INLINE_CODE_RE.sub(lambda match: match.group(1), line)
        if PLACEHOLDER_SUMMARY_RE.match(plain_line):
            lines[index] = f"`GET` {summary}"
        break
    for index, raw_line in enumerate(lines):
        if raw_line.strip() == STANDARD_RESPONSE_PLACEHOLDER:
            lines[index] = "返回该接口的标准 MarketHub 响应数据。"
    normalized = "\n".join(lines)
    if text.endswith("\n"):
        return normalized + "\n"
    return normalized


def extract_doc_title(text: str, path: Path) -> str:
    match = HEADING_RE.search(text)
    if match:
        return match.group(1).strip()
    return path.stem


def build_doc_payload(path: Path) -> dict[str, str]:
    content = read_text_file(path)
    raw_relative_path = path.relative_to(INTEGRATION_DOCS_ROOT).as_posix()
    title = extract_doc_title(content, path)
    return {
        "path": to_public_doc_path(raw_relative_path),
        "title": title,
        "content": content,
    }


def resolve_doc_payload(path_text: str, with_links: bool) -> dict[str, str]:
    normalized_path = path_text.replace("\\", "/").strip().strip("/")
    if normalized_path == "all":
        return build_all_doc_payload(with_links)
    return build_doc_payload(resolve_doc_path(normalized_path))


def resolve_doc_path(path_text: str) -> Path:
    normalized_path = path_text.replace("\\", "/").strip().lstrip("/")
    docs_root = INTEGRATION_DOCS_ROOT.resolve()
    for relative_path in iter_relative_doc_candidates(normalized_path):
        candidate = INTEGRATION_DOCS_ROOT / relative_path
        try:
            resolved_path = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if resolved_path.is_file() and docs_root in resolved_path.parents:
            return resolved_path
    raise HTTPException(status_code=404, detail="文档不存在")


def build_doc_view_anchor(path_text: str) -> str:
    safe_path = escape(path_text, quote=True)
    return f'<a href="{safe_path}"><code>{safe_path}</code></a>'


def build_external_url_anchor(url_text: str) -> str:
    safe_url = escape(url_text, quote=True)
    return f'<a href="{safe_url}"><code>{safe_url}</code></a>'


def build_view_content(text: str) -> str:
    content = DOCS_PATH_RE.sub("/doc-view", text)
    content = HTTP_URL_CODE_RE.sub(lambda match: build_external_url_anchor(match.group("url")), content)
    content = DOC_VIEW_GET_CODE_RE.sub(lambda match: build_doc_view_anchor(match.group("path")), content)
    content = DOC_VIEW_CODE_RE.sub(lambda match: build_doc_view_anchor(match.group("path")), content)
    return DOC_VIEW_TEXT_RE.sub(lambda match: build_doc_view_anchor(match.group("path")), content)


def render_doc_html(payload: dict[str, str]) -> str:
    rendered = markdown.markdown(
        build_view_content(payload["content"]),
        extensions=MARKDOWN_EXTENSIONS,
        extension_configs=MARKDOWN_EXTENSION_CONFIGS,
        output_format="html5",
    )
    title = escape(payload["title"])
    doc_path = escape(payload["path"])
    code_css = CODE_FORMATTER.get_style_defs(".doc-body .codehilite")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="icon" href="/favicon.ico" type="image/svg+xml">
  <style>
    :root {{
      --bg: #f4efe6;
      --panel: #fffaf1;
      --line: #d8c8ae;
      --text: #241c15;
      --muted: #6a5d51;
      --accent: #a54d2d;
      --code-bg: #1d252c;
      --code-text: #e6edf3;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, #f9e5cf 0, transparent 32%),
        linear-gradient(135deg, #f4efe6 0%, #efe4d2 100%);
    }}

    .shell {{
      max-width: 1040px;
      margin: 0 auto;
      padding: 32px 20px 72px;
    }}

    .topbar {{
      margin-bottom: 18px;
      color: var(--muted);
      font-size: 14px;
      display: flex;
      gap: 16px;
      align-items: center;
      flex-wrap: wrap;
    }}

    .topbar a,
    .topbar button {{
      color: var(--accent);
      text-decoration: none;
      background: none;
      border: none;
      padding: 0;
      font: inherit;
      cursor: pointer;
    }}

    .panel {{
      background: color-mix(in srgb, var(--panel) 92%, white 8%);
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: 28px 32px 36px;
      box-shadow: 0 18px 46px rgba(87, 58, 38, 0.10);
    }}

    .doc-path {{
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      margin: 0 0 24px;
      padding: 0 12px;
      border-radius: 999px;
      border: 1px solid color-mix(in srgb, var(--line) 90%, white 10%);
      background: rgba(255, 251, 244, 0.95);
      color: var(--muted);
      font-size: 13px;
      word-break: break-all;
    }}

    .doc-body {{
      line-height: 1.82;
      font-size: 16px;
    }}

    .doc-body > *:first-child {{
      margin-top: 0;
    }}

    .doc-body p,
    .doc-body ul,
    .doc-body ol,
    .doc-body blockquote,
    .doc-body table,
    .doc-body pre {{
      margin-top: 0;
      margin-bottom: 18px;
    }}

    .doc-body h1,
    .doc-body h2,
    .doc-body h3,
    .doc-body h4 {{
      line-height: 1.25;
      margin-top: 34px;
      margin-bottom: 14px;
      scroll-margin-top: 24px;
    }}

    .doc-body h1 {{
      font-size: clamp(28px, 4vw, 42px);
      letter-spacing: -0.02em;
    }}

    .doc-body h2 {{
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(216, 200, 174, 0.9);
      font-size: 26px;
    }}

    .doc-body h3 {{
      font-size: 21px;
    }}

    .doc-body h4 {{
      font-size: 18px;
    }}

    .doc-body ul,
    .doc-body ol {{
      padding-left: 24px;
    }}

    .doc-body li + li {{
      margin-top: 6px;
    }}

    .doc-body hr {{
      margin: 28px 0;
      border: 0;
      border-top: 1px solid rgba(216, 200, 174, 0.85);
    }}

    .doc-body blockquote {{
      padding: 14px 18px;
      border-left: 4px solid #d38a55;
      border-radius: 0 14px 14px 0;
      background: rgba(243, 229, 212, 0.75);
      color: #5f4d3e;
    }}

    .doc-body blockquote > *:last-child {{
      margin-bottom: 0;
    }}

    .doc-body table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
      background: rgba(255, 253, 248, 0.9);
      overflow: hidden;
      border-radius: 16px;
    }}

    .doc-body th,
    .doc-body td {{
      border: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
    }}

    .doc-body th {{
      background: #f5e4d4;
    }}

    .doc-body img {{
      max-width: 100%;
      height: auto;
      border-radius: 16px;
    }}

    .doc-body .codehilite {{
      margin-bottom: 18px;
      overflow: auto;
      border-radius: 18px;
      border: 1px solid rgba(94, 110, 123, 0.28);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
    }}

    .doc-body .codehilite pre {{
      margin: 0;
      padding: 18px 20px;
      overflow: auto;
      background: var(--code-bg);
      color: var(--code-text);
      font-size: 13px;
      line-height: 1.7;
    }}

    .doc-body code {{
      font-family: Consolas, "Courier New", monospace;
    }}

    .doc-body :not(pre) > code {{
      padding: 2px 6px;
      border-radius: 6px;
      background: #f5e4d4;
      color: #7c4127;
    }}

    .doc-body a {{
      color: var(--accent);
      text-underline-offset: 2px;
    }}

    .doc-body a:hover {{
      color: #8b381d;
    }}

    {code_css}

    @media (max-width: 720px) {{
      .shell {{
        padding: 22px 14px 56px;
      }}

      .panel {{
        padding: 22px 18px 28px;
        border-radius: 22px;
      }}

      .doc-body {{
        font-size: 15px;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="topbar">
      <button type="button" id="back-button">返回上一页</button>
      <a href="/doc-view/search">打开搜索页</a>
      <a href="/doc-view">文档首页</a>
    </div>
    <section class="panel">
      <div class="doc-path">{doc_path}</div>
      <article class="doc-body">{rendered}</article>
    </section>
  </main>
  <script>
    const backButton = document.getElementById("back-button");

    function fallbackBack() {{
      try {{
        const raw = sessionStorage.getItem("integration_docs_search_state");
        if (raw) {{
          const saved = JSON.parse(raw);
          if (saved.query) {{
            window.location.href = `/doc-view/search?q=${{encodeURIComponent(saved.query)}}`;
            return;
          }}
        }}
      }} catch (error) {{
        void error;
      }}
      window.location.href = "/doc-view/search";
    }}

    backButton.addEventListener("click", () => {{
      if (document.referrer) {{
        try {{
          const referrer = new URL(document.referrer);
          if (referrer.origin === window.location.origin && window.history.length > 1) {{
            window.history.back();
            return;
          }}
        }} catch (error) {{
          void error;
        }}
      }}
      fallbackBack();
    }});
  </script>
</body>
</html>"""


@router.get("/docs/search")
async def search_docs_api(q: str = Query(""), limit: int = Query(RESULT_LIMIT, ge=1, le=50)) -> dict[str, object]:
    items = [
        {
            "path": hit.path,
            "title": hit.title,
            "api_name": hit.api_name,
            "docset": hit.docset,
            "summary": hit.summary,
            "snippet": hit.snippet,
            "example": hit.example,
            "source_url": hit.source_url,
            "score": hit.score,
        }
        for hit in search_docs(q, limit)
    ]
    return {"items": items}


@router.post("/api/admin/docs/reindex")
def api_reindex() -> dict[str, int]:
    return {"count": build_index()}


@router.get("/docs")
@router.get("/docs/")
async def read_root_doc() -> dict[str, str]:
    return resolve_doc_payload("", False)


@router.get("/docs/{doc_path:path}")
async def read_doc(doc_path: str) -> dict[str, str]:
    return resolve_doc_payload(doc_path, False)


@router.get("/doc-view/search")
@router.get("/doc-view/search/")
async def docs_search_page() -> FileResponse:
    return FileResponse(STATIC_INDEX_PATH)


@router.get("/doc-view", response_class=HTMLResponse)
@router.get("/doc-view/", response_class=HTMLResponse)
async def docs_home() -> HTMLResponse:
    return HTMLResponse(render_doc_html(resolve_doc_payload("", True)))


@router.get("/doc-view/{doc_path:path}", response_class=HTMLResponse)
async def docs_page(doc_path: str) -> HTMLResponse:
    return HTMLResponse(render_doc_html(resolve_doc_payload(doc_path, True)))

