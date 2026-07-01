# oMLX 11335 多模型加载/卸载内存泄露测试报告

测试时间：2026-07-01 08:31-08:39 Asia/Shanghai

测试对象：当前分支 `local/integration`，服务通过 `scripts/start-omlx-app-config.sh --host 127.0.0.1 --port 11335` 在 tmux 会话 `omlx-11335` 中启动。

## 结论

存在可稳定复现的 unload 后进程驻留内存递增问题。API 层面每轮卸载后均显示 `models_loaded=0`、`model_memory_used=0`，但 macOS `vmmap -summary` 显示 `IOAccelerator (graphics)` 以约 2.1GB/轮的速度线性增长。

这不是普通 Python heap 泄露：`MALLOC_LARGE` 在卸载后回到约 52.6MB，`VM_ALLOCATE` 仅从 161.1MB 增至 270.4MB。主要增量全部落在 Metal/IOAccelerator 驱动驻留区。

## 测试模型

正式测试使用 oMLX 当前可见模型 ID：

| 用户指定模型 | 本次实际 oMLX 模型 ID |
| --- | --- |
| Qwen3-4B-Instruct-2507-MLX-4bit | Qwen3-4B-Instruct-2507-MLX-4bit |
| Qwen3-Embedding-4B-4bit-DWQ | Qwen3-Embedding-4B-4bit-DWQ |
| Qwen3-Reranker-0.6B-4bit | Qwen3-Reranker-0.6B-4bit |
| leonsarmiento/Ornith-1.0-35B-5bit-mlx | Ornith-1.0-35B-5bit-mlx |

## 测试方法

1. 停止 tmux 中原服务。
2. 使用同一启动命令重启 11335，得到新 PID `25297`。
3. 重启后服务自动加载默认模型 `Ornith-1.0-35B-5bit-mlx`，正式测试脚本先卸载所有已加载模型并等待 `models_loaded=0`。
4. 记录空载基线。
5. 循环加载 4 个目标模型，记录“全部加载后”内存。
6. 反向卸载 4 个目标模型，等待 `models_loaded=0` 后记录“全部卸载后”内存。

原始样本：

- `/tmp/omlx-memory-test-11335/samples.jsonl`
- `/tmp/omlx-memory-test-11335/samples.csv`（脚本被人工中断，CSV 未必生成；JSONL 为权威记录）

后续复测请使用仓库内脚本与方案：

- `scripts/omlx_memory_cycle_probe.py`
- `docs/omlx-memory-cycle-retest-plan-zh.md`

## 样本数据

脚本被人工停止前完成了 8 轮完整加载/卸载，并进入第 9 轮加载。以下表格只使用 8 轮完整卸载样本。

| 阶段 | Cycle | RSS MB | Physical Footprint MB | IOAccelerator Graphics MB | API model_memory_used MB |
| --- | ---: | ---: | ---: | ---: | ---: |
| clean baseline | 0 | 490.1 | 265.7 | 2.0 | 0.0 |
| after unload | 1 | 2742.9 | 2457.6 | 2150.4 | 0.0 |
| after unload | 2 | 4931.8 | 4608.0 | 4300.8 | 0.0 |
| after unload | 3 | 7108.5 | 6860.8 | 6451.2 | 0.0 |
| after unload | 4 | 9291.0 | 9011.2 | 8601.6 | 0.0 |
| after unload | 5 | 11467.8 | 11161.6 | 10752.0 | 0.0 |
| after unload | 6 | 13644.3 | 13414.4 | 13004.8 | 0.0 |
| after unload | 7 | 15818.9 | 15564.8 | 15155.2 | 0.0 |
| after unload | 8 | 17992.6 | 17715.2 | 17305.6 | 0.0 |

完整加载峰值也随轮次同步抬升：

