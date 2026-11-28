# 安全发布与回滚手册（魔搭原链接优先）

> **红线**：`https://www.modelscope.cn/studios/luis123XI394/TracePilot` 必须始终可访问。
> 任何发布动作都必须先经过本地构建 + 冒烟 + 健康门禁；失败立即回滚，不 force push、不覆盖线上数据。
>
> 本手册编写时的实测状态（2026-11-28）：公网 `/api/health` 200、`trace_count=48`；
> V2 实验页、Benchmark/Judge 归档正常；真实 API 关闭、预算 fail-closed。
> 可用 `python scripts/verify_public_deploy.py` 随时复核（只读，不产生任何写入）。

## 1. 本次升级的风险结论（先看这里）

本次 LangFuse/OTel + K8s + 术语修正 **不修改** 生产运行链路的任何关键文件：

| 变更 | 是否影响魔搭镜像 | 处理 |
|---|---|---|
| 新增 `backend/app/services/langfuse_export.py`、`langfuse_sdk_export.py`、`scripts/export_langfuse.py` | **是**（`requirements.txt` 新增 `langfuse==5.2.0`） | 必须本地 docker build/run 验证后按第 3/4 节发布 |
| 新增 `deploy/k8s/**`、`tests/test_k8s_manifests.py` | 否（kind 本地验证专用，独立 namespace/镜像 tag） | 无需重新部署 |
| 术语修正（README/docs/接口字段值/注释） | 镜像代码里的接口值有变化；但**不改变端口/依赖/启动方式** | 可暂不部署；需要对外展示时再走第 4 节 |
| `requirements.txt` | **已改动**：新增 `langfuse==5.2.0` | 触发镜像重建；已本地验证 SDK 可安装、服务可启动 |
| 根 `Dockerfile` / `docker-entrypoint.sh` | 未改动 | 仍为单端口 7860 |
| 端口 | 仍为 `7860`（单端口同源） | 无变化 |

因此**本次需要发布一次镜像**（仅新增依赖与后端旁路模块，不改端口/启动方式）。
发布前已在本地完成 `docker build` + `docker run` 7860 冒烟；发布时严格执行第 4 节流程并保留回滚点。
若任一门禁失败，立即用 `scripts/rollback_modelscope.ps1` 回滚。

## 2. 本地开发：dev 分支 → 测试 → 合并

```bash
# 0) 从当前稳定主线开分支（本仓库当前主线：main；历史 V2 分支：v2.0）
git switch main
git pull --ff-only            # 若配置了远端；无远端时跳过
git switch -c dev/langfuse-k8s-terminology

# 1) 开发并分主题提交（小步、可回滚）
git add backend/app/services/langfuse_export.py scripts/export_langfuse.py tests/test_langfuse_export.py
git commit -m "feat(observability): Trace -> LangFuse OTLP 增量导出（零新增依赖）"

git add deploy/k8s tests/test_k8s_manifests.py
git commit -m "feat(k8s): kind 单节点 Deployment/Service 与静态校验"

git add README.md docs/TERMINOLOGY.md docs/OBSERVABILITY_LANGFUSE.md scripts/check_terminology.py tests/test_terminology.py
git commit -m "docs/chore(terminology): 统一术语并接入 CI 检查"

# 2) 本地门禁（全绿才允许合并）
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe scripts/check_terminology.py
cd frontend && npm ci && npm run build && cd ..
python scripts/export_langfuse.py --dry-run --limit 5
python scripts/verify_public_deploy.py        # 只读确认线上仍健康

# 3) 合并回 main（要求快进合并，保持历史线性、不产生不可回滚的 merge commit）
git switch main
git merge --ff-only dev/langfuse-k8s-terminology
git tag -a pre-deploy-$(date +%Y%m%d-%H%M) -m "发布前检查点"
```

> 若 `--ff-only` 失败，说明 main 有新提交：先 `git switch dev/... && git rebase main`，
> 重新跑第 2 步门禁后再快进合并。**禁止** `git push --force` 到任何发布仓库。

## 3. 本地 Docker 验证（当且仅当改了镜像相关文件时）

本次未改根 Dockerfile/依赖，但仍可用 kind 专用镜像做一次"生产形态"验证：

```powershell
# 3.1 构建（BuildKit 会自动使用 deploy/k8s/Dockerfile.dockerignore）
docker build -f deploy/k8s/Dockerfile -t tracepilot:local .

# 3.2 单容器冒烟：端口 7860（与线上同端口语义）
docker run --rm -d --name tracepilot-smoke -p 7860:7860 `
  -e TRACEPILOT_PUBLIC_DEMO=1 -e TRACEPILOT_ALLOW_REAL_API=0 `
  -e TRACEPILOT_REAL_TOTAL_CALL_LIMIT=0 tracepilot:local
Start-Sleep -Seconds 8
curl.exe -fsS http://127.0.0.1:7860/api/health
curl.exe -fsS http://127.0.0.1:7860/api/v2/status
curl.exe -fsS -o NUL -w "%{http_code}" http://127.0.0.1:7860/v2
docker stop tracepilot-smoke
```

