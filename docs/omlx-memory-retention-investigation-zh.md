# oMLX 卸载内存保留问题调查

日期：2026-06-26

## 问题

观察到的 oMLX 后端状态与 UI 和管理缓存表面不一致：

- UI 显示无活跃任务，热缓存为空。
- 仅加载了一个模型：`Qwen3.6-35B-A3B-nvfp4`。
- `/api/status` 报告无活跃或等待中的请求。
- 进程仍然持有非常大的驻留内存 footprint：`64.8G`。
- `vmmap -summary` 将大部分内存归因于 Metal/IOAccelerator：
  `IOAccelerator (graphics): 61.9G`。

清空热缓存没有回收任何内存：

- `/admin/api/hot-cache/clear`：`total_cleared=0`，`bytes_reclaimed=0`。

卸载已加载的模型释放了模型权重内存，但并非所有 Metal 内存都被回收：

- `POST /v1/models/Qwen3.6-35B-A3B-nvfp4/unload`
- 日志结果：`freed=19.01GB`，`active_memory: 42.89GB (settled)`。
- 卸载后，`/api/status` 显示 `models_loaded=0` 和 `model_memory_used=0B`。
- `vmmap` 仍显示 footprint 约 `45.6G`，其中 `IOAccelerator 42.9G`。

重启服务器释放了内存：

- 新进程 footprint：约 `109.5M`。
- `IOAccelerator`：约 `48K`。

这证明了保留的内存既不是热缓存、也不是模型权重，更不是以已加载模型形式可见的内容。而是引擎卸载后进程生命周期的 Metal/MLX 内存保留。

相关上游 issue 评论：

- https://github.com/jundot/omlx/issues/1691#issuecomment-4809293896

该评论记录了 oMLX `0.4.4` 上同类内存保留问题，但有一个重要澄清：泄漏并不要求预填充失败或 mid-stream fallback。在该复现中，一次成功的长上下文补全同样留下了大量 Metal 分配：

- 请求正常完成，日志中有 `prompt: 40338` 和 `finish_reason=stop`。
- oMLX 报告 `active_requests=0`、`waiting_requests=0`，且仅有一个已加载模型，模型内存账面约 `19.95GB`。
- 进程级 `vmmap -summary` 仍显示 `64.8G` 物理 footprint，以及 `61.9G` 的 `IOAccelerator (graphics)`。
- 先卸载 embedding 模型，再卸载主模型后，`models_loaded=0` 且 `model_memory_used_formatted=0B`，但 `vmmap` 仍显示 `45.6G` 物理 footprint 和 `42.9G` 的 `IOAccelerator (graphics)`。
- `POST /admin/api/hot-cache/clear` 回收了 `0` 字节。
- `lsof` 未显示模型权重文件仍被打开。
- 重启 Python/oMLX 进程后，footprint 降至约 `109.5M`，`IOAccelerator` 降至 `48K`。

这将本地调查与上游 issue `#1691` 连接起来：保留内存并不限于某一条错误路径。共同特征是进程生命周期内的 MLX/Metal 分配，不再以已加载模型、热缓存条目或活跃请求形式可见。

## 日志中的证据

该事件发生在一个长上下文生成之后，伴随重复的分块预填充内存压力：

- 大量日志行显示 `Chunked prefill above max_bytes... 68.xGB > 67.9GB`。
- 在 `2026-06-26 17:10:29`，oMLX 存储了一个边界缓存快照：
  `storing 28672/40512 tokens`。
- 同一补全报告：
  `Chat completion: 174 tokens in 183.43s, prompt: 40338`。

之后，在模型卸载后：

- 调度器关闭/深度重置完成。
- oMLX 日志显示 `freed=19.01GB`。
- MLX 活跃内存仍为 `42.89GB`。