| Cycle | 全部加载后 RSS MB | 全部加载后 Physical Footprint MB | 全部加载后 IOAccelerator Graphics MB |
| ---: | ---: | ---: | ---: |
| 1 | 29757.0 | 29491.2 | 28672.0 |
| 2 | 31928.7 | 31641.6 | 30822.4 |
| 3 | 34095.3 | 33792.0 | 33075.2 |
| 4 | 36261.9 | 36044.8 | 35225.6 |
| 5 | 38431.7 | 38195.2 | 37376.0 |
| 6 | 40598.2 | 40345.6 | 39526.4 |
| 7 | 42765.7 | 42496.0 | 41676.8 |
| 8 | 44932.5 | 44646.4 | 43827.2 |

## 定量判断

以 8 轮完整卸载样本计算：

- RSS：490.1MB -> 17992.6MB，净增 17502.5MB。
- Physical footprint：265.7MB -> 17715.2MB，净增 17449.5MB。
- IOAccelerator (graphics)：2.0MB -> 17305.6MB，净增 17303.6MB。
- 从第 1 轮卸载后到第 8 轮卸载后，Physical footprint 增长 15257.6MB，约 2179.7MB/轮。
- 每轮 API 均确认 `model_memory_used=0`，说明 oMLX 的模型内存账本已经归零，但进程物理驻留没有归零。

人工停止脚本后，当前服务仍空载：

- `/api/status`: `models_loaded=0`, `model_memory_used=0`
- `vmmap -summary 25297`: `Physical footprint=19.4G`, `IOAccelerator (graphics)=19.0G`

这与第 9 轮被中断前的加载/卸载残留一致。

## 原因分析

### 已排除

1. 不是“模型没有卸载”。每个完整 after-unload 样本 API 都显示 `models_loaded=0`。
2. 不是 oMLX 模型内存计数没有扣减。每个完整 after-unload 样本 API 都显示 `model_memory_used=0`。
3. 不是普通 Python malloc 主导。卸载后 `MALLOC_LARGE` 稳定回到约 52.6MB，`MALLOC_SMALL (empty)` 在 116-148MB 区间波动。

### 高概率原因

卸载路径已经执行了常规清理，但仍无法释放一部分 Metal 驱动层驻留内存：

- `EnginePool._unload_engine()` 在停止 engine 后清空引用、执行 `gc.collect()`，并通过 MLX executor 调用 `mx.synchronize()` + `mx.clear_cache()`。
- `EngineCore.close()` 会在 engine 线程上执行 `scheduler.shutdown`、`scheduler.deep_reset`，随后清空 model/tokenizer/scheduler 引用，并在线程本地 stream 上执行 `_final_engine_thread_reclaim()`。
- `_final_engine_thread_reclaim()` 内部再次执行 `gc.collect()`、`_sync_and_clear_cache(stream)`、`gc.collect()`。

因此，本次现象不是缺少最基本的 `clear_cache` 调用，而是“多 engine/多后端模型重复创建销毁后，仍有与 MLX/Metal stream、compiled graph、model wrapper 或 driver allocation 相关的 GPU allocation 没有回到系统”。

尤其值得注意的是：每轮残留约 2.1GB，远低于四个模型全部加载的 30GB 账面模型内存，但增长非常线性，说明更像是某个固定模型或固定 backend 的卸载残留，而不是随机碎片。需要进一步拆分到单模型测试定位。

## 解决方案

## 追加定位：单模型隔离测试

追加测试时间：2026-07-01 08:50-09:01 Asia/Shanghai。

为缩小范围，每个模型测试前都重启 11335 服务，并在重启后确认空载。测试结果如下：

