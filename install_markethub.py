from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
VENV_ROOT = ROOT / ".venv"
QUOTEMUX_ROOT = ROOT / "QuoteMux"
MARKETHUB_ROOT = ROOT / "MarketHub"


def main() -> None:
    _assert_layout()
    python_executable = _ensure_venv()
    _install_core_projects(python_executable)
    _build_console()
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
    subprocess.run([str(python_executable), "-m", "pip", "install", "-r", str(MARKETHUB_ROOT / "requirements.dev.txt")], check=True)


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
