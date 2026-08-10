"""独立入口。

- `python tasker_cli.py <命令> ...` 可直接运行（不必 pip install）。
- PyInstaller 用本文件作为打包入口（main.py 内部是相对导入，不能直接当脚本）。
"""
from tasker.main import main

if __name__ == "__main__":
    raise SystemExit(main())
