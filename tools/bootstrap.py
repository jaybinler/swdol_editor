"""Prepare a project-local Python environment before launching the Windows editor."""

from pathlib import Path
import argparse
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def ready(python: Path) -> bool:
    """Probe moved virtual environments rather than assuming their executables still work."""
    try:
        result = subprocess.run([str(python), '-c', 'import sys,lzokay;sys.exit(sys.version_info < (3,11))'],
                                capture_output=True, timeout=15)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    """Install pinned dependencies only into this tool's runtime, then forward editor arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-env', action='store_true')
    args, forwarded = parser.parse_known_args()
    try:
        if sys.version_info < (3, 11):
            raise RuntimeError('请安装 Python 3.11 或更新版本，并启用 Python Launcher。')
        python = next((path for path in (ROOT / '.venv/Scripts/python.exe', ROOT / 'output/runtime/venv/Scripts/python.exe')
                       if ready(path)), None)
        if python is None:
            environment = ROOT / 'output/runtime/venv'
            print('正在准备工具专用环境；首次安装依赖需要网络。', flush=True)
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / 'Scripts/python.exe'
            subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')],
                           check=True, timeout=180)
            if not ready(python):
                raise RuntimeError('运行依赖未通过检查，请检查 Python 安装和网络后重试。')
        editor = ROOT / 'editor'
        if not (editor / 'dist/index.html').is_file():
            node = shutil.which('node')
            vite = editor / 'node_modules/vite/bin/vite.js'
            if not node or not vite.is_file():
                raise RuntimeError('缺少前端构建。请使用完整迁移包，或在 editor 目录执行 npm ci 和 npm run build。')
            subprocess.run([node, str(vite), 'build'], cwd=editor, check=True, timeout=120)
        print('运行环境通过：' + str(python), flush=True)
        if args.check_env:
            return 0
        return subprocess.call([str(python), '-X', 'utf8', str(ROOT / 'tools/editor_server.py'), *forwarded], cwd=ROOT)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'环境准备失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
