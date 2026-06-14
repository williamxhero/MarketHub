from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys


SCRIPT_ROOT = Path(__file__).resolve().parent
MARKETHUB_ROOT = SCRIPT_ROOT.parent
WORKSPACE_ROOT = MARKETHUB_ROOT.parent
SERVICE_ROOT = MARKETHUB_ROOT / 'services' / 'markethub_api'
APP_PATH = SERVICE_ROOT / 'app.py'
VENV_ROOT = WORKSPACE_ROOT / '.venv'
RUNTIME_ROOT = Path(os.getenv('MARKETHUB_RUNTIME_ROOT', str(WORKSPACE_ROOT / 'runtime'))).expanduser().resolve()


def main() -> None:
    python_executable = _venv_python_executable()
    _assert_layout(python_executable)
    _run_api(python_executable)


def _venv_python_executable() -> Path:
    if sys.platform.startswith('win'):
        return VENV_ROOT / 'Scripts' / 'python.exe'
    return VENV_ROOT / 'bin' / 'python'


def _assert_layout(python_executable: Path) -> None:
    if not APP_PATH.is_file():
        raise RuntimeError(f'缺少 API 入口文件: {APP_PATH}')
    if not python_executable.is_file():
        raise RuntimeError('缺少虚拟环境解释器，请先在工作区根目录执行 install_markethub.py 完成安装。')


def _run_api(python_executable: Path) -> None:
    env = os.environ.copy()
    _load_env_file(env, RUNTIME_ROOT / 'env' / 'markethub.env')
    env.setdefault('MARKETHUB_PROJECT_ROOT', str(MARKETHUB_ROOT))
    env.setdefault('MARKETHUB_RUNTIME_ROOT', str(RUNTIME_ROOT))
    env.setdefault('QUOTEMUX_RUNTIME_ROOT', str(RUNTIME_ROOT / 'runtime'))
    env.setdefault('DATALAKE_ROOT', str(WORKSPACE_ROOT / 'datalake'))
    subprocess.run([str(python_executable), str(APP_PATH)], cwd=str(SERVICE_ROOT), env=env, check=True)


def _load_env_file(env: dict[str, str], path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if line == '' or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        env.setdefault(name, value)


if __name__ == '__main__':
    main()
