from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
VENV_ROOT = ROOT / ".venv"
QUOTEMUX_ROOT = ROOT / "QuoteMux"
MARKETHUB_ROOT = ROOT / "MarketHub"
RUNTIME_ROOT = Path(os.getenv("MARKETHUB_RUNTIME_ROOT", str(ROOT / "runtime"))).expanduser().resolve()


def main() -> None:
    _assert_layout()
    python_executable = _ensure_venv()
    _install_core_projects(python_executable)
    _prepare_workspace()
    _ensure_workspace_directories()
    _verify_install(python_executable)
    _print_next_steps(python_executable)


def _assert_layout() -> None:
    if not QUOTEMUX_ROOT.is_dir():
        raise RuntimeError(f"缺少 QuoteMux 目录: {QUOTEMUX_ROOT}")
    if not MARKETHUB_ROOT.is_dir():
        raise RuntimeError(f"缺少 MarketHub 目录: {MARKETHUB_ROOT}")


def _ensure_venv() -> Path:
    python_executable = _venv_python_executable()
    if python_executable.is_file():
        return python_executable
    subprocess.run([sys.executable, "-m", "venv", str(VENV_ROOT)], check=True)
    return _venv_python_executable()


def _venv_python_executable() -> Path:
    if sys.platform.startswith("win"):
        return VENV_ROOT / "Scripts" / "python.exe"
    return VENV_ROOT / "bin" / "python"


def _install_core_projects(python_executable: Path) -> None:
    subprocess.run([str(python_executable), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([str(python_executable), "-m", "pip", "install", "-e", str(QUOTEMUX_ROOT)], check=True)
    subprocess.run([str(python_executable), "-m", "pip", "install", "-r", str(MARKETHUB_ROOT / "requirements.txt")], check=True)


def _prepare_workspace() -> None:
    console_root = MARKETHUB_ROOT / 'services' / 'markethub_console'
    source_path = console_root / 'web' / 'index.html'
    target_root = console_root / 'dist'
    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target_root / 'index.html')


def _ensure_workspace_directories() -> None:
    (ROOT / 'datalake').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'cache_payloads').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'data-update').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'env').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'logs').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'package_venvs').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'runtime').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'scripts').mkdir(parents=True, exist_ok=True)
    (RUNTIME_ROOT / 'store').mkdir(parents=True, exist_ok=True)
    _write_default_environment()
    _install_runtime_scripts()


def _write_default_environment() -> None:
    env_path = RUNTIME_ROOT / "env" / "markethub.env"
    if env_path.exists():
        return
    env_path.write_text(
        "\n".join(
            [
                "MARKETHUB_HOST=127.0.0.1",
                "MARKETHUB_PORT=8803",
                f"MARKETHUB_DATA_ROOT={RUNTIME_ROOT / 'store'}",
                "MARKETHUB_DB_HOST=localhost",
                "MARKETHUB_DB_PORT=55432",
                "MARKETHUB_DB_NAME=markethub_dev",
                "MARKETHUB_DB_USER=markethub",
                "MARKETHUB_DB_PASSWORD=markethub_dev_password",
                f"MARKETHUB_RUNTIME_ROOT={RUNTIME_ROOT}",
                f"MARKETHUB_LOG_ROOT={RUNTIME_ROOT / 'logs'}",
                f"MARKETHUB_DATA_UPDATE_ROOT={RUNTIME_ROOT / 'data-update'}",
                f"QUOTEMUX_RUNTIME_ROOT={RUNTIME_ROOT / 'runtime'}",
                f"QUOTEMUX_CACHE_PAYLOAD_ROOT={RUNTIME_ROOT / 'cache_payloads'}",
                f"QUOTEMUX_PACKAGE_VENV_ROOT={RUNTIME_ROOT / 'package_venvs'}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _install_runtime_scripts() -> None:
    script_names = [
        "global-data-update.sh",
        "limit-order-amount-update.sh",
    ]
    for script_name in script_names:
        source_path = MARKETHUB_ROOT / "scripts" / script_name
        target_path = RUNTIME_ROOT / "scripts" / script_name
        shutil.copyfile(source_path, target_path)
        if not sys.platform.startswith("win"):
            target_path.chmod(0o755)


def _build_console() -> None:
    build_script = MARKETHUB_ROOT / "services" / "markethub_console" / "scripts" / "build.ps1"
    if sys.platform.startswith("win"):
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(build_script)], check=True)
        return
    subprocess.run(["pwsh", "-ExecutionPolicy", "Bypass", "-File", str(build_script)], check=True)


def _verify_install(python_executable: Path) -> None:
    subprocess.run([str(python_executable), "-c", "import quotemux; import fastapi"], check=True)


def _print_next_steps(python_executable: Path) -> None:
    print(f"Python: {python_executable}")
    print("启动命令:")
    print(f"  {python_executable} {MARKETHUB_ROOT / 'scripts' / 'run_api.py'}")
    print("启动后访问: http://127.0.0.1:8803/admin")
    print("然后点击: 安装或更新全部 Packages")


if __name__ == "__main__":
    main()
