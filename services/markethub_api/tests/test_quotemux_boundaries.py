from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _assert_patterns_absent(paths: tuple[Path, ...], patterns: tuple[str, ...]) -> None:
    hits: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern in text:
                hits.append(f"{path}: {pattern}")
    assert hits == []


def test_markethub_runtime_does_not_import_quotemux_source_internals() -> None:
    paths = _python_files(REPO_ROOT / "services" / "markethub_api" / "src")

    _assert_patterns_absent(
        paths,
        (
            "quotemux.sources",
            "SourceProxy",
            "platform_provider_clients",
            "from providers",
            "import providers",
        ),
    )


def test_markethub_repo_does_not_embed_stock_platform_runtime_dirs() -> None:
    assert not (REPO_ROOT / "jobs").exists()
    assert not (REPO_ROOT / "libs").exists()
