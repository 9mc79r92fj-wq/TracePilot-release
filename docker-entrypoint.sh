#!/bin/sh
# TracePilot · ModelScope Docker 创空间入口脚本
#
# 职责（全部幂等，可重复启动）：
#   1. 选择数据根目录：优先 /mnt/workspace/tracepilot（ModelScope 唯一持久化目录），
#      不可写时退回容器本地目录并打印警告；
#   2. 首次启动时把镜像内 seed_data/ 的演示数据集注入数据根目录，绝不覆盖已有文件；
#   3. 用符号链接把应用默认路径 /app/data、/app/backend/data 指向数据根目录
#      （前后端代码中的默认路径不变，无需修改业务代码）；
#   4. 注入公开演示默认环境变量（可被创空间设置里的环境变量覆盖），最后 exec 启动服务。
#
# 说明：
#   - 本脚本不使用任何 bash 扩展语法（Debian /bin/sh = dash）；
#   - 不读取、不打印任何 API Key；
#   - 容器内数据目录约定：
#       /mnt/workspace/tracepilot/data/            -> /app/data
#       /mnt/workspace/tracepilot/backend_data/    -> /app/backend/data（SQLite + 上传文件）
set -eu

APP_ROOT="${TRACEPILOT_APP_DIR:-/app}"
SEED_DIR="${TRACEPILOT_SEED_DIR:-$APP_ROOT/seed_data}"
PERSIST_BASE="${TRACEPILOT_PERSIST_DIR:-/mnt/workspace/tracepilot}"
# 数据模式：auto（默认，持久化目录可用则用）/ symlink（强制持久化）/ local（不持久化，仅本地调试）
DATA_MODE="${TRACEPILOT_DATA_MODE:-auto}"

log() { printf '[tracepilot] %s\n' "$*"; }

# 只补齐缺失文件：不覆盖用户上传 / 新 Trace / 已修改的 SQLite
seed_tree() {
  _src="$1"
  _dst="$2"
  [ -d "$_src" ] || return 0
  mkdir -p "$_dst"
  find "$_src" -type f 2>/dev/null | while IFS= read -r _file; do
    _rel="${_file#"$_src"/}"
    _target="$_dst/$_rel"
    if [ ! -e "$_target" ]; then
      mkdir -p "$(dirname "$_target")"
      cp -p "$_file" "$_target"
    fi
  done
}

prepare_dirs() {
  _data="$1"
  _backend="$2"
  mkdir -p "$_data/traces" \
           "$_data/evaluation/traces" \
           "$_data/knowledge" \
           "$_data/hitl" \
           "$_backend/documents"
}

persist_ready=0
case "$DATA_MODE" in
  local)
    persist_ready=0
    ;;
  symlink)
    mkdir -p "$PERSIST_BASE/data" "$PERSIST_BASE/backend_data"
    persist_ready=1
    ;;
  *)
    if mkdir -p "$PERSIST_BASE/data" "$PERSIST_BASE/backend_data" 2>/dev/null \
       && [ -w "$PERSIST_BASE/data" ]; then
      persist_ready=1
    fi
    ;;
esac

if [ "$persist_ready" = "1" ]; then
  DATA_ROOT="$PERSIST_BASE/data"
  BACKEND_ROOT="$PERSIST_BASE/backend_data"
  prepare_dirs "$DATA_ROOT" "$BACKEND_ROOT"

  # 迁移：镜像内旧目录（若有）中缺失的文件补进持久化目录（不覆盖持久化目录已有文件）
  seed_tree "$APP_ROOT/data" "$DATA_ROOT"
  seed_tree "$APP_ROOT/backend/data" "$BACKEND_ROOT"

  if [ ! -f "$PERSIST_BASE/.seeded" ]; then
    seed_tree "$SEED_DIR/data" "$DATA_ROOT"
    seed_tree "$SEED_DIR/backend_data" "$BACKEND_ROOT"
    # 只有关键种子文件确实落盘才写完成标记；否则下次启动重试，避免半套数据成为“已完成”
    if [ -f "$DATA_ROOT/knowledge/knowledge_base.json" ] \
       && [ -f "$BACKEND_ROOT/tracepilot.db" ]; then
      if touch "$PERSIST_BASE/.seeded" 2>/dev/null; then
        log "首次启动：演示数据集已注入 $PERSIST_BASE"
      fi
    else
      log "警告：演示种子注入不完整（持久卷可能不可写），下次启动将重试"
    fi
  fi

  # 真实性整改迁移（v2）：把历史批量生成的执行记录/派生产物整体归档到持久卷，
  # 保留用户上传文件与业务数据库，再注入本次真实执行证据数据。
  MIGRATION_MARKER="$PERSIST_BASE/.migration_v2_authenticity"
  if [ ! -f "$MIGRATION_MARKER" ]; then
    ARCHIVE_DIR="$PERSIST_BASE/archive_legacy_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$ARCHIVE_DIR"
    for rel in traces agent_runs attribution failure_patterns pipelines quality_gate \
               regression version_comparison pareto rootcause_metrics governance \
               evaluation/traces evaluation/runs; do
      if [ -e "$DATA_ROOT/$rel" ]; then
        mkdir -p "$ARCHIVE_DIR/$(dirname "$rel")"
        mv "$DATA_ROOT/$rel" "$ARCHIVE_DIR/$rel"
      fi
    done
    if [ -f "$DATA_ROOT/hitl/approvals.json" ]; then
      mkdir -p "$ARCHIVE_DIR/hitl"
      mv "$DATA_ROOT/hitl/approvals.json" "$ARCHIVE_DIR/hitl/approvals.json"
    fi
    # 仅当文件资料目录只剩旧版种子测试文档时移入归档；用户上传文件一律保留
    LEGACY_DOC_ID="32a009bbbe244d96b8814889da8ffc6a"
    if [ -f "$BACKEND_ROOT/documents/$LEGACY_DOC_ID.json" ]; then
      DOC_COUNT=$(find "$BACKEND_ROOT/documents" -maxdepth 1 -name '*.json' -type f 2>/dev/null | wc -l | tr -d ' ')
      if [ "$DOC_COUNT" = "1" ]; then
        mkdir -p "$ARCHIVE_DIR/backend_data/documents"
        mv "$BACKEND_ROOT/documents/$LEGACY_DOC_ID.json" "$ARCHIVE_DIR/backend_data/documents/"
      fi
    fi
    seed_tree "$SEED_DIR/data" "$DATA_ROOT"
    seed_tree "$SEED_DIR/backend_data" "$BACKEND_ROOT"
    if touch "$MIGRATION_MARKER" 2>/dev/null; then
      log "真实性整改迁移完成：历史执行数据已归档到 $ARCHIVE_DIR"
    else
      log "警告：真实性整改迁移标记写入失败，下次启动将重试"
    fi
  fi

  # 一次性清理：旧实现会把「未授权真实 API 探测」写成无 Trace 的失败 Run，
  # 这里只删除验收脚本产生的那类记录（task 固定、mode=real_api、trace_id 为空）。
  PROBE_CLEANUP_MARKER="$PERSIST_BASE/.cleanup_step74_probe_runs"
  if [ ! -f "$PROBE_CLEANUP_MARKER" ]; then
    if [ -d "$DATA_ROOT/agent_runs" ]; then
      python - "$DATA_ROOT/agent_runs" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
