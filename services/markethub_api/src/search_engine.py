from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sqlite3

from docs_all import PLACEHOLDER_SUMMARY_RE, SUMMARY_BY_API_PATH
from docs_paths import to_public_doc_path


SERVER_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_ROOT.parent
DOCS_ROOT = PROJECT_ROOT / "docs"
INTEGRATION_DOCS_ROOT = DOCS_ROOT / "integration-api"
DATA_DIR = SERVER_ROOT / "data"
DB_PATH = DATA_DIR / "docs_search.db"
RESULT_LIMIT = 20
INDEX_VERSION = "4"

HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
API_TITLE_RE = re.compile(r"^#\s+(?P<api_path>/api/\S+)\s*$", re.MULTILINE)
SOURCE_URL_RE = re.compile(r"^>\s*(https?://\S+)", re.MULTILINE)
TS_API_RE = re.compile(r"^接口：([A-Za-z0-9_]+)", re.MULTILINE)
PROTOTYPE_RE = re.compile(r"函数原型：.*?```(?:[^\n]*)\n([A-Za-z0-9_]+)\s*\(", re.DOTALL)
CODE_BLOCK_RE = re.compile(r"```(?:[^\n]*)\n(.*?)```", re.DOTALL)
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
MULTI_SPACE_RE = re.compile(r"\s+")
CHINESE_ONLY_RE = re.compile(r"^[\u4e00-\u9fff]+$")
IGNORED_DOC_PATHS = {"system/transitional-items.md"}
STANDARD_RESPONSE_PLACEHOLDER = "Returns the standard MarketHub payload for this API route."


@dataclass(frozen=True, slots=True)
class DocRecord:
    path: str
    title: str
    api_name: str
    docset: str
    summary: str
    content: str
    code: str
    source_url: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    title: str
    api_name: str
    docset: str
    summary: str
    snippet: str
    example: str
    source_url: str
    score: float


def normalize_query(text: str) -> str:
    return MULTI_SPACE_RE.sub(" ", text).strip()


def iter_doc_paths() -> list[Path]:
    if not INTEGRATION_DOCS_ROOT.exists():
        return []
    return sorted(
        path
        for path in INTEGRATION_DOCS_ROOT.rglob("*.md")
        if path.name != "template.md" and path.relative_to(INTEGRATION_DOCS_ROOT).as_posix() not in IGNORED_DOC_PATHS
    )


def read_markdown(path: Path) -> str:
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


def strip_markdown(text: str) -> str:
    without_code_fence = CODE_BLOCK_RE.sub(lambda match: "\n" + match.group(1) + "\n", text)
    without_links = LINK_RE.sub(lambda match: match.group(1), without_code_fence)
    without_inline_code = INLINE_CODE_RE.sub(lambda match: match.group(1), without_links)
    without_quotes = re.sub(r"^>\s?", "", without_inline_code, flags=re.MULTILINE)
    without_headings = re.sub(r"^#{1,6}\s*", "", without_quotes, flags=re.MULTILINE)
    without_tables = without_headings.replace("|", " ")
    cleaned = re.sub(r"[-]{3,}", " ", without_tables)
    return MULTI_SPACE_RE.sub(" ", cleaned).strip()


def extract_title(text: str, path: Path) -> str:
    match = HEADING_RE.search(text)
    if match:
        return match.group(1).strip()
    return path.stem


def extract_source_url(text: str) -> str:
    match = SOURCE_URL_RE.search(text)
    if match:
        return match.group(1)
    return ""


def extract_api_name(text: str, title: str) -> str:
    ts_match = TS_API_RE.search(text)
    if ts_match:
        return ts_match.group(1)
    if " - " in title:
        title_candidate = title.split(" - ", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", title_candidate):
            return title_candidate
    prototype_match = PROTOTYPE_RE.search(text)
    if prototype_match:
        return prototype_match.group(1)
    return ""


def extract_summary(text: str) -> str:
    plain_text = strip_markdown(text)
    for prefix in ("描述：", "函数原型：", "接口："):
        if prefix in plain_text:
            start = plain_text.index(prefix)
            return plain_text[start:start + 160]
    return plain_text[:160]


def extract_code(text: str) -> str:
    code_blocks = [match.group(1).strip() for match in CODE_BLOCK_RE.finditer(text)]
    return "\n\n".join(block for block in code_blocks if block)


def build_record(path: Path) -> DocRecord:
    text = read_markdown(path)
    raw_relative_path = path.relative_to(INTEGRATION_DOCS_ROOT).as_posix()
    title = extract_title(text, path)
    relative_path = to_public_doc_path(raw_relative_path)
    if raw_relative_path.startswith("docs/"):
        docset = "docs"
    elif path.parent == INTEGRATION_DOCS_ROOT:
        docset = "root"
    else:
        docset = raw_relative_path.split("/", 1)[0]
    return DocRecord(
        path=relative_path,
        title=title,
        api_name=extract_api_name(text, title),
        docset=docset,
        summary=extract_summary(text),
        content=strip_markdown(text),
        code=extract_code(text),
        source_url=extract_source_url(text),
        updated_at=path.stat().st_mtime,
    )


def source_state() -> str:
    paths = iter_doc_paths()
    if not paths:
        return f"{INDEX_VERSION}:0:0"
    latest_mtime = max(path.stat().st_mtime for path in paths)
    return f"{INDEX_VERSION}:{len(paths)}:{latest_mtime:.6f}"


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS docs;
        DROP TABLE IF EXISTS meta;
        DROP TABLE IF EXISTS docs_fts;

        CREATE TABLE docs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            api_name TEXT NOT NULL,
            docset TEXT NOT NULL,
            summary TEXT NOT NULL,
            content TEXT NOT NULL,
            code TEXT NOT NULL,
            source_url TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE docs_fts USING fts5(
            path,
            title,
            api_name,
            summary,
            content,
            code,
            tokenize='trigram'
        );
        """
    )


def build_index() -> int:
    records = [build_record(path) for path in iter_doc_paths()]
    with connect_db() as connection:
        init_schema(connection)
        for record in records:
            cursor = connection.execute(
                """
                INSERT INTO docs (path, title, api_name, docset, summary, content, code, source_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.path,
                    record.title,
                    record.api_name,
                    record.docset,
                    record.summary,
                    record.content,
                    record.code,
                    record.source_url,
                    record.updated_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO docs_fts (rowid, path, title, api_name, summary, content, code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cursor.lastrowid,
                    record.path,
                    record.title,
                    record.api_name,
                    record.summary,
                    record.content,
                    record.code,
                ),
            )
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("source_state", source_state()),
        )
    return len(records)