这表明大型临时/前缀缓存/预填充 Metal 分配在引擎生命周期内存活，即使在请求和模型状态通过管理 API 不可见之后。相关的 `#1691` 复现强化了这一解释：它在一次成功请求之后、卸载所有真实模型之后、且 hot-cache 回收返回 `0` 字节之后，仍观察到同样的 `IOAccelerator` footprint 保留。

## 分析

根本原因很可能是 `EngineCore.close()` 中的拆除顺序和 executor/流所有权 bug。

补丁前的相关行为：

- 每个引擎拥有自己的 MLX executor 和线程局部 MLX stream。
- 调度器关闭和深度重置通过该引擎 executor 运行。
- 之后，`EngineCore.close()` 清除 MLX 编译缓存并关闭 executor。
- 只有在处理完 executor 之后，才清除输出收集器并将 `self.model`、`self.tokenizer` 和 `self.scheduler` 设为 `None`。
- 在丢弃这些引用后，没有对引擎工作线程执行最终的 `mx.synchronize(stream)` 加 `mx.clear_cache()` 操作。
- 其他管理/全局清空路径使用全局 MLX executor，而非可能持有保留缓冲区的引擎 executor/stream。

还存在一个微妙的引用保留问题：

- `for fn in (self.scheduler.shutdown, self.scheduler.deep_reset)` 循环后，局部变量 `fn` 仍绑定到最后一个调度器方法。
- 该绑定的方法可以在 `close()` 返回前保持调度器对象存活。
- 在丢弃此引用之前执行最终的 GC/回收，无法看到真正可回收的对象图。

为什么这匹配运行时证据：

- 模型卸载释放了约 `19GB`，与模型权重被丢弃一致。
- 剩余的 `42.9GB` 是 Metal/IOAccelerator 内存，与大型预填充/缓存/临时 MLX 缓冲区一致。
- 重启释放了所有内容，证明它是进程本地的保留 MLX/Metal 状态，而非外部文件或热缓存条目。
- 上游 `#1691` 评论显示，不发生 in-flight 请求失败时也会出现同样模式，因此修复应针对正常的引擎拆除和工作线程回收路径，而不是只清理 failed-prefill 路径。

实时验证还暴露了第二个 VLM 特定的引用保留路径：

- `EngineCore.model` 对于 VLM 引擎是一个 `VLMModelAdapter`。
- 该 adapter 同时保留了 `_vlm_model` 和 `_language_model` 的强引用。
- `VLMBatchedEngine` 还在包装器侧保留了 VLM 模型、processor、adapter、tokenizer 和 drafter 的引用，直到 `EngineCore.close()` 之后。
- 第一次补丁运行，在 `EngineCore.close()` 中添加了最终工作线程回收，卸载后仍保留了约 `19.0GB` 的
  `IOAccelerator (graphics)`。
- 仅在 `EngineCore.close()` 之前丢弃包装器引用本身不够，因为 `EngineCore.model` 内的 adapter 仍然持有原始模型对象。

因此最终修复需要两者兼顾：

- 在引擎自有引用丢弃之后，在工作线程上执行最终回收；以及
- 在执行最终回收之前，显式释放 VLM adapter/包装器引用。

次要观察：

- `VLMBatchedEngine.has_active_requests()` 仅检查输出收集器。
- 即使存在调度器侧的异步清理或延迟删除，它也可能报告无活跃请求。
- 这可能导致 UI 在调度器/MLX 内存生命周期完全稳定之前更早显示空闲。这可能不是卸载后残留内存的主要原因，但会向用户隐藏清理活动。

## 当前补丁

更改的文件：

- `omlx/engine_core.py`
- `omlx/engine/vlm.py`
- `omlx/models/vlm.py`
- `tests/test_per_engine_threads.py`
- `tests/test_engine_core.py`
- `tests/test_vlm_model_adapter.py`

补丁意图：

1. 添加 `_final_engine_thread_reclaim(stream)`。
2. 在该辅助函数中：
   - 执行 `gc.collect()`；
   - 调用调度器的 `_sync_and_clear_cache(stream)`；
   - 再次执行 `gc.collect()`。