| 模型 | 轮次 | after-unload Physical Footprint 漂移 | after-unload IOAccelerator Graphics 漂移 | 判断 |
| --- | ---: | ---: | ---: | --- |
| Qwen3-Embedding-4B-4bit-DWQ | 5 | +8601.6MB | +8601.6MB | 线性泄露，主因 |
| Qwen3-Reranker-0.6B-4bit | 5 | +8.3MB | +0.0MB | 可排除 |
| Qwen3-4B-Instruct-2507-MLX-4bit | 5 | +0.0MB（相对第 1 轮卸载后） | +0.0MB（相对第 1 轮卸载后） | 首轮保留约 2.15GB，但后续收敛，不是线性泄露源 |
| Ornith-1.0-35B-5bit-mlx | 3 | +19.3MB | +0.0MB | 可排除 |

单模型原始样本：

- `/tmp/omlx-single-model-tests/Qwen3-Embedding-4B-4bit-DWQ.jsonl`
- `/tmp/omlx-single-model-tests/Qwen3-Reranker-0.6B-4bit.jsonl`
- `/tmp/omlx-single-model-tests/Qwen3-4B-Instruct-2507-MLX-4bit.jsonl`
- `/tmp/omlx-single-model-tests/Ornith-1.0-35B-5bit-mlx.jsonl`

### 关键证据

`Qwen3-Embedding-4B-4bit-DWQ` 单独循环即可复现与多模型测试一致的每轮约 2.15GB 残留：

| Cycle | after-unload RSS MB | after-unload Physical Footprint MB | after-unload IOAccelerator Graphics MB | API model_memory_used MB |
| ---: | ---: | ---: | ---: | ---: |
| baseline | 483.1 | 265.3 | 2.0 | 0.0 |
| 1 | 2649.4 | 2457.6 | 2150.4 | 0.0 |
| 2 | 4811.0 | 4608.0 | 4300.8 | 0.0 |
| 3 | 6974.0 | 6758.4 | 6451.2 | 0.0 |
| 4 | 9137.6 | 8908.8 | 8601.6 | 0.0 |
| 5 | 11300.4 | 11059.2 | 10752.0 | 0.0 |

对应服务日志显示 embedding unload 的内部 MLX active memory 未释放：

- 第 4 轮：`Settle barrier timed out for 'Qwen3-Embedding-4B-4bit-DWQ': freed=0.00B`
- 第 4 轮：`Emergency reclaim failed ... active_memory=8.44GB exceeds safe threshold (5.00GB)`
- 第 5 轮：`Settle barrier timed out ... freed=0.00B`
- 第 5 轮：`Emergency reclaim failed ... active_memory=10.54GB exceeds safe threshold (5.00GB)`

对照组：

- `Qwen3-Reranker-0.6B-4bit` 每轮卸载后 `IOAccelerator (graphics)` 回到 2.0MB，日志中 active memory 回到约 1KB。
- `Ornith-1.0-35B-5bit-mlx` 每轮卸载后 `IOAccelerator (graphics)` 回到 2.0MB，日志中 `freed=23.50GB` 且 active memory 回到约 1KB。
- `Qwen3-4B-Instruct-2507-MLX-4bit` 首轮卸载后保留约 2.15GB，后续加载/卸载不再增长；这是一次性 runtime/cache 驻留，不是本次多模型线性增长的主因。

### 缩小后的根因判断

问题集中在 `Qwen3-Embedding-4B-4bit-DWQ` 的 embedding backend，尤其是 `mlx-embeddings` fallback + `mx.compile` 路径。

代码证据：

- `EmbeddingEngine.start()` 在全局 MLX executor 中调用 `MLXEmbeddingModel.load()`。
- `MLXEmbeddingModel.load()` 对该模型走 `mlx-embeddings` fallback，并调用 `_try_compile()`。
- `_try_compile()` 创建 `_compiled_embed = mx.compile(_compiled_embed)`，其中闭包捕获 `base_model = self.model`。
- `EmbeddingEngine.stop()` 只执行 `self._model = None`、`gc.collect()`、全局 executor 上的 `mx.synchronize()` + `mx.clear_cache()`。

