#!/usr/bin/env bash
# ============================================================
# ontoMeta 前后端服务管理脚本
# 用法:
#   ./service.sh start     一键启动后端和前端
#   ./service.sh stop      一键停止后端和前端
#   ./service.sh restart   一键重启后端和前端
#   ./service.sh status    查看服务状态
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$ROOT/.pids"
LOG_DIR="$ROOT/.logs"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"

BACKEND_PID_FILE="$PID_DIR/backend.pid"
FRONTEND_PID_FILE="$PID_DIR/frontend.pid"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------- 检查进程是否存活 ----------
is_running() {
  local pid_file="$1"
  if [ -f "$pid_file" ]; then
    local pid
    pid="$(cat "$pid_file")"
    kill -0 "$pid" 2>/dev/null && return 0
  fi
  return 1
}

# ---------- 等待服务就绪 ----------
wait_for_health() {
  local url="$1"
  local name="$2"
  local max_retries="${3:-30}"

  echo -n "[INFO] 等待 $name 就绪 ..."
  for i in $(seq 1 "$max_retries"); do
    if curl -sf "$url" > /dev/null 2>&1; then
      echo " OK"
      return 0
    fi
    sleep 1
    echo -n "."
  done
  echo " TIMEOUT"
  return 1
}

# ============================================================
# start
# ============================================================
do_start() {
  mkdir -p "$PID_DIR" "$LOG_DIR"

  # --- 后端 ---
  if is_running "$BACKEND_PID_FILE"; then
    log_warn "后端已在运行 (PID: $(cat "$BACKEND_PID_FILE"))，跳过启动"
  else
    rm -f "$BACKEND_PID_FILE"

    if [ ! -d "$BACKEND_DIR/.venv" ]; then
      log_error "后端虚拟环境不存在，请先运行: make install-backend"
      exit 1
    fi

    log_info "启动后端 (FastAPI :8000) ..."
    nohup bash -c '
      cd "'"$BACKEND_DIR"'" &&
      . .venv/bin/activate &&
      uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
    ' > "$LOG_DIR/backend.log" 2>&1 &
    echo "$!" > "$BACKEND_PID_FILE"
    log_info "后端已启动 (PID: $(cat "$BACKEND_PID_FILE"))，日志: $LOG_DIR/backend.log"

    wait_for_health "http://127.0.0.1:8000/health" "后端 (Backend)" 30 || \
      log_warn "后端启动超时，请检查日志: $LOG_DIR/backend.log"
  fi

  # --- 前端 ---
  if is_running "$FRONTEND_PID_FILE"; then
    log_warn "前端已在运行 (PID: $(cat "$FRONTEND_PID_FILE"))，跳过启动"
  else
    rm -f "$FRONTEND_PID_FILE"

    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
      log_error "前端依赖不存在，请先运行: make install-frontend"
      exit 1
    fi

    log_info "启动前端 (Vite :5180) ..."
    nohup bash -c '
      cd "'"$FRONTEND_DIR"'" &&
      npm run dev
    ' > "$LOG_DIR/frontend.log" 2>&1 &
    echo "$!" > "$FRONTEND_PID_FILE"
    log_info "前端已启动 (PID: $(cat "$FRONTEND_PID_FILE"))，日志: $LOG_DIR/frontend.log"

    wait_for_health "http://127.0.0.1:5180" "前端 (Frontend)" 20 || \
      log_warn "前端启动超时，请检查日志: $LOG_DIR/frontend.log"
  fi

  echo ""
  echo "=========================================="
  echo "  ontoMeta 启动完成"
  echo "  后端 API:  http://localhost:8000/docs"
  echo "  前端页面:  http://localhost:5180"
  echo "=========================================="
}

# ============================================================
# stop
# ============================================================
do_stop() {
  if [ ! -d "$PID_DIR" ]; then
    log_info "没有找到 PID 目录，服务可能未运行"
    return 0
  fi

  _stop_one() {
    local pid_file="$1"
    local name="$2"

    if [ ! -f "$pid_file" ]; then
      return 0
    fi

    local pid
    pid="$(cat "$pid_file")"

    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      return 0
    fi

    echo -n "[INFO] 停止 $name (PID: $pid) ..."
    kill "$pid" 2>/dev/null || true

    for i in $(seq 1 10); do
      if ! kill -0 "$pid" 2>/dev/null; then
        echo " 已停止"
        rm -f "$pid_file"
        return 0
      fi
      sleep 1
    done

    echo -n " 优雅关闭超时，强制终止 ..."
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      echo " 失败"
      return 1
    else
      echo " 已强制停止"
      rm -f "$pid_file"
      return 0
    fi
  }

  _stop_one "$BACKEND_PID_FILE" "后端 (Backend)"
  _stop_one "$FRONTEND_PID_FILE" "前端 (Frontend)"

  rmdir "$PID_DIR" 2>/dev/null || true
  log_info "所有服务已停止"
}

# ============================================================
# status
# ============================================================
do_status() {
  echo "=========================================="
  echo "  ontoMeta 服务状态"
  echo "=========================================="

  _status_one() {
    local pid_file="$1"
    local name="$2"

    if is_running "$pid_file"; then
      local pid
      pid="$(cat "$pid_file")"
      echo -e "  $name: ${GREEN}运行中${NC} (PID: $pid)"
    else
      echo -e "  $name: ${RED}未运行${NC}"
    fi
  }

  _status_one "$BACKEND_PID_FILE" "后端 (Backend :8000)"
  _status_one "$FRONTEND_PID_FILE" "前端 (Frontend :5180)"
  echo ""
}

# ============================================================
# 入口
# ============================================================
usage() {
  echo "用法: $0 {start|stop|restart|status}"
  echo ""
  echo "  start    一键启动后端和前端"
  echo "  stop     一键停止后端和前端"
  echo "  restart  一键重启后端和前端"
  echo "  status   查看服务运行状态"
  exit 1
}

case "${1:-}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; echo ""; sleep 1; do_start ;;
  status)  do_status ;;
  *)       usage ;;
esac