检查清单：`/api/health` 200、`/api/v2/status` 200 且 `real_api.available=false`、`/v2` 返回 200、
容器以非 root（uid 10001）运行、日志无 traceback。

```powershell
# 3.3 kind 单节点（可选，需要 kind / kubectl）
kind create cluster --name tracepilot --config deploy/k8s/kind-config.yaml
kind load docker-image tracepilot:local --name tracepilot
kubectl apply -k deploy/k8s
kubectl -n tracepilot rollout status deploy/tracepilot --timeout=180s
curl.exe -fsS http://localhost:8080/api/health
kubectl -n tracepilot logs deploy/tracepilot --tail=50
kind delete cluster --name tracepilot
```

## 4. 发布到魔搭创空间（仅当确实需要更新线上时）

> 魔搭创空间是**重建式部署**：push 代码后触发构建，构建期间旧实例可能被替换。
> 因此必须先在本地用"部署副本 + 7860 单端口"完成构建与冒烟，再发布；发布失败立刻回滚。

### 4.1 发布前检查（全部通过才继续）

```powershell
# 1) 部署副本必须与新代码一致（部署副本不是主仓库，必须显式同步）
#    - 后端：TracePilot -> TracePilot-ModelScope/backend
#    - 前端：构建 dist 后同步到 TracePilot-ModelScope/frontend/dist
#    - 文档/seed 按需同步
# 2) 部署副本不得包含 .env / backend/.env / backend/data / node_modules
# 3) 本地部署副本 7860 冒烟（与线上完全同构）
cd C:\Users\yyxxy\Desktop\TracePilot-ModelScope
docker build -t tracepilot-ms:candidate .
docker run --rm -d --name ms-candidate -p 7860:7860 `
  -e TRACEPILOT_PUBLIC_DEMO=1 -e TRACEPILOT_PUBLIC_REAL_API=0 `
  -e TRACEPILOT_REAL_TOTAL_CALL_LIMIT=0 tracepilot-ms:candidate
Start-Sleep -Seconds 10
curl.exe -fsS http://127.0.0.1:7860/api/health
curl.exe -fsS http://127.0.0.1:7860/api/v2/status
docker stop ms-candidate
```

### 4.2 推送空间仓库（保留回滚点，绝不 force）

```powershell
$token = $env:MODELSCOPE_TOKEN   # 只从环境变量读取，禁止打印/写入文件
$space = "$env:TEMP\tracepilot-space"
git clone "https://oauth2:$token@www.modelscope.cn/studios/luis123XI394/TracePilot.git" $space
cd $space
git rev-parse HEAD > "$env:TEMP\tracepilot-rollback-commit.txt"   # 记录回滚点
Get-Content "$env:TEMP\tracepilot-rollback-commit.txt"

# 用部署副本内容覆盖工作区（保留 .git）
# 注意：frontend/dist 被部署副本的 .gitignore 忽略，必须强制加入（历史踩坑：缺 assets 会白屏）
# 在部署副本目录执行同步脚本/robocopy 后：
git add -A
git add -f frontend/dist
git -c user.name="TracePilot Deploy" -c user.email="deploy@tracepilot.local" commit -m "deploy: <本次变更摘要>"
git push origin HEAD:master        # 不 --force
```

### 4.3 触发构建并观测

```powershell
# 用 ModelScope OpenAPI 触发（token 同上，只读日志接口也需要）
Invoke-RestMethod -Method POST -Uri "https://modelscope.cn/openapi/v1/studios/luis123XI394/TracePilot/deploy" `
  -Headers @{ Authorization = "Bearer $env:MODELSCOPE_TOKEN" }

# 轮询状态（Building -> Deploying -> Running），Running 后再做健康门禁
Invoke-RestMethod -Uri "https://modelscope.cn/openapi/v1/studios/luis123XI394/TracePilot" `
  -Headers @{ Authorization = "Bearer $env:MODELSCOPE_TOKEN" }
```

### 4.4 发布后健康门禁（任一失败立即回滚）

```powershell
python scripts/verify_public_deploy.py
# 人工抽查（真实浏览器）：
#   https://www.modelscope.cn/studios/luis123XI394/TracePilot
#   - 工作台正常渲染，无白屏
#   - 系统 -> V2 实验：真实 API 不可用、预算 0/0、Benchmark/Judge 归档可见
#   - 刷新 /v2 深链接不 404
```

## 5. 回滚（失败时唯一正确动作）

```powershell
# 方式 A（推荐）：revert 本次部署提交，再触发一次部署（不改变历史、不需要 force）
cd "$env:TEMP\tracepilot-space"
git revert HEAD --no-edit
git push origin HEAD:master
Invoke-RestMethod -Method POST -Uri "https://modelscope.cn/openapi/v1/studios/luis123XI394/TracePilot/deploy" `
  -Headers @{ Authorization = "Bearer $env:MODELSCOPE_TOKEN" }

