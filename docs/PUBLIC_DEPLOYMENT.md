# TracePilot 公网部署指南（Render + Vercel）

> 本文件描述第 63 步的部署方案与当前状态。**真实公网 URL 只有在用户完成
> GitHub / Render / Vercel 授权并实际部署后才会出现；本文件不编造链接。**

## 1. 架构

```
GitHub 私有仓库 (TracePilot)
        │  push 触发自动部署
        ├──────────────► Render（FastAPI / Docker，免费或付费磁盘）
        │                        ▲
        └──────────────► Vercel（React Vite 静态站点）
                                 │ 浏览器直连 API（CORS 白名单）
                                 └── VITE_API_BASE_URL=https://<render-service>
```

- 前端不使用任何 API Key；Key 只可能存在于 Render 服务端环境变量（当前公开演示不设置 Key）。
- 公开演示模式 `TRACEPILOT_PUBLIC_DEMO=1` 在代码层强制关闭真实 DeepSeek API，即使误设
  `TRACEPILOT_ALLOW_REAL_API=1` 也不会放行；匿名访客只能使用 Mock 模式。
- 文件上传、Trace、DAG、评测、归因等页面均可匿名浏览；工具白名单、SQL 只读、文件路径
  限制、请求体限制全部保留；生产环境启用按 IP 限流。

## 2. 云端账号与授权（需要用户操作）

| 平台 | 需要用户完成 | 原因 |
|---|---|---|
| GitHub | 确认仓库私有；在用户网络执行 `git push origin main` | 当前执行环境到 github.com:443 连接超时，无法推送；仓库已配置 remote |
| Render | 登录 Render → 安装 GitHub App → New Blueprint 选择本仓库；或提供 Render API Key | 创建服务需要账号授权；免费实例无持久磁盘 |
| Vercel | 登录 Vercel → Add New Project → 导入本仓库（Root Directory=`frontend`） | 创建站点需要账号授权；当前环境到 api.vercel.com 超时 |

**禁止事项**：不得创建公开仓库；不得强制推送/删除历史；不得未经确认购买付费资源。

## 3. Render 后端部署

1. 新建 Blueprint，选择本仓库（`render.yaml` 已提供）。
2. `render.yaml` 默认 **Free** 计划：无需付费，但 `/app/data` 为临时文件系统，
   **重新部署/重启会丢失 SQLite、运行 Trace、上传文件**。
3. 需要数据持久化时（**需用户明确确认费用**）：
   - 将 `plan: free` 改为 `plan: starter`（约 $7/月起）；
   - 取消 `render.yaml` 中 `disk` 段注释（`mountPath: /app/data`, 1GB）；
   - 重新部署。容器入口会把静态数据集/演示 Trace 复制到空磁盘，**不覆盖已有数据**。
4. 部署后记录后端地址，例如 `https://tracepilot-api.onrender.com`。
5. 在 Render 环境变量中填写 `TRACEPILOT_CORS_ORIGINS`（Vercel 正式域名）并重新部署。

### Render 环境变量（`render.yaml` 已声明）

| 变量 | 值 | 说明 |
|---|---|---|
| `TRACEPILOT_ENV` | `production` | 严格执行限额 |
| `TRACEPILOT_PUBLIC_DEMO` | `1` | 强制禁止真实 API |
| `TRACEPILOT_RATE_LIMIT_ENABLED` | `1` | 启用按 IP 限流 |
| `TRACEPILOT_RATE_LIMIT_GENERAL_PER_MINUTE` | `120` | 普通请求/分钟 |
| `TRACEPILOT_RATE_LIMIT_HEAVY_PER_MINUTE` | `10` | 高成本 POST/分钟 |
| `TRACEPILOT_TRUST_PROXY` | `1` | 使用 Render 的 X-Forwarded-For |
| `TRACEPILOT_DATA_DIR` | `/app/data` | 持久化挂载点 |
| `TRACEPILOT_DB_PATH` | `/app/data/tracepilot.db` | SQLite 路径 |
| `TRACEPILOT_CORS_ORIGINS` | Vercel 域名 | 部署 Vercel 后填写 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 仅模型名；**不设置 API Key / 不设置 ALLOW_REAL_API** |

