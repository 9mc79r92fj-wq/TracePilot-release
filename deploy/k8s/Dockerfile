#
# TracePilot · kind / K8s 单端口生产风格镜像（纯新增；不替换、不修改根目录 Dockerfile）
#
# 结构：
#   stage 1  node:22 构建前端（npm run build -> dist）
#   stage 2  python:3.11-slim 运行 FastAPI，同源托管 /api 与前端 SPA，监听 0.0.0.0:7860
#
# 安全：
#   - 非 root 用户 uid/gid 10001 运行，/app/data 与 /app/backend/data 归该用户；
#   - 镜像内不含任何 API Key / Token，敏感信息由 K8s Secret（secretKeyRef）注入；
#   - 不设置 TRACEPILOT_ALLOW_REAL_API，公开演示默认不发起付费调用。
#
# 构建（在仓库根目录执行）：
#   docker build -f deploy/k8s/Dockerfile -t tracepilot:local .
#
# 构建上下文说明：仓库根 .dockerignore 排除了 frontend/（后端镜像场景），因此本目录提供
# deploy/k8s/Dockerfile.dockerignore，BuildKit 会自动优先使用它（不排除 frontend/），
# 无需修改根 .dockerignore，也不影响既有部署。旧版 Docker（<23，无 BuildKit）见 README 的方式 A。

# ---------- stage 1：前端静态资源 ----------
FROM node:22-bookworm-slim AS frontend-build

WORKDIR /build/frontend

# 先拷贝依赖清单，利用 Docker 层缓存；package-lock.json 用于可复现的 npm ci
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# 拷贝前端源码并构建（vite 产物目录：/build/frontend/dist）
COPY frontend/ ./
RUN npm run build

# ---------- stage 2：FastAPI 单端口运行时 ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 生产档位 + 单端口同源托管（FastAPI 挂载前端构建产物）
    TRACEPILOT_ENV=production \
    TRACEPILOT_STATIC_DIR=/app/frontend/dist \
    TRACEPILOT_DB_PATH=/app/data/tracepilot.db \
    PORT=7860

WORKDIR /app

# 依赖层单独缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 后端代码 / 配置 / 脚本：目录结构与仓库内相对导入保持一致
COPY backend ./backend
COPY config ./config
COPY scripts ./scripts

# 前端构建产物：由 FastAPI 依据 TRACEPILOT_STATIC_DIR 同源托管
COPY --from=frontend-build /build/frontend/dist /app/frontend/dist

# 非 root 运行时用户（uid/gid 10001）；数据目录提前创建并归属该用户，
# 兼容 K8s emptyDir / PVC + fsGroup，避免启动时因目录不可写而失败。
RUN groupadd --gid 10001 tracepilot \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin tracepilot \
    && mkdir -p /app/data /app/backend/data \
    && chown -R 10001:10001 /app/data /app/backend/data

USER 10001

EXPOSE 7860

# 健康检查：不依赖 curl/wget，直接用 Python 标准库请求 /api/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/api/health', timeout=3)" || exit 1

# 单端口监听：/api 与前端 SPA 由同一个 uvicorn 进程提供
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "7860"]