# 方式 B：回到记录的回滚 commit（仅当 revert 不可行时）
#   git reset --hard $(Get-Content "$env:TEMP\tracepilot-rollback-commit.txt") 后强推
#   —— 强推会丢失空间仓库历史，仅作最后手段，需书面确认。

# 回滚后再次执行健康门禁
python scripts/verify_public_deploy.py
```

## 6. 100% 保护原链接的硬规则

1. **不碰生产关键文件**：不修改部署副本的 `EXPOSE/CMD` 端口（7860）、不改启动参数；
   若必须改，先在本地完整构建 + 7860 冒烟，再走第 4 节。
2. **不 force push**：发布用普通 push；回滚用 `git revert`；任何 reset 强推都要人工确认。
3. **先本地、后线上**：`docker build` 失败/冒烟失败，一律停止，线上保持原样。
4. **保留回滚点**：push 前记录空间仓库 HEAD；发布失败在 5 分钟内 revert。
5. **健康门禁**：`scripts/verify_public_deploy.py` 全 PASS 才算发布成功；深链接、前台渲染人工抽查。
6. **安全默认值**：公开空间保持 `TRACEPILOT_PUBLIC_REAL_API=0`、`TRACEPILOT_REAL_TOTAL_CALL_LIMIT=0`、
   `TRACEPILOT_ALLOW_REAL_API=0`，避免发布过程产生真实费用。
7. **不覆盖数据**：部署副本数据在 `/mnt/workspace/tracepilot` 持久卷；发布流程只更新代码/镜像，
   不执行删除持久卷/清库操作。
8. **K8s 与魔搭隔离**：`deploy/k8s` 使用独立 namespace 和 `tracepilot:local` 镜像，不参与魔搭生产发布。

## 7. 常见故障

| 现象 | 处理 |
|---|---|
| 云端构建失败、服务 Down | 立即 revert 到回滚点并重新部署；查看 `/logs/build` |
| 页面白屏 / assets 404 | 检查 `frontend/dist` 是否被强制提交（`git add -f frontend/dist`）；重推 |
| `/api/health` 200 但页面空白 | 检查 `TRACEPILOT_STATIC_DIR` 是否指向 `/app/frontend/dist` |
| 构建超时 | 本地先 `docker build` 验证；云端构建节点网络波动时重试 |
| 部署后数据为空 | 检查持久卷 `/mnt/workspace/tracepilot` 与 `.seeded` 标记；勿删除持久卷 |
| 误触发真实费用 | 立即把 `TRACEPILOT_REAL_TOTAL_CALL_LIMIT` 置 0 并重启；检查 `/api/admin/budget` |

## 附录 A：本次发布记录（2026-11-28）

| 项目 | 实际值 |
|---|---|
| 发布内容 | LangFuse SDK/OTLP 可观测性、kind K8s 编排、术语统一（Plan-and-Execute / 受控执行 / 评测流水线）、JD 术语映射 |
| requirements 变更 | 新增 `langfuse==5.2.0`（含官方 SDK 依赖） |
| 本地镜像验证 | `docker build` 成功；`docker run -p 7860:7860` 容器 `healthy`；`/api/health` ok、`/api/v2/status` 正常、`/api/observability/langfuse` 返回 `sdk.available=true (5.2.0)`、`/v2` 200 |
| 空间仓库回滚点 | `91aaf2e3f2e25a84e9f2ce4277493e1b850b46c6` |
| 空间仓库发布提交 | `c16291f9aec97baaa9e2296dfe88d800f6de23cc`（普通 push，无 force） |
| 云端构建 | 成功；studio/runtime 均 `Running` |
| 公网健康门禁 | 7/7 PASS（health / v2 status / 真实 API 关闭 / 预算 fail-closed / Judge 校准 / 互操作 schema / 首页） |
| 持久化 | 发布后 `trace_count=49`（沿用持久卷数据，未清库） |
| 原链接 | 未变：<https://www.modelscope.cn/studios/luis123XI394/TracePilot> |

### 一键回滚（本次发布专用）

```powershell
# 1) 设置令牌（只在当前 shell，不落盘）
$env:MODELSCOPE_TOKEN = "<你的 ModelScope 令牌>"

# 2) 一键回滚：revert 最新部署提交 -> 普通 push -> 触发重新部署 -> 等待 Running + 健康检查
cd C:\Users\yyxxy\Desktop\TracePilot
.\scripts\rollback_modelscope.ps1 -SpaceRepo "$env:TEMP\tracepilot-space-deploy"

# 3) 复核（只读）
python scripts\verify_public_deploy.py
```

> 回滚后空间仓库会恢复到 `91aaf2e` 的代码内容（通过 `git revert` 产生新提交，不 force、不丢历史）。
> **不使用** `reset --hard` + 强推：该方式会改写空间仓库历史，只允许在 revert 无法执行时由维护者人工确认后使用。