3. 在 `EngineCore.close()` 中：
   - 在引擎 executor 上执行调度器关闭和深度重置；
   - 清除残留的绑定方法局部变量 `fn`；
   - 关闭并分离 SSD 缓存管理器；
   - 清除输出收集器和请求侧的流状态；
   - 如果存在，调用模型特定的 `release_resources()`；
   - 将 `self.model`、`self.tokenizer` 和 `self.scheduler` 设为 `None`；
   - 将 `_final_engine_thread_reclaim(self._mlx_stream)` 提交到同一引擎 executor；
   - 然后根据现有编译缓存策略，清除 MLX 编译缓存并关闭或不朽化 executor。
4. 在 `VLMBatchedEngine.stop()` 中：
   - 在关闭内部引擎之前丢弃包装器侧引用。
5. 在 `VLMModelAdapter.release_resources()` 中：
   - 在最终工作线程回收之前，丢弃待处理的 VLM 数组和原始 `_vlm_model` / `_language_model` 引用。

重要的设计选择是：最终回收在引擎自有引用被丢弃之后执行，并且在拥有 MLX 分配的是同一工作线程/流上执行。

测试更改：

- `test_close_clears_compile_cache_then_shuts_down` 现在断言：
  - 模型特定的 `release_resources()` 在最终回收之前运行；
  - 最终回收在编译缓存清除之前调用；
  - 到最终回收执行时，`engine.model`、`engine.tokenizer` 和 `engine.scheduler` 已为 `None`。
- `test_close_fatal_exits_when_compile_cache_clear_times_out` 现在考虑编译缓存清除之前的额外 executor 提交。
- `test_release_resources_drops_model_references` 断言 VLM adapter 丢弃原始模型和待处理数组引用。

## 验证更新

初始直接测试命令：

```bash
uv run pytest tests/test_per_engine_threads.py tests/test_engine_core.py
```

在准备依赖时失败：

```text
Failed to download mlx-metal==0.31.2
Failed to extract archive: mlx_metal-0.31.2-py3-none-macosx_26_0_arm64.whl
I/O operation failed during extraction
Failed to download distribution due to network timeout.
Try increasing UV_HTTP_TIMEOUT (current value: 30s).
```

然后将测试移至 tmux：

```bash
tmux new -A -d -s omlx
tmux send-keys -t omlx 'cd /Users/zhouwei/code/zw/omlx && UV_HTTP_TIMEOUT=300 uv run pytest tests/test_per_engine_threads.py tests/test_engine_core.py 2>&1 | tee /tmp/omlx-pytest.log' C-m
```

该命令运行了数分钟，无 stdout 输出，且 `/tmp/omlx-pytest.log` 仍为空，因此被中断并拆分为环境设置加测试执行。

当前 tmux 命令：

```bash
cd /Users/zhouwei/code/zw/omlx && UV_HTTP_TIMEOUT=300 uv sync 2>&1 | tee /tmp/omlx-uv-sync.log
```

截至最新检查：

- `uv sync` 仍在运行。
- `/tmp/omlx-uv-sync.log` 仍为 `0` 字节。
- `lsof -p <uv-pid>` 显示 `uv` 处于活跃状态，未死锁：
  - 已打开 HTTPS 连接；
  - 正在 `~/.cache/uv` 下写入临时文件；
  - 观察到的文件包括 `mlx/lib/mlx.metallib` 和 `cmake`。

因此原始阻塞点是依赖下载/提取速度或网络行为，而非已知的代码/测试失败。

由于 GitHub 依赖的完整 `uv sync` 太慢，验证路径缩小为目标测试：

1. 停止缓慢的 `uv sync` / `git fetch` 进程。
2. 使用现有的 `.venv`。
3. 仅从清华 PyPI 镜像安装这些测试所需的最小包：