这条链路没有显式释放 `MLXEmbeddingModel.model`、`processor`、`_compiled_embed`，也没有像 `EngineCore.close()` 那样在 engine-owned executor/thread/stream 上执行完整 reclaim。结合 `freed=0.00B` 和每轮新增一个模型体量级别的 `IOAccelerator (graphics)`，高概率是 compiled callable、compile cache 或 `mlx-embeddings` 模型对象仍持有旧模型权重/Metal buffer。

### 更新后的修复优先级

1. 先修 `EmbeddingEngine.stop()` / `MLXEmbeddingModel` teardown，而不是先动 VLM、Reranker 或通用 EngineCore。
2. 为 `MLXEmbeddingModel` 增加显式 `close()` 或 `release_resources()`：
   - 先将 `_compiled_embed = None`
   - 再将 `model = None`
   - 再将 `processor = None`
   - 重置 `_loaded/_is_compiled`
   - 在线程内执行 `gc.collect()`、`mx.synchronize()`、`mx.clear_cache()`
3. 如果 MLX 提供 compile cache 清理 API，应在 embedding stop 中清理当前线程 compile cache；否则优先增加开关，允许对 embedding 禁用 `mx.compile`，验证泄露是否消失。
4. 增加回归测试脚本或手工验收：单独对 `Qwen3-Embedding-4B-4bit-DWQ` 做 5-10 轮 load/unload，after-unload `IOAccelerator (graphics)` 不应继续每轮 +2.15GB。

### 立即规避

1. 当需要长时间稳定运行 11335 时，避免频繁加载/卸载这组模型；对常用模型保持常驻，减少 churn。
2. 如果业务上必须频繁切换模型，应在卸载后监控 `vmmap -summary <pid>` 的 `IOAccelerator (graphics)`，不要只看 `/api/status`。
3. 当空载 `IOAccelerator (graphics)` 超过阈值（例如 8-12GB）时，执行服务级重启；当前证据显示进程重启可以恢复到数百 MB 级空载基线。

### 修复路线

1. 增加单模型循环测试，分别测试四个模型，每个模型至少 5 轮：
   - 只测 `Qwen3-4B-Instruct-2507-MLX-4bit`
   - 只测 `Qwen3-Embedding-4B-4bit-DWQ`
   - 只测 `Qwen3-Reranker-0.6B-4bit`
   - 只测 `Ornith-1.0-35B-5bit-mlx`
   目标是确认每轮约 2.1GB 的残留来自哪个 engine/backend。
2. 在卸载日志中补充 `mx.get_active_memory()`、`mx.get_peak_memory()`、`vmmap IOAccelerator` 快照，避免只依赖 oMLX 内部账本。
3. 审查模型 wrapper 的 backend-specific 释放接口，优先检查 VLM/Batched/Embedding/Reranker 的 `stop()` 是否真正释放 tokenizer、processor、compiled callable、model module、stream-local caches。
4. 对每个 engine executor shutdown 后增加可选强制等待与线程退出验证。当前 `EngineCore.close()` 在 compile-cache 可清理时调用 `shutdown(wait=False)`，这有利于避免卡死，但无法证明线程和 thread-local MLX/Metal 资源已经退出。
5. 如果单模型定位到 MLX/Metal 运行时层而非 oMLX 引用残留，应增加“内存阈值触发 supervised restart”的运维保护，并把 MLX 版本、macOS 版本、复现脚本和 `vmmap` 证据提交给上游。

### 验证标准

修复后重新执行同一测试：

- 重启后先卸载自动加载模型，确认 `models_loaded=0`。
- 完成 10 轮完整循环。
- after-unload 的 `Physical footprint` 和 `IOAccelerator (graphics)` 不应线性增长。
- 建议阈值：第 10 轮 after-unload 相比第 1 轮 after-unload 增长小于 500MB；如果 macOS/MLX 有合理缓存保留，则至少应在前 2-3 轮后收敛，而不能持续每轮增加约 2GB。
