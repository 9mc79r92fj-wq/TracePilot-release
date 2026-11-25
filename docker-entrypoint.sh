#!/bin/sh
# TracePilot 容器入口：准备持久化数据目录并注入静态数据集/演示 Trace。
# - 生产环境将持久化磁盘挂载到 /app/data（Render disk mountPath=/app/data）；
# - seed 文件只在目标不存在时复制，绝不覆盖用户已有数据；
# - 不读取/不打印任何 API Key。
set -eu

DATA_DIR="${TRACEPILOT_DATA_DIR:-/app/data}"
SEED_DIR="${TRACEPILOT_SEED_DIR:-/app/seed_data}"

mkdir -p "$DATA_DIR"
mkdir -p "$DATA_DIR/traces" "$DATA_DIR/documents" "$DATA_DIR/evaluation/traces"

if [ -d "$SEED_DIR" ]; then
  # 只补齐缺失文件：用户上传、运行 Trace、SQLite 数据库不会被 seed 覆盖。
  find "$SEED_DIR" -type f -print | while IFS= read -r src; do
    rel="${src#"$SEED_DIR"/}"
    dest="$DATA_DIR/$rel"
    if [ ! -e "$dest" ]; then
      mkdir -p "$(dirname "$dest")"
      cp "$src" "$dest"
    fi
  done
fi

echo "[tracepilot] data_dir=$DATA_DIR seed_dir=$SEED_DIR profile=${TRACEPILOT_ENV:-production}"

exec "$@"
