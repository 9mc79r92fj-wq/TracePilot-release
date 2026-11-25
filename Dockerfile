# TracePilot 后端镜像（Render / 容器部署）
#
# 安全原则：
# - 镜像内不包含任何 .env / API Key；真实模型调用默认关闭（TRACEPILOT_ALLOW_REAL_API 不设置）；
# - 运行数据统一放在 /app/data，生产环境应挂载持久化磁盘（Render 需付费 Starter 计划）；
# - 首次启动时从 /app/seed_data 复制静态数据集与演示 Trace，不覆盖已有用户数据。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TRACEPILOT_ENV=production \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY config ./config
COPY scripts ./scripts
COPY data ./seed_data
COPY docker-entrypoint.sh /usr/local/bin/tracepilot-entrypoint
RUN chmod +x /usr/local/bin/tracepilot-entrypoint && mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health',timeout=3)" || exit 1

ENTRYPOINT ["tracepilot-entrypoint"]
CMD ["sh", "-c", "python -m uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
