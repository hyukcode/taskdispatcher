"""离线构建入口（配合 --no-build-isolation 使用，避免联网装 setuptools/wheel）。

用法（在项目根目录）：
  python setup.py --version              # 只读版本
  SETUPTOOLS_USE_DISTUTILS=stdlib python setup.py sdist bdist_wheel --dist-dir dist
"""
from setuptools import setup

# 元数据集中在 pyproject.toml；这里只做最低限度，保证离线 -e 安装可用。
setup()