## 4. Vercel 前端部署

1. Add New Project → 导入 GitHub 私有仓库。
2. Root Directory：`frontend`（包含 `package.json` 与 `vercel.json`）。
3. Framework Preset：Vite（Build Command `npm run build`，Output `dist` 已在 `vercel.json` 固定）。
4. 环境变量：`VITE_API_BASE_URL=https://<Render 后端地址>`（不要放 Key）。
5. 部署后把 Vercel 域名写入 Render 的 `TRACEPILOT_CORS_ORIGINS`。
6. `frontend/vercel.json` 已配置 SPA 回退：刷新 `/traces/...`、`/dag/...` 等深链不会 404。

## 5. 自动更新与回滚

- Render：`render.yaml` 中 `autoDeploy: true`，push 到 `main` 自动构建部署；平台保留
  部署历史与日志，可回滚到上一版本。
- Vercel：Git 集成后 push 到 `main` 自动构建；平台保留 Deployment 列表可一键回滚。
- GitHub：使用现有 `main` 分支，不强制推送；需要回滚时使用平台“Redeploy/Rollback”，
  不要删除仓库历史。

## 6. 本地生产模式验收（已执行，2026-11-20）

- 生产配置后端实例（8001）：`TRACEPILOT_ENV=production`、`PUBLIC_DEMO=1`、无 Key；
  `/api/health` 200；`/api/agent/status` 显示真实 API 不可用（公共演示原因）；
  真实模式 POST 返回 403；CORS 预检对配置域名返回正确头。
- 前端生产构建（`npm run build`）+ `vite preview` 4173：
  19 条路由全部 HTTP 200；Trace 详情、DAG 深链刷新正常；Mock Agent 执行成功；
  Evaluation Center 可运行；文件上传可解析并在页面列出；手机视口无横向溢出；
  页面无 console error；页面源码不含 `sk-` / `DEEPSEEK_API_KEY=`。
- 自动化：`pytest tests` 全量通过（见最终交付报告）；新增
  `tests/test_public_demo_guard.py` 9 项（真实 API 匿名禁用、限流、CORS）。

## 7. 未验证 / 限制

- 未实际构建 Docker 镜像（本机无 Docker）；Render 构建日志是首次真实容器构建验证。
- 未创建 Render/Vercel 资源（缺少账号授权），因此**没有真实公网 URL**。
- Render 免费实例无持久化磁盘，数据会在重新部署后丢失；付费磁盘方案已准备但需用户确认。
- Render Free 实例会休眠，首次访问可能冷启动较慢。
- 公开演示不开启真实 DeepSeek API；如需受控开放，必须另行设计登录/配额/服务端 Key 保护，
  不得直接把 Key 或 `TRACEPILOT_ALLOW_REAL_API=1` 暴露给匿名访客。

## 8. 部署后验收清单

1. `GET https://<render>/api/health` 返回 `{"status":"ok"}`；
2. 打开 Vercel 首页与 19 条路由；
3. 刷新 `/traces/<id>`、`/dag/<id>` 不 404；
4. Agent Runs 选择 Mock 执行成功并生成 Trace；
5. Trace 列表/详情/DAG/归因/评测页面可打开；
6. 文件上传→预览→引用→Mock 执行；
7. 手机/桌面访问无横向溢出；
8. 浏览器 Network 中无 `DEEPSEEK_API_KEY`/`sk-`；
9. `POST /api/agent/runs` 真实模式返回 403；
10. 连续快速请求命中 429；
11. 重新部署后数据是否保留（取决于是否启用付费磁盘）。