```bash
uv pip install --python .venv/bin/python \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  pytest pytest-asyncio mlx==0.31.2 fastapi uvicorn numpy pyyaml \
  requests psutil setproctitle transformers tokenizers huggingface-hub \
  jinja2 itsdangerous pillow rich sentencepiece tiktoken protobuf tqdm \
  jsonschema python-multipart tabulate socksio

uv pip install --python .venv/bin/python \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  mlx-lm mlx-vlm
```

注意：

- 国内 PyPI 镜像解决了 wheel/包下载瓶颈。
- 它不替换 `pyproject.toml` 中 `git+https://github.com/...` 的依赖；这些仍需 GitHub 或显式 Git 镜像。
- 在沙箱内运行 MLX 导入失败：
  `RuntimeError: [metal::load_device] No Metal device available`。
- 在沙箱外运行相同的测试命令允许 MLX 访问 Metal。

目标验证通过：

```bash
.venv/bin/python -m pytest tests/test_per_engine_threads.py tests/test_engine_core.py
```

结果：

```text
78 passed in 4.31s
```

VLM adapter/包装器引用释放补丁后，更窄的目标套件也通过：

```bash
.venv/bin/python -m pytest \
  tests/test_vlm_model_adapter.py::TestVLMModelAdapter::test_release_resources_drops_model_references \
  tests/test_vlm_engine.py::TestStopSafety \
  tests/test_per_engine_threads.py::TestPerEngineExecutor::test_close_clears_compile_cache_then_shuts_down \
  tests/test_engine_core.py::TestEngineCoreClose
```

结果：

```text
9 passed in 1.33s
```

`git diff --check` 也通过。

## 端口 11445 上的实时验证

验证服务器：

```bash
.venv/bin/python -m omlx.cli serve \
  --host 127.0.0.1 \
  --port 11445 \
  --base-path /tmp/omlx-patched-validation2 \
  --model-dir /Users/zhouwei/.omlx/models \
  --api-key validation-key \
  --hf-endpoint https://hf-mirror.com \
  --paged-ssd-cache-dir /tmp/omlx-patched-validation2/cache \
  --paged-ssd-cache-max-size 20GB \
  --hot-cache-max-size 10GB \
  --memory-guard-gb 76 \
  --log-level info
```

请求前的基线：

- `/api/status`：`models_loaded=0`，`model_memory_used=0B`。
- `vmmap`：物理 footprint `102.3M`。
- `vmmap`：`IOAccelerator (graphics)` 驻留 `48K`。

长上下文请求：

- 模型：`Qwen3.6-35B-A3B-nvfp4`。
- 提示 tokens：`51419`。
- 缓存提示 tokens：`0`。
- 补全 tokens：`32`。
- 请求状态：`200`。
- 客户端 elapsed wall time：`37.99s`。

卸载前：

- `/api/status`：`models_loaded=1`。
- `/api/status`：`model_memory_used_formatted=19.95GB`。
- `vmmap`：物理 footprint `23.6G`。
- `vmmap`：`IOAccelerator (graphics)` 驻留 `20.5G`。

卸载后：

- `POST /v1/models/Qwen3.6-35B-A3B-nvfp4/unload` 返回：
  `{"status":"ok","model_id":"Qwen3.6-35B-A3B-nvfp4"}`。
- `/api/status`：`models_loaded=0`，`model_memory_used=0B`。
- `vmmap`：物理 footprint `569.7M`。
- `vmmap`：`IOAccelerator (graphics)` 驻留 `2832K`。
- 服务器日志：
  `Unloaded model: Qwen3.6-35B-A3B-nvfp4, freed=20.41GB (expected>=17.95GB), active_memory: 1.02KB (settled)`。

这验证了补丁在复现泄漏的相同长预填充场景中释放了之前保留的 Metal 内存。剩余的进程 footprint 是正常的运行时/库分配器残留，而非之前的保留 GPU 分配。