def read_meta(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return ""
    return row["value"]


def needs_rebuild() -> bool:
    if not DB_PATH.exists():
        return True
    try:
        with connect_db() as connection:
            return read_meta(connection, "source_state") != source_state()
    except sqlite3.DatabaseError:
        return True


def ensure_index() -> int:
    if needs_rebuild():
        return build_index()
    with connect_db() as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM docs").fetchone()
        return int(row["count"])


def build_fts_query(query: str) -> str:
    terms = [part for part in re.split(r"\s+", query) if part]
    escaped_terms = ['"' + term.replace('"', '""') + '"' for term in terms]
    return " AND ".join(escaped_terms)


def make_snippet(text: str, query: str) -> str:
    if not text:
        return ""
    plain_text = strip_markdown(text)
    lowered_text = plain_text.lower()
    lowered_query = query.lower()
    index = lowered_text.find(lowered_query)
    if index < 0:
        for term in query.split(" "):
            if not term:
                continue
            index = lowered_text.find(term.lower())
            if index >= 0:
                lowered_query = term.lower()
                break
    if index < 0:
        return plain_text[:180]
    start = max(index - 50, 0)
    end = min(index + len(lowered_query) + 120, len(plain_text))
    snippet = plain_text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(plain_text):
        snippet = snippet + "..."
    return snippet


def uses_substring_fallback(query: str) -> bool:
    compact = query.replace(" ", "")
    if compact == "":
        return True
    if len(compact) < 3 and CHINESE_ONLY_RE.fullmatch(compact):
        return True
    if len(compact) < 3 and compact.isascii():
        return True
    return False


def row_to_hit(row: sqlite3.Row, query: str) -> SearchHit:
    content = row["content"]
    code = row["code"]
    example = code[:240]
    snippet = make_snippet(content if content else code, query)
    return SearchHit(
        path=row["path"],
        title=row["title"],
        api_name=row["api_name"],
        docset=row["docset"],
        summary=row["summary"],
        snippet=snippet,
        example=example,
        source_url=row["source_url"],
        score=float(row["score"]),
    )


def search_by_fts(connection: sqlite3.Connection, query: str, limit: int) -> list[SearchHit]:
    fts_query = build_fts_query(query)
    if fts_query == "":
        return []
    rows = connection.execute(
        """
        SELECT
            docs.path,
            docs.title,
            docs.api_name,
            docs.docset,
            docs.summary,
            docs.content,
            docs.code,
            docs.source_url,
            bm25(docs_fts, 16.0, 8.0, 12.0, 4.0, 1.0, 2.0) AS score
        FROM docs_fts
        JOIN docs ON docs.id = docs_fts.rowid
        WHERE docs_fts MATCH ?
        ORDER BY
            CASE WHEN docs.path = ? THEN 0 ELSE 1 END,
            CASE WHEN docs.api_name = ? THEN 0 ELSE 1 END,
            score,
            docs.title
        LIMIT ?
        """,
        (fts_query, query, query, limit),
    ).fetchall()
    return [row_to_hit(row, query) for row in rows]


def search_by_substring(connection: sqlite3.Connection, query: str, limit: int) -> list[SearchHit]:
    keyword = query.lower()
    rows = connection.execute(
        """
        SELECT
            path,
            title,
            api_name,
            docset,
            summary,
            content,
            code,
            source_url,
            CASE
                WHEN lower(path) = ? THEN 0.0
                WHEN lower(api_name) = ? THEN 0.0
                WHEN instr(lower(path), ?) > 0 THEN 0.5
                WHEN instr(lower(title), ?) > 0 THEN 1.0
                WHEN instr(lower(api_name), ?) > 0 THEN 2.0
                WHEN instr(lower(summary), ?) > 0 THEN 3.0
                ELSE 4.0
            END AS score
        FROM docs
        WHERE
            instr(lower(path), ?) > 0
            OR
            instr(lower(title), ?) > 0
            OR instr(lower(api_name), ?) > 0
            OR instr(lower(summary), ?) > 0
            OR instr(lower(content), ?) > 0
            OR instr(lower(code), ?) > 0
        ORDER BY score, title
        LIMIT ?
        """,
        (
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            keyword,
            limit,
        ),
    ).fetchall()
    return [row_to_hit(row, query) for row in rows]


def search_docs(query: str, limit: int = RESULT_LIMIT) -> list[SearchHit]:
    normalized_query = normalize_query(query)
    if normalized_query == "":
        return []
    ensure_index()
    with connect_db() as connection:
        if uses_substring_fallback(normalized_query):
            hits = search_by_substring(connection, normalized_query, limit)
            if hits:
                return hits
        hits = search_by_fts(connection, normalized_query, limit)
        if hits:
            return hits
        return search_by_substring(connection, normalized_query, limit)
