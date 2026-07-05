# 仓库规范指南

## 项目结构与模块组织

`omlx/` 包含 Python 包与服务端实现。关键区域包括：
- `omlx/engine*` — 推理引擎与生命周期管理
- `omlx/models/` — 模型封装
- `omlx/admin/` — 管理面板与 API 路由
- `omlx/cache/` — 缓存实现
- `omlx/eval/` — 内置评测数据

`tests/` 存放单元测试与集成测试。`apps/`、`Formula/` 和 `packaging/`
包含 macOS 应用、Homebrew 公式及发布打包资源。`scripts/` 仅存放仓库
官方提供的工具脚本；本地维护文件应保持在版本跟踪之外。

## 构建、测试与开发命令

- `uv sync --dev`：安装运行时与开发依赖。
- `.venv/bin/omlx serve --host 127.0.0.1 --port 8000`：启动本地服务。
- `.venv/bin/python -m pytest`：运行全部测试。
- `.venv/bin/python -m pytest tests/test_engine_core.py -q`：运行指定测试文件。
- `uv run ruff check .`：执行代码检查。
- `uv run ruff format .` 或 `uv run black .`：格式化 Python 代码。

部分依赖为 git 锁定版本且依赖 MLX；优先使用项目虚拟环境，而非系统 Python。

## 编码风格与命名约定

- Python 代码使用 4 空格缩进，行宽不超过 88 字符。
- 类名使用 `PascalCase`，函数和变量使用 `snake_case`，测试函数以 `test_` 开头。
- 新增抽象前先参考已有的引擎/模型模式。
- 注释保持简洁，仅对非显而易见的生命周期、线程、缓存或 MLX 行为进行说明。

## 测试规范

- 使用 `pytest` + `pytest-asyncio`，测试文件位于 `tests/`。
- 对涉及生命周期、缓存、解析、API 行为变更的代码，添加针对性的回归测试。
- 开发阶段使用精准命令，在影响范围交叉时再运行更广的测试。
- 部分 MLX 测试需要 Apple Silicon/Metal；在 PR 说明中标注跳过或本地受阻的验证项。

## 提交与 Pull Request 规范

- commit 信息使用简洁的祈使句，必要时加约定前缀，如 `fix: release embedding MLX resources on unload` 或 `docs: update memory retest report`。
- 提交个人仓库时，commit message 必须使用中文；向 GitHub 上游提交 PR 时根据源仓库语言提交。
- 上游 PR 应聚焦、仅包含代码变更，除非明确要求文档更新。
- PR 描述应包含：摘要、测试命令、相关验证证据、关联 issue 或后续 PR。

## 分支职责与同步规则

- `mirror` 只作为源仓库镜像分支，跟踪 `upstream/main`。维护时仅允许
  `git switch mirror && git merge --ff-only upstream/main`；不要在该分支提交
  本地修改，也不要让它跟踪 fork 或本地远程。
- `main` 是本机集成分支，跟踪 `github-fork/main`。本地修复、已验证的集成
  提交和需要推送到个人 fork 的工作应汇入 `main`。
- `local/integration` 仅作为历史过渡分支使用；若它与 `main` 指向同一提交，
  不应继续在其上开发新工作。
- 临时 `fix/*` 分支在上游或 `main` 已通过祖先关系或 patch-id 等价合入后
  应清理。删除前先用 `git cherry -v main <branch>` 或等价 diff 核验；若
  只是 patch-id 等价而非祖先关系，才考虑 `git branch -D`。
- 同步顺序建议：先更新 `mirror`，再把 `mirror` 合入 `main`，最后从 `main`
  切出新的修复分支或推送到 `github-fork/main`。

## 安全与配置提示

- 不提交 API 密钥、本地模型路径、生成的报告或机器特定的启动脚本。
- 使用 `.git/info/exclude` 排除仅本地使用的目录，如 `/local-omlx/`。

## local-omlx/ — 本地维护仓库

`omlx/local-omlx/` 是一个独立的 Git 仓库，用于跟踪本地产生的
修改与维护记录。关键约束：

1. **仅本地使用** — 不推送到任何远程仓库，不涉及上游 PR。
2. **独立 git 状态** — 该目录有自己的 `.git`，与项目主仓库的 `git status`
   互不影响；`local-omlx/` 的内容也不会出现在主仓库的 diff 中。
3. **用途** — 保存临时排查脚本、本地测试报告、诊断文档、未提交的
   工作区变更等。适合需要版本追溯但不愿污染主仓库历史的场景。
4. **agent 行为** — 当 agent 在该目录中执行 git 操作（add/commit/log/diff）
   时，应使用 `cd local-omlx && git …` 明确限定作用域；
   不得将其误认为主仓库的分支或暂存区。

示例命令：
```bash
cd local-omlx
git add -A && git commit -m "本地修改摘要"
git log --oneline          # 查看变更记录
```
