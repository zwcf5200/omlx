# oMLX 架构与缓存命中率机制

日期：2026-06-27

## 项目结构

oMLX 是一个本地模型服务，核心路径由 API 层、引擎池、调度器、模型适配层和缓存层组成。

- `omlx/cli.py`：命令行入口，`serve` 子命令负责启动服务并接入全局配置。
- `omlx/server.py`、`omlx/api/`：HTTP API、OpenAI/Anthropic 兼容接口、管理接口和状态查询。
- `omlx/engine_pool.py`、`omlx/engine_core.py`、`omlx/engine/`：模型加载、请求分发、引擎生命周期、VLM/embedding/reranker 等引擎封装。
- `omlx/scheduler.py`：请求排队、prefill/decode 调度、prefix cache 接入、内存保护和请求清理。
- `omlx/models/`：模型适配层，屏蔽 LLM、VLM、embedding、reranker 的加载和推理差异。
- `omlx/cache/`：KV/prefix cache、paged SSD cache、hot cache、缓存统计和缓存序列化逻辑。
- `omlx/settings.py`、`omlx/config.py`、`omlx/model_settings.py`：全局配置、CLI 参数映射和单模型配置。
- `omlx/admin/`：管理 UI 和管理 API 静态资源。
- `tests/`：单元测试、集成测试和回归测试。

## 请求路径

典型文本生成请求路径：

1. API 层解析请求，生成 `Request`。
2. `EnginePool` 选择或加载对应模型引擎。
3. `EngineCore` 将请求交给 `Scheduler`。
4. `Scheduler._prepare_prefix_cache_for_request()` 查询 prefix cache。
5. 命中时复用已缓存 KV block，只对剩余 token 做 prefill。
6. 未命中或重建失败时，从完整 prompt 重新 prefill。
7. decode 产生输出 token，完成后调度器清理请求并尝试保存可复用 cache block。

VLM 请求在 token 序列之外还会带图像相关 extra keys。extra keys 会进入 block hash，因此相同文本但不同图片不会误命中。

## 缓存分层

当前项目的主要缓存机制分三层：

- Prefix/block cache：`PagedCacheManager` 以固定 block size 对 prompt token 分块，并用链式 hash 匹配连续前缀。
- Paged SSD cache：`PagedSSDCacheManager` 将可复用 KV block 序列化到 SSD，服务重启或模型重新加载后仍可复用。
- Hot cache：SSD cache 的内存加速层，热 block 以 LRU 管理，避免重复从 SSD 读同一批 block。

调度器查询缓存时先通过 `BlockAwarePrefixCache.fetch_cache()` 找共享前缀；找到 block 后再 `preload_blocks()` 和 `reconstruct_cache()` 重建 KVCache 对象。SSD 读取成功后会提升到 hot cache，后续请求可从内存直接命中。

## 命中率口径

项目里至少存在两类命中率口径，分析时不能混用：

- block 级命中率：`PagedCacheManager.get_computed_blocks()` 中每匹配一个完整 block 记一次 hit，遇到第一个缺失 block 记一次 miss。`BaseCacheStats.hit_rate = hits / (hits + misses)`。
- 请求/prefix 级命中率：`BlockAwarePrefixCache.fetch_cache()` 只要找到可复用前缀就记一次 `_hits`，完全找不到才记 `_misses`，同时累计 `_tokens_saved`、`_tokens_matched_total` 和 `_tokens_requested_total`。
- SSD/hot cache I/O 统计：`PagedSSDCacheManager.load_block()` 记录 SSD/hot cache 的 `loads`、`hits`、`misses`、`hot_cache_hits`、`hot_cache_promotions` 等。

因此，一个请求可以是 prefix 级 hit，但只命中部分 block；也可能 block hash 存在，但 KV 重建失败后被调度器按 miss 处理。评估用户体感时，`cached_tokens / prompt_tokens` 通常比单纯 hit-rate 更有意义。

## 命中条件

prefix cache 命中要求前缀在 block 边界上连续匹配：

- 模型名参与 cache signature，不同模型不会共用。
- block hash 是链式 hash，前一个 block 的 hash 会参与下一个 block 的 hash。
- 只有完整 block 会进入主要匹配路径，尾部不足一个 block 的 token 不会贡献 block 命中。
- VLM extra keys 会参与 hash；同文本不同图片、图片位置不同或 extra key range 不同都会隔离缓存。
- cache format、block size、layer cache type、TurboQuant/RotatingKVCache 等签名不兼容时，SSD block 会被跳过或清理。
- exact hit 还需要能把 KV 状态安全修剪到 `N-1` token；某些 stateful cache 类型无法修剪时会回退全量 prefill。

