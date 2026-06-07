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


def _venv_python_executable() -> Path:
    if sys.platform.startswith('win'):
        return VENV_ROOT / 'Scripts' / 'python.exe'
    return VENV_ROOT / 'bin' / 'python'


def _assert_layout(python_executable: Path) -> None:
    if not python_executable.is_file():
        raise RuntimeError('缺少虚拟环境解释器，请先在工作区根目录执行 install_markethub.py 完成安装。')


def _install_all_packages(python_executable: Path) -> None:
    env = os.environ.copy()
    env.setdefault('MARKETHUB_PROJECT_ROOT', str(MARKETHUB_ROOT))
    env.setdefault('QUOTEMUX_RUNTIME_ROOT', str(WORKSPACE_ROOT / 'runtime'))
    env.setdefault('DATALAKE_ROOT', str(WORKSPACE_ROOT / 'datalake'))
    subprocess.run(
        [str(python_executable), '-c', 'from quotemux import install_all_packages; print(install_all_packages())'],
        cwd=str(WORKSPACE_ROOT),
        env=env,
        check=True,
    )


if __name__ == '__main__':
    main()
