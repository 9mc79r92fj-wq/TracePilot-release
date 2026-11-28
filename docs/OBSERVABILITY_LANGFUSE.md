# TracePilot 可观测性：Trace → LangFuse（OTLP/HTTP）

本文件说明如何把 TracePilot 的 Trace **增量、幂等、可审计**地导出到自托管 LangFuse，
以及如何在不影响魔搭线上部署的前提下安全启用。

> 结论先行：**默认关闭、不改端口/启动方式**；SDK 通道引入官方 `langfuse==5.2.0`（已加入
> `requirements.txt`，需重建镜像一次）；内置 OTLP 通道零新增依赖。发布流程与回滚见
> `docs/SAFE_DEPLOY_AND_RELEASE.md`，发布前必须完成本地 `docker build` + `docker run` 冒烟。

## 1. 为什么用 OTLP 而不是绑定某个 SDK

- **标准协议**：OTLP/HTTP JSON 是 OpenTelemetry 规范的一部分，任何兼容后端（LangFuse、otel-collector、
  Tempo、Jaeger…）都能消费；后续换后端无需改导出代码；
- **两条通道**：SDK 通道用官方 `langfuse`（已加入 requirements）；内置 OTLP 通道只用标准库 `urllib`，
  供最小化镜像/离线环境使用，两者共用映射与增量状态；
- **可审阅**：`--dry-run` 直接产出 OTLP JSON 文件，可以 diff、归档、离线校验，而不是"黑盒发送"。

## 2. 数据映射

### 2.1 Trace → OTel 结构

| TracePilot | OTLP 字段 |
|---|---|
| 一条 `TraceRecord` | 一个 OTel trace：`traceId = sha256("tracepilot:trace:<trace_id>")[:32]`（32 位十六进制，确定性） |
| `TraceRecord` 本身 | 根 span `agent.run`（含任务、run_id、状态、总 Token、执行模式） |
| `TraceStep` | 子 span：`spanId = sha256("tracepilot:span:<trace_id>:<step_id>")[:16]` |
| `parent_step_ids` | `parentSpanId`（无父节点时挂到根 span） |
| `timestamp + duration_ms` | `startTimeUnixNano / endTimeUnixNano`（纳秒字符串） |
| `status` | `status.code`：succeeded→1、failed→2、其他→0；失败附 `exception` event |
| `token_usage` | `gen_ai.usage.input_tokens` / `output_tokens` |
| `tool_name/arguments/result` | `tracepilot.tool_*` 属性（按 `max_attr_chars` 截断） |

### 2.2 语义属性

- 通用：`service.name`、`service.version`、`deployment.environment`、`telemetry.sdk.*`；
- GenAI：`gen_ai.system`、`gen_ai.request.model`、`gen_ai.operation.name`、`gen_ai.usage.*`；
- LangFuse 增强（可选）：`langfuse.observation.type`
  （`generation` / `tool` / `retriever` / `agent` / `span`）、`langfuse.observation.input/output`、
  `langfuse.trace.name`、`langfuse.trace.metadata.*`。

> 不同 LangFuse 版本对增强属性支持不同；**通用 OTel 字段始终有效**。若你的 LangFuse 版本不识别某属性，
> 仅表现为 UI 展示差异，不影响 Span 落库。

## 2.3 两种发送通道（SDK / OTLP）

| 通道 | 实现 | 依赖 | 适用 |
|---|---|---|---|
| `--transport sdk`（默认 auto 优先） | 官方 `langfuse` SDK 的类型化接口 `client.api.opentelemetry.export_traces` | `langfuse==5.2.0`（已加入 `requirements.txt`） | 生产接入自托管 LangFuse，由 SDK 负责鉴权/序列化 |
| `--transport otlp` | 内置 OTLP/HTTP JSON 发送器（`urllib`） | 零新增依赖 | 最小化镜像、离线环境、任意 OTLP 后端 |

两条通道**共用同一份增量状态与确定性 ID**，可混用（先 dry-run 审阅，再用 SDK 发送，或用 OTLP 补发到 collector）。
`--transport auto`（默认）在安装 SDK 时选择 sdk，否则回退 otlp；dry-run 始终走本地负载生成，不发送。

> 为什么既引 SDK 又保留 OTLP：SDK 满足"官方接入"的工程规范；内置 OTLP 保证在未安装 SDK 的
> 最小镜像/CI 环境仍可导出，且便于离线审计负载。两者不是重复实现业务逻辑，只共享映射层。

## 3. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TRACEPILOT_LANGFUSE_ENABLED` | 未设置（关闭） | `1` 时真实发送；否则只允许 dry-run |
| `LANGFUSE_HOST` | 空 | 自托管地址，如 `http://localhost:3000`；自动拼 `/api/public/otel/v1/traces` |
| `TRACEPILOT_OTEL_ENDPOINT` | 空 | 直接指定完整 OTLP endpoint（优先级高于 `LANGFUSE_HOST`）；SDK 通道会从该地址推导 host |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 空 | 二者同时存在时使用 `Authorization: Basic base64(pk:sk)` |
| `TRACEPILOT_OTEL_TOKEN` | 空 | 使用 `Authorization: Bearer <token>`（无 Basic 时） |
| `TRACEPILOT_OTEL_HEADERS` | 空 | 额外请求头，`Key1=Value1,Key2=Value2` |
| `TRACEPILOT_OTEL_SERVICE_NAME` | `tracepilot` | `service.name` |
| `TRACEPILOT_OTEL_TIMEOUT` | `15` | 单次请求超时秒数 |
| `TRACEPILOT_OTEL_MAX_RETRIES` | `3` | 5xx/429/网络错误重试次数 |
| `TRACEPILOT_OTEL_BATCH_TRACES` | `20` | 每个 OTLP 请求包含的 Trace 数 |
| `TRACEPILOT_OTEL_MAX_ATTR_CHARS` | `4000` | 单属性截断长度 |