## 后续：范围收窄和重复 bge reranker 循环

日期：2026-06-27

VLM 卸载修复验证后，单独测试了第二个内存保留症状：

- 反复加载和卸载 `bge-reranker-v2-m3` 可能导致每个循环进程物理 footprint 增加数十 MB 到约一百 MB。
- 在这些运行中，卸载后 `/v1/models/status` 返回 `current_model_memory=0`。
- `vmmap` 显示卸载后 `IOAccelerator (graphics)` 回到约 `1-2MB`。
- 增长归因于 macOS malloc 类别，尤其是 `MALLOC_LARGE (empty)`。

这与原始 VLM/长预填充泄漏机制不同：

- 原始 bug 在 VLM 引擎卸载后保留 MLX/Metal 内存。
- bge reranker 循环在模型已释放后保留分配器拥有的空堆页。

测试了多个更窄的假设：

- 禁用 reranker `mx.compile` **未**阻止循环 footprint 增长。
- 运行时调用 macOS `malloc_zone_pressure_relief()` 不是可靠修复，已从最终代码路径中移除。
- 以 `MallocSpaceEfficient=1` 启动进程确实在重复 bge 加载/卸载测试中阻止了增长。

使用 `MallocSpaceEfficient=1` 的验证：

- 隔离 `11445` 服务，6 次 bge 循环：
  - 卸载后：`101.2M`，`102.6M`，`103.0M`，`103.4M`，`103.9M`，`104.3M`。
- 通过本地源启动脚本重启的现有 `11335` 服务，3 次 bge 循环：
  - 卸载后：`102.8M`，`103.2M`，`103.6M`。

最终范围决策：

- 上游 PR 仅包含 VLM/EngineCore 拆除修复（对应 issue 级 MLX/Metal 保留问题）。
- 本地源启动脚本在重复模型加载/卸载循环中保持 `MallocSpaceEfficient=1` 作为 macOS 部署策略。
- 运行时 malloc 压力释放 helper 被移除，因为它增加了 ctypes 复杂度却未解决重复 bge footprint 增长。
- Reranker `mx.compile` 默认保持启用。可使用 `OMLX_RERANKER_COMPILE=0` 进行排障禁用，但禁用不属于内存修复的一部分。

干净的 upstream PR：

- PR: https://github.com/jundot/omlx/pull/2010
- Issue: https://github.com/jundot/omlx/issues/2004
- 相关 issue 复现：
  https://github.com/jundot/omlx/issues/1691#issuecomment-4809293896
- 范围：
  - `EngineCore.close()` 在最终工作线程 MLX 回收之前丢弃 model/tokenizer/scheduler 引用。
  - `VLMBatchedEngine.stop()` 在关闭内部引擎之前丢弃包装器侧 VLM 引用。
  - `VLMModelAdapter.release_resources()` 在最终回收之前丢弃 VLM 拥有的数组和原始模型引用。
- 明确排除在 PR 之外：
  - 本地安装文档和 `pyproject.toml` `file://` 依赖路径；
  - `.codebase-memory` 工件；
  - 本地源启动脚本；
  - macOS `MallocSpaceEfficient=1` 部署设置；
  - 运行时 malloc 压力释放 helper。

## 建议的后续步骤

