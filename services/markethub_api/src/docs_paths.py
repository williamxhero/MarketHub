from __future__ import annotations


PUBLIC_PAGE_ALIAS = {
    "docs/search-docs.md": "search-docs",
    "docs/sync-workflow.md": "sync-workflow",
}

FILE_ALIAS_BY_PUBLIC_PATH = {value: key for key, value in PUBLIC_PAGE_ALIAS.items()}


def to_public_doc_path(raw_relative_path: str) -> str:
    if raw_relative_path == "README.md":
        return ""
    if raw_relative_path in PUBLIC_PAGE_ALIAS:
        return PUBLIC_PAGE_ALIAS[raw_relative_path]
    if raw_relative_path.endswith("/README.md"):
        return raw_relative_path.removesuffix("/README.md")
    if raw_relative_path.endswith(".md"):
        return raw_relative_path.removesuffix(".md")
    return raw_relative_path


def iter_relative_doc_candidates(public_path: str) -> list[str]:
    normalized_path = public_path.strip("/")
    if normalized_path == "":
        return [FILE_ALIAS_BY_PUBLIC_PATH.get("", "README.md")]
    if normalized_path.endswith(".md"):
        return [normalized_path]
    alias_path = FILE_ALIAS_BY_PUBLIC_PATH.get(normalized_path, "")
    if alias_path != "":
        return [alias_path]
    return [
        f"{normalized_path}/README.md",
        f"{normalized_path}.md",
    ]