## 影响命中率的因素

最常见的低命中原因：

- prompt 前缀不稳定：系统提示、工具列表、时间戳、随机上下文、用户个性化内容放在开头。
- prompt 太短或变化发生在首个 block 内，无法形成完整 block 命中。
- block size 与模型 cache 类型对齐后较大，导致需要更长稳定前缀才能命中。
- 频繁切换模型、量化配置、TurboQuant KV、RotatingKVCache 或 VLM 输入，导致 cache signature 变化。
- SSD cache 容量不足，旧 block 被 LRU 淘汰。
- hot cache 太小或被内存压力收缩，SSD 命中仍存在，但热命中下降。
- 内存压力达到 guard 后，调度器会跳过 hot-cache preload/promotion，减少内存占用但降低热命中。
- exact hit 遇到不可安全 trim 的 cache 类型，会为了确定性回退全量 prefill。

## 提高命中率的办法

策略层面优先级：

1. 稳定 prompt 前缀。把系统提示、工具定义、固定格式说明放在最前面；把时间、会话摘要、用户临时变量放在后面。
2. 让常用长前缀跨过 block 边界。短 prompt 或变化发生在第一个 block 内时，prefix cache 很难产生收益。
3. 避免无意义的模型配置切换。模型名、block size、cache 类型和量化/KV 策略变化都会降低复用。
4. 给 SSD cache 足够容量。`paged_ssd_cache_max_size` 太小会导致 LRU 淘汰，长上下文场景建议使用独立高速 SSD 目录。
5. 给 hot cache 合理内存预算。`hot_cache_max_size=0` 等于关闭内存热层；过大则可能触发内存保护收缩。
6. 降低内存压力。减少同时加载模型数量、降低 hot cache 预算或提高可用内存，避免调度器绕过 hot cache preload。
7. 对 VLM 请求保持图片顺序和预处理稳定。图片 hash/range 是 key 的一部分，任何变化都会隔离 cache。
8. 用 token 级指标看效果。关注接口返回的 cached prompt tokens、管理面 cache stats、SSD hot hit 和 tokens saved，而不是只看请求数 hit-rate。

可操作配置：

- 启用 SSD cache：设置 `paged_ssd_cache_dir` 或使用 CLI 的 `--paged-ssd-cache-dir`。
- 调整 SSD 容量：`paged_ssd_cache_max_size` / `--paged-ssd-cache-max-size`。
- 启用 hot cache：`hot_cache_max_size` / `--hot-cache-max-size` 设置为非零。
- 对重复大前缀服务，优先使用固定 `--base-path` 和固定 cache 目录，避免每次启动换空缓存。

## 观测与排障

建议按这个顺序看：

1. API 响应中的 cached prompt tokens 是否增加。
2. `/admin/api/stats` 或管理 UI 的 hot cache、SSD cache、模型内存和 active request 状态。
3. `PagedSSDCacheManager.get_stats_dict()` 暴露的 hot cache entries、hot cache size、SSD tracked size、loads/hits/misses。
4. DEBUG 日志中的 prefix divergence 信息，用于判断 prompt 在哪里开始偏离已存前缀。
5. 是否出现内存压力日志，例如跳过 hot-cache preload 或 process memory enforcer shrink hot cache。

如果请求显示 idle 但内存或缓存状态异常，应结合 `vmmap -summary`、模型 unload 日志和管理面 stats 判断是 cache、模型权重、MLX/Metal allocator 还是进程常驻内存。

## 当前可改进点

从机制上看，当前缓存设计已经具备稳定前缀复用、SSD 持久化和热层加速。更值得补强的是观测和策略：

- 在管理面同时展示请求级 hit rate、block 级 hit rate、token saved rate，避免单一命中率误导。
- 将 prefix divergence 的结果做成管理 API/诊断接口，帮助用户知道第几个 token 或哪个 extra key 导致 miss。
- 对 exact hit fallback 计数，区分“找到了缓存但因不可 trim 回退”的情况。
- 对 hot cache bypass under pressure 计数，解释为什么 SSD 命中存在但热命中下降。
- 为常见 chat template/工具调用场景提供 prompt 稳定性建议或模板检查。