1. 如果后续仍需要完整依赖同步，优先使用国内 PyPI 镜像获取 wheel 包，但单独处理 GitHub 依赖：

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync
```

2. 对于此补丁，目标测试已通过：

```bash
.venv/bin/python -m pytest tests/test_per_engine_threads.py tests/test_engine_core.py
```

3. 运行基本 diff 卫生检查：

```bash
git diff --check
```

补丁后已通过。

4. 端口 `11445` 上的实时内存验证已通过；补丁后卸载路径将 `IOAccelerator (graphics)` 从 `20.5G` 降至 `2832K`。

5. 考虑后续 UI/管理修复：

- 使活跃请求报告包含调度器清理状态，如待处理异步删除、进行中的 store futures 或延迟清空状态。
- 这不替代内存修复，但会使"空闲"状态在清理期间更真实。

## 历史阻塞命令

以下命令属于阻塞的完整同步路径，在此保留以供审计。

检查 tmux `uv sync` 命令是否完成：

```bash
tmux capture-pane -pt omlx -S -220
ls -l /tmp/omlx-uv-sync.log
```

如果 `uv sync` 仍在运行且输出为空，检查活跃进程：

```bash
ps aux | rg '[u]v sync|[u]v run pytest'
lsof -p <uv-pid>
```

`uv sync` 完成后，单独运行目标测试：

```bash
UV_HTTP_TIMEOUT=300 uv run pytest tests/test_per_engine_threads.py tests/test_engine_core.py
```

或在现有 tmux 会话中：

```bash
tmux send-keys -t omlx 'cd /Users/zhouwei/code/zw/omlx && UV_HTTP_TIMEOUT=300 uv run pytest tests/test_per_engine_threads.py tests/test_engine_core.py 2>&1 | tee /tmp/omlx-pytest.log' C-m
```

## 2026-06-27 后续：单实例 cache 域和最终卸载治理验证

本节记录后续本地分支验证，范围不同于上游 PR `#2010`。目标是验证两个本地任务：

- 单 oMLX 实例内的 cache domain 策略和 prefix cache 复用；
- 所有模型卸载后的 cache 自愈治理、MLX 清理和 maintenance gate。

验证服务：

```bash
scripts/start-omlx-app-config.sh --host 127.0.0.1 --port 11445
```

验证结果：

- 源码服务在 `127.0.0.1:11445` 启动，base path 为 `~/.omlx`，SSD cache 为 `~/.omlx/cache`，hot cache 为 `10GB`。
- 依次加载：
  - `Qwen3-Embedding-0.6B-8bit`
  - `Qwen3-Reranker-0.6B-4bit`
  - `Qwen2.5-0.5B-Instruct-MLX-4bit`
- 三个模型同时加载后，状态显示 `models_loaded=3`，模型内存约 `1.22GB`。
- 对小 LLM 使用稳定长前缀连续请求：
  - 第一次：`prompt_tokens=2725`，`cached_tokens=0`；
  - 第二次：`prompt_tokens=2725`，`cached_tokens=2560`，`cache_efficiency=46.3`。
- 卸载 embedding 和 reranker 时仍有 LLM 存活，只执行单模型卸载，不触发全模型治理。
- 卸载最后一个 LLM 后触发最终治理：
  - `Cache domain janitor freed 3.25 GB across 32 files`
  - `All-models-unloaded maintenance complete: ssd_freed=3.25GB, hot_cache_preserved=True`
  - API 状态回到 `models_loaded=0`、`model_memory_used_formatted=0B`、无 active/waiting request。
- 治理后立即执行真实 `/v1/rerank` 请求，`Qwen3-Reranker-0.6B-4bit` 正常加载并返回相关性排序结果，说明 maintenance gate 完成后业务可以继续进入。
- 再次卸载最后一个 reranker 后，治理可重复触发：
  - `Cache domain janitor freed 3.17 GB across 32 files`
  - `All-models-unloaded maintenance complete: ssd_freed=3.17GB, hot_cache_preserved=True`
  - API 状态再次回到 `models_loaded=0`、模型内存 `0B`。

额外发现：

- `/admin/api/models/{model}/load` 支持 Bearer API key 或 admin session。
- `/admin/api/models/{model}/unload` 当前只接受 admin session cookie。真机验证通过 `/admin/auto-login` 使用主 API key 换取临时 admin cookie 后执行卸载。
- 本次验证没有改变上游 PR `#2010` 的范围；cache domain 和 final unload governance 属于本地分支后续增强。