removed = 0
for path in root.glob("*.json"):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    task = str(data.get("task") or "").strip()
    if data.get("mode") == "real_api" and not data.get("trace_id") and task in {"live probe", "smoke probe"}:
        path.unlink(missing_ok=True)
        removed += 1
if removed:
    print(f"[tracepilot] 清理无 Trace 的探测运行记录: {removed}")
PY
    fi
    touch "$PROBE_CLEANUP_MARKER" 2>/dev/null || true
  fi

  # V2 升级迁移：注入 V2 Benchmark 归档与 Judge 校准结果（seed_tree 只补缺失文件，
  # 不覆盖用户上传、新 Trace 与已有实验产物）。Benchmark 原始数据不进镜像，
  # 只注入实际运行产生的报告 JSON；缺文件时前端显示「尚未生成实验结果」。
  V2_MARKER="$PERSIST_BASE/.migration_v2_upgrade"
  if [ ! -f "$V2_MARKER" ]; then
    seed_tree "$SEED_DIR/data/v2_experiments" "$DATA_ROOT/v2_experiments"
    if touch "$V2_MARKER" 2>/dev/null; then
      log "V2 升级迁移完成：Benchmark 归档与 Judge 校准结果已注入"
    else
      log "警告：V2 迁移标记写入失败，下次启动将重试"
    fi
  fi

  # 应用默认路径 -> 持久化目录（业务代码无需感知）
  rm -rf "$APP_ROOT/data" "$APP_ROOT/backend/data"
  ln -sfn "$DATA_ROOT" "$APP_ROOT/data"
  ln -sfn "$BACKEND_ROOT" "$APP_ROOT/backend/data"
  log "数据持久化已启用: $PERSIST_BASE（/app/data、/app/backend/data 为符号链接）"
else
  DATA_ROOT="$APP_ROOT/data"
  BACKEND_ROOT="$APP_ROOT/backend/data"
  mkdir -p "$DATA_ROOT" "$BACKEND_ROOT"
  prepare_dirs "$DATA_ROOT" "$BACKEND_ROOT"
  seed_tree "$SEED_DIR/data" "$DATA_ROOT"
  seed_tree "$SEED_DIR/backend_data" "$BACKEND_ROOT"
  log "警告：未使用 /mnt/workspace 持久化（mode=$DATA_MODE）；容器重启后上传、评测与新 Trace 会丢失"
fi

# 公开演示默认值（创空间设置里显式配置的环境变量优先）
export TRACEPILOT_ENV="${TRACEPILOT_ENV:-production}"
export TRACEPILOT_PUBLIC_DEMO="${TRACEPILOT_PUBLIC_DEMO:-1}"
export TRACEPILOT_RATE_LIMIT_ENABLED="${TRACEPILOT_RATE_LIMIT_ENABLED:-1}"
export TRACEPILOT_TRUST_PROXY="${TRACEPILOT_TRUST_PROXY:-1}"
export TRACEPILOT_STATIC_DIR="${TRACEPILOT_STATIC_DIR:-$APP_ROOT/frontend/dist}"
if [ -z "${TRACEPILOT_DB_PATH:-}" ]; then
  export TRACEPILOT_DB_PATH="$BACKEND_ROOT/tracepilot.db"
fi

log "profile=${TRACEPILOT_ENV} public_demo=${TRACEPILOT_PUBLIC_DEMO} rate_limit=${TRACEPILOT_RATE_LIMIT_ENABLED} db_path=${TRACEPILOT_DB_PATH}"
log "static_dir=${TRACEPILOT_STATIC_DIR}"

exec "$@"
