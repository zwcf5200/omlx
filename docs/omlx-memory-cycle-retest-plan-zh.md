# oMLX 模型加载/卸载内存驻留复测方案

本文档用于复测 11335 服务在模型反复加载/卸载后的内存驻留问题，重点验证 `Qwen3-Embedding-4B-4bit-DWQ` 的 embedding backend 是否仍然每轮泄露约 2.15GB `IOAccelerator (graphics)`。

## 前置条件

- 当前分支已安装可运行的本地 editable 环境。
- oMLX 使用 `scripts/start-omlx-app-config.sh --host 127.0.0.1 --port 11335` 启动。
- 测试前必须重启服务，避免旧的 Metal/MLX 驻留污染基线。
- 测试脚本需要 admin API key：

```bash
export OMLX_API_KEY='<admin-api-key>'
```

如果服务不是 `http://127.0.0.1:11335`，同时设置：

```bash
export OMLX_BASE_URL='http://127.0.0.1:11335'
```

## 标准重启步骤

在 tmux 会话 `omlx-11335` 中重启服务：

```bash
tmux send-keys -t omlx-11335 C-c
sleep 5
tmux send-keys -t omlx-11335 'scripts/start-omlx-app-config.sh --host 127.0.0.1 --port 11335' Enter
```

确认新进程监听：

```bash
lsof -nP -iTCP:11335 -sTCP:LISTEN
curl -sS -H "Authorization: Bearer $OMLX_API_KEY" \
  http://127.0.0.1:11335/api/status
```

注意：服务可能会自动加载默认模型。复测脚本默认会在正式采样前卸载所有已加载模型，并等待 `models_loaded=0`。

## 脚本

统一脚本：

```bash
.venv/bin/python scripts/omlx_memory_cycle_probe.py --help
```

脚本输出：

- JSONL：逐阶段原始样本
- CSV：同样本的表格版本
- summary JSON：漂移摘要

默认输出目录：

```text
/tmp/omlx-memory-cycle-probe
```

每条样本包含：

- `/api/status` 中的 `models_loaded`、`loaded_models`、`model_memory_used`
- `ps` RSS
- `vmmap -summary` 中的 `Physical footprint`
- `vmmap -summary` 中的 `IOAccelerator (graphics)`
- `MALLOC_LARGE`、`MALLOC_SMALL (empty)`、`VM_ALLOCATE`

## 复测 A：Embedding 单模型定位

目的：验证已定位的主因是否仍然存在。

重启服务后执行：

```bash
.venv/bin/python scripts/omlx_memory_cycle_probe.py \
  --model Qwen3-Embedding-4B-4bit-DWQ \
  --rounds 5 \
  --label embedding-qwen3-4b-dwq
```

已知异常签名：

- 每轮 after-unload 都是 `models_loaded=0`、`model_memory_used_mb=0.0`
- `IOAccelerator (graphics)` 每轮约 +2150MB
- 5 轮后 after-unload `IOAccelerator (graphics)` 约 10752MB
- 服务日志出现 `Settle barrier timed out ... freed=0.00B`
- 服务日志出现 `Emergency reclaim failed ... active_memory=...`

修复后验收标准：

- after-unload 的 `IOAccelerator (graphics)` 不再每轮线性增长。
- 建议 5 轮后相对第 1 轮 after-unload 漂移小于 500MB。
- 如果 MLX/Metal 保留一次性缓存，必须在前 1-2 轮后收敛。

## 复测 B：对照模型

目的：确认修复没有破坏其它 backend 的卸载行为。

每个模型测试前都重启服务。

Reranker：

```bash
.venv/bin/python scripts/omlx_memory_cycle_probe.py \
  --model Qwen3-Reranker-0.6B-4bit \
  --rounds 5 \
  --label reranker-qwen3-06b
```

Instruct：

```bash
.venv/bin/python scripts/omlx_memory_cycle_probe.py \
  --model Qwen3-4B-Instruct-2507-MLX-4bit \
  --rounds 5 \
  --label instruct-qwen3-4b
```

Ornith：

```bash
.venv/bin/python scripts/omlx_memory_cycle_probe.py \
  --model Ornith-1.0-35B-5bit-mlx \
  --rounds 3 \
  --label ornith-35b-5bit
```

已知基线：

- `Qwen3-Reranker-0.6B-4bit`：5 轮 after-unload `IOAccelerator (graphics)` 漂移 0MB。
- `Ornith-1.0-35B-5bit-mlx`：3 轮 after-unload `IOAccelerator (graphics)` 漂移 0MB。
- `Qwen3-4B-Instruct-2507-MLX-4bit`：首轮可能保留约 2.15GB，但后续不继续累加。

## 复测 C：四模型组合回归

目的：验证真实组合场景是否修复。

重启服务后执行：

```bash
.venv/bin/python scripts/omlx_memory_cycle_probe.py \
  --default-suite \
  --rounds 10 \
  --label four-model-suite-11335
```

四模型组合包含：

- `Qwen3-4B-Instruct-2507-MLX-4bit`
- `Qwen3-Embedding-4B-4bit-DWQ`
- `Qwen3-Reranker-0.6B-4bit`
- `Ornith-1.0-35B-5bit-mlx`

修复前异常签名：

- 8 轮完整卸载后，`IOAccelerator (graphics)` 从 2.0MB 增至 17305.6MB。
- 增长约 2.1GB/轮。
- `/api/status` 每轮 after-unload 仍显示 `models_loaded=0`、`model_memory_used=0`。

修复后验收标准：

- 10 轮 after-unload 不应线性增长。
- 建议第 10 轮 after-unload 相比第 1 轮 after-unload 的 `IOAccelerator (graphics)` 漂移小于 500MB。
- 如果存在一次性 runtime/cache 驻留，应在前 1-2 轮后收敛。

## 结果归档

建议将每次复测输出目录复制或移动到 repo 外的时间戳目录，例如：

```bash
mkdir -p /tmp/omlx-memory-retention-runs/$(date +%Y%m%d-%H%M%S)
cp -p /tmp/omlx-memory-cycle-probe/* /tmp/omlx-memory-retention-runs/$(date +%Y%m%d-%H%M%S)/
```

若要把关键结论写入仓库文档，更新：

```text
docs/omlx-11335-model-cycle-memory-leak-report-zh.md
```

至少记录：

- commit/branch
- macOS 版本
- MLX / mlx-lm / mlx-embeddings 版本
- 服务启动命令
- 测试模型
- 轮次数
- summary JSON 中的 drift 数值
- 服务日志中的 unload settle 结果
