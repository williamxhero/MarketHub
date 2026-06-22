from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys


SCRIPT_ROOT = Path(__file__).resolve().parent
MARKETHUB_ROOT = SCRIPT_ROOT.parent
WORKSPACE_ROOT = MARKETHUB_ROOT.parent
VENV_ROOT = WORKSPACE_ROOT / '.venv'


def main() -> None:
    python_executable = _venv_python_executable()
    _assert_layout(python_executable)
    _install_all_packages(python_executable)
    _install_playwright_browsers()


def _venv_python_executable() -> Path:
    python_text = os.getenv('MARKETHUB_PYTHON', '')
    if python_text != '':
        return Path(python_text)
    venv_text = os.getenv('MARKETHUB_VENV_ROOT', '')
    if venv_text != '':
        return _python_executable(Path(venv_text))
    return _python_executable(VENV_ROOT)


def _python_executable(venv_path: Path) -> Path:
    if sys.platform.startswith('win'):
        return venv_path / 'Scripts' / 'python.exe'
    return venv_path / 'bin' / 'python'


def _assert_layout(python_executable: Path) -> None:
    if not python_executable.is_file():
        raise RuntimeError('缺少虚拟环境解释器，请先在工作区根目录执行 install_markethub.py 完成安装。')


def _install_all_packages(python_executable: Path) -> None:
    env = os.environ.copy()
    env.setdefault('MARKETHUB_PROJECT_ROOT', str(MARKETHUB_ROOT))
    env.setdefault('QUOTEMUX_RUNTIME_ROOT', str(WORKSPACE_ROOT / 'runtime'))
    env.setdefault('QUOTEMUX_PACKAGE_REPO_SPEC', str(WORKSPACE_ROOT / 'QuoteMux_Packages'))
    env.setdefault('DATALAKE_ROOT', str(WORKSPACE_ROOT / 'datalake'))
    subprocess.run(
        [str(python_executable), '-c', 'from quotemux import install_all_packages; print(install_all_packages())'],
        cwd=str(WORKSPACE_ROOT),
        env=env,
        check=True,
    )


def _install_playwright_browsers() -> None:
    package_venv_root = WORKSPACE_ROOT / 'runtime' / 'package_venvs'
    candidates = sorted(package_venv_root.glob('crawler_provider-*'))
    if candidates == []:
        return
    python_executable = _package_python_executable(candidates[-1])
    if not python_executable.is_file():
        return
    subprocess.run(
        [str(python_executable), '-m', 'playwright', 'install', 'chromium'],
        cwd=str(WORKSPACE_ROOT),
        check=True,
    )


def _package_python_executable(venv_path: Path) -> Path:
    if sys.platform.startswith('win'):
        return venv_path / 'Scripts' / 'python.exe'
    return venv_path / 'bin' / 'python'


if __name__ == '__main__':
    main()
