# TracePilot 浏览器 E2E（Playwright）运行说明

## 前置服务（本机已就绪，勿由测试启动）

| 服务 | 地址 | 说明 |
|------|------|------|
| 后端 | `http://127.0.0.1:8000` | `TRACEPILOT_ALLOW_REAL_API` 未开启；真实 API 调用被禁用；管理员认证未强制（本机开发模式） |
| 前端 dev server | `http://localhost:5173` | `npm run dev`（Vite） |

`playwright.config.ts` 使用系统 Microsoft Edge（`channel: 'msedge'`），单 worker 串行执行。

## 运行

```powershell
cd frontend
$env:PATH="C:\node-v26.13.0-win-x64;$env:PATH"   # 需要时
npx playwright test --project=desktop-edge        # 等价于 npm run test:e2e
```

单文件调试：

```powershell
cd frontend
npx playwright test e2e/flows.spec.ts --project=desktop-edge
```

## E2E 前置 fixture（受控 Trace，由仓库脚本生成，不是生产种子数据）

数据重建（第 74 步真实性整改）会重置 `data/traces/`，因此部分 spec 依赖的两条**受控 fixture Trace**
需要在跑 E2E 前用仓库自带脚本生成。二者均为本地真实执行链路 + 脚本化模型，**不调用付费 API**：

```powershell
cd C:\Users\yyxxy\Desktop\TracePilot

# 1) MOCK 失败 Trace（7 步，calculator 注入 tool_timeout；四类受控实验资格均可用）
#    实际生成 trace_id: trace-9b4a0dd4871d4c1394e3497af7c55c47
.\.venv\Scripts\python.exe scripts\create_mock_failure_trace.py

# 2) SIMULATED 重试失败 Trace（13 步，calculator 连续 4 次 tool_timeout，形成 retry 链）
#    需要 local 执行档以允许 >3 次工具调用；实际生成 trace_id:
#    trace-cf771763fe1d4f27b94167611fe5b0e3
$env:TRACEPILOT_ENV="local"
.\.venv\Scripts\python.exe scripts\create_simulated_retry_trace.py
```

说明：

- 两个脚本幂等（同一 `scenario_id` 已存在时跳过），仅写入 `data/traces/` 下 1 个文件；`data/` 不入版本库。
- fixture 的 `metadata.data_source` 分别为 `mock` / `simulated`，**绝不会是 `real_api`**。
- 脚本每次生成的 `trace_id` 不固定：依赖 fixture 的 spec 已改为**从后端 API 动态发现**
  （MOCK 失败 Trace / 13 步 SIMULATED 失败 Trace），数据重建后无需改 spec 里的 id。
- 当前数据目录没有已验证的 REAL_API Trace（REAL_API 全库 0、付费 API 0 次），
  因此 cost-analytics / capabilities 等 spec 不假设存在真实 API Trace，按 MOCK/SIMULATED 真实口径断言。
- 若数据目录再次重建，请重新执行以上两条命令即可。

## 产物

`test-results/`、`playwright-report/`、`e2e-results.json` 为运行产物，均已 gitignore。