状态文件默认 `data/observability/langfuse_export_state.json`（已 gitignore，不含任何密钥）。

## 4. 使用

```bash
# 1) 预览：生成 OTLP JSON，不发网络请求
python scripts/export_langfuse.py --dry-run --limit 10 --out ./.tmp/otlp

# 2) 真实导出（自托管 LangFuse）
LANGFUSE_HOST=http://localhost:3000 \
LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... \
python scripts/export_langfuse.py --yes --transport sdk --limit 200

# 3) 只读状态（不请求网络、不打印密钥）
python scripts/export_langfuse.py --status --json
curl -fsS http://127.0.0.1:8000/api/observability/langfuse
```

CLI 常用参数：`--yes`（确认真实发送）、`--force`（忽略增量重导）、`--limit N`、`--trace-id ID`、
`--state-file`、`--traces-dir`、`--endpoint`、`--max-retries`、`--json`。

### 自托管 LangFuse（参考）

LangFuse 官方仓库提供 docker-compose（Postgres + ClickHouse + Redis + MinIO）：

```bash
git clone https://github.com/langfuse/langfuse.git
cd langfuse
docker compose up -d          # 默认 Web 端口 3000
# 首次打开 http://localhost:3000 创建组织/项目，拿到 Public/Secret Key
```

> 本机没有拉起真实 LangFuse 实例，因此**未对真实 LangFuse 做端到端验收**；
> 已用"OTLP mock 服务"验证协议负载、鉴权头、重试与增量状态（见第 7 节）。
> 上线前建议先在测试环境用 LangFuse 或 otel-collector 验证一次。

## 5. 增量、幂等与一致性

1. **内容指纹**：对 Trace 的规范化 JSON 做 SHA-256；指纹未变化则跳过（`skipped`），
   这样"导出一次"之后重复运行不会重复写入；
2. **幂等 ID**：`traceId/spanId` 由 TracePilot ID 确定性派生，同一 Trace 重导不会产生随机新链路；
3. **dry-run 不推进状态**：避免"预览把真实导出变成跳过"；
4. **失败不推进**：某批失败时状态不记录该批 Trace，重跑即可补齐；状态同时记录 `last_error`；
5. **原子写**：状态先写临时文件再 `os.replace`；文件损坏时 **fail-closed**（报错而不是静默重置）；
6. **并发锁**：`<state>.lock` 防止两个导出进程同时写状态；超过 1 小时的陈旧锁自动清理；
7. **批次隔离**：默认 20 条 Trace/请求，单批失败不影响其他批次。

## 6. 安全边界

- 默认关闭；公开演示/魔搭空间**不设置**任何 LangFuse 变量，因此不会产生外部网络请求；
- 密钥只从环境变量读取，不写入状态文件、不打印、不进入 OTLP 负载；日志中 endpoint 去掉 userinfo/query；
- 状态文件只包含 trace_id/指纹/时间/span 数，不含 Trace 内容；
- 导出只读本地 Trace，不修改原始数据、不触发真实模型调用、不产生费用；
- 与生产镜像解耦：`scripts/`、`backend/app/services/langfuse_export.py` 是纯增量代码，无新依赖/新端口。

## 7. 验证记录（实际执行）

- 单测 27 项全绿：`tests/test_langfuse_export.py`（21）与 `tests/test_langfuse_sdk_export.py`（6，真实 SDK + 本地 mock LangFuse）
  （负载结构、父子关系、GenAI/LangFuse 属性、错误事件、dry-run、增量/force、Basic 鉴权、
  5xx 重试、401 不重试、确认保护、并发锁、损坏状态 fail-closed、密钥不泄露、CLI、只读 API；
  SDK 通道：类型化 OtelResourceSpan 转换、SDK 实际 POST 路径/鉴权头、增量跳过、失败不推进状态）；
- 真实本地 Trace dry-run：`python scripts/export_langfuse.py --dry-run --limit 3` →
  3 条 Trace / 20 个 Span，生成 3 个 OTLP JSON（约 21–26KB/条）；
- 协议联调：测试内置 OTLP mock HTTP 服务校验请求头、负载与重试行为；
- **线上验证**（2026-11-28）：部署到魔搭后，`GET /api/observability/langfuse` 返回
  `sdk.available=true`、`sdk.version=5.2.0`，默认 `enabled=false`（不会产生外部调用）；
- **未执行**：真实 LangFuse 实例端到端（本机无实例）。这是已知限制，不宣称已验证。

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| `真实导出需要显式确认` | 加 `--yes`，或设置 `TRACEPILOT_LANGFUSE_ENABLED=1` |
| `未配置 OTLP endpoint` | 设置 `LANGFUSE_HOST` 或 `TRACEPILOT_OTEL_ENDPOINT` |
| HTTP 401/403 | 检查 Public/Secret Key 是否成对配置；或改用 Bearer/自定义 header |
| HTTP 404 | LangFuse 版本路径不同：用 `TRACEPILOT_OTEL_ENDPOINT` 覆盖完整路径 |
| 重复导出 | 正常增量会跳过；需要强制重导用 `--force` |
| 状态文件损坏 | 检查 `last_error`；确认后删除状态文件重导（不删除原始 Trace） |
| 想换后端 | 把 endpoint 指向任何 OTLP/HTTP collector 即可，无需改代码 |
