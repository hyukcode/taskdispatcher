# 发布 / 安装手册

包名：**`multicc`**（`tasker` / `claude-codex-tasker` 在 PyPI 被占用，`multicc` 可用）。安装后命令仍是 **`tasker`**。

## 目标机安装（三种方式）

### A. 从 PyPI 安装（推荐，需联网）
```bash
pip install multicc
# 升级
pip install -U multicc
# 卸载
pip uninstall multicc
```

### B. 离线安装（目标机无网 / 内网）
1. 在**源码机**离线构建 wheel（纯标准库，无需网络 / wheel / setuptools）：
   ```bash
   python scripts/build_wheel.py
   # → dist/multicc-0.6.2-py3-none-any.whl
   ```
2. 把该 `.whl` 拷到目标机：
   ```bash
   pip install multicc-0.6.2-py3-none-any.whl
   ```
   wheel 安装不触发构建、不需要网络。

### C. 源码直接跑（无需安装）
```bash
git clone ... && cd multicc
python tasker_cli.py --help        # 或 python -m tasker --help
```

> 无论哪种方式，目标机都还需另装执行器 CLI（打包不含它们）：
> - Claude Code：`brew install --cask claude`（macOS）或官网安装
> - Codex：`npm install -g @openai/codex`

## 发布到 PyPI

### 1. 构建机一次性准备
```bash
pip install build twine
```
（可选）`twine` 需要凭证：`export TWINE_USERNAME=__token__` + `export TWINE_PASSWORD=pypi-xxxx`，
或用 `~/.pypirc`。

### 2. 发布
```bash
python scripts/publish.py --test        # 先发 TestPyPI 试跑
python scripts/publish.py               # 正式发 PyPI
```
`publish.py` 会自动：离线构建 wheel → 若有 `build` 则补打 sdist → `twine upload`。

### 3. 验证
```bash
pip install multicc==0.6.2
tasker --version
tasker verify-config
```

## 版本 / 改名注意事项

- 包名 `multicc`（PyPI 与 TestPyPI 均可用，已用 HTTP 404 确认）。命令名仍是 `tasker`。
- 版本号唯一维护在 `pyproject.toml`；离线 wheel 脚本会自动读取该版本。
- 发布新版本时 `dist/` 里旧 wheel 会残留，建议 `rm -rf dist build *.egg-info` 后再构建。
