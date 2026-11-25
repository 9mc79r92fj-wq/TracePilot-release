# DEPLOYMENT（第 55 步补齐：部署能力；第 63 步补充公网部署）

> **公网部署（Render + Vercel）请优先阅读 [docs/PUBLIC_DEPLOYMENT.md](PUBLIC_DEPLOYMENT.md)**；
> 其中包含真实公网部署所需的账号授权、环境变量、持久化磁盘与验收清单。
>
> 本文件状态：PARTIAL / NOT_VERIFIED（容器镜像未在本机构建；公网资源尚未创建）。
> 本地开发演示（venv + uvicorn + Vite）已验证可用；容器/公网运行状态必须在实际部署后
> 才能标记为 COMPLETE。原始需求 PDF 第 11 节阶段 12 要求 CI/CD 与可部署 Demo，
> 本目录仅完成配置与文档，不冒充已验证部署。

## 1. 本地开发（已实测）

```powershell
# 后端（127.0.0.1:8000）
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

# 前端（localhost:5173）
cd frontend
npm run dev -- --port 5173
```

打开 `http://localhost:5173/`。全部数据为本地文件/内存数据，默认不调用付费 API。

## 2. Docker Compose（配置已提供，未运行）

```bash
docker compose up --build
# 前端 http://localhost:8080
# 后端 http://localhost:8000
```

- `Dockerfile`：Python 3.11 + FastAPI/uvicorn，仅安装 `requirements.txt` 依赖；
- `frontend/Dockerfile`：Node 构建静态资源 + nginx；
- `frontend/nginx.conf`：SPA 路由回退 + `/api/` 反向代理到 backend；
- `docker-compose.yml`：backend / frontend 两服务，`data/`、`config/` 挂载；
- **不注入任何 API Key**；真实模型调用默认关闭，需要用户显式授权并自担费用。

构建后建议验证：

```bash
curl http://localhost:8000/api/health
curl http://localhost:8000/api/capabilities/matrix
curl -I http://localhost:8080/pareto
```

## 3. 安全与数据

- 容器内只运行本地确定性服务；`.env` 不进入镜像（`.dockerignore` 排除）；
- `data/traces/`、`data/evaluation/traces/`、`data/pareto/` 等运行数据在 `.dockerignore` 中排除，
  镜像不包含本机演示 Trace；compose 通过 volume 挂载 `./data`；
- 生产部署需补充：访问控制、TLS、只读挂载、资源限制、日志轮转与备份策略（当前未实现）。

## 4. 仍需完成（对应 docs/TECH_DEBT.md）

1. 在具备 Docker 的环境执行 `docker compose build && up` 并截图/记录；
2. 修复构建过程中发现的依赖/路径问题；
3. 增加 `docker build` 到 CI（当前 GitHub 云端 CI 也未实际运行）；
4. 部署后验证 19 条路由、API 与关键流程；
5. 如需在线 Demo，还需服务器/域名/TLS 与访问控制方案（原始需求待确认项）。
