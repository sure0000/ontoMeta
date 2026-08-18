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

BACKEND_PORT=8000
FRONTEND_PORT=5180

# ============================================================
# 引导期环境变量（无 .env 文件：业务/连接/执行配置一律在设置页配，落库；
# 见 docs/DEVELOPMENT_PRINCIPLES.md）。仅当外部 shell 未提供时给可移植默认值。
# Flink 执行参数已迁到【设置页 → Airflow/Flink】，不再走环境变量。
# ============================================================
: "${ONTOMETA_ADMIN_TOKEN:=dev-admin-token-change-me}"; export ONTOMETA_ADMIN_TOKEN

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

# ---------- 列出监听指定 TCP 端口的进程 PID（IPv4+IPv6 都算） ----------
# nohup 的 bash wrapper 退出后，uvicorn/vite 真身会 reparent 到 init(ppid=1)，
# 记录的 wrapper PID 随即失效——只认 PID 会漏杀导致僵尸占端口累积。按端口兜底。
port_pids() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN -n -P 2>/dev/null || true
}

# ---------- 清理占用指定端口的进程（先 TERM 后 KILL） ----------
kill_port() {
  local port="$1"
  local pids
  pids="$(port_pids "$port")"
  [ -z "$pids" ] && return 0
  kill $pids 2>/dev/null || true
  sleep 2
  pids="$(port_pids "$port")"
  if [ -n "$pids" ]; then
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
  return 0
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

# ---------- 启动后核验：端口真绑定了才算起来 ----------
# 健康检查失败时区分两种情况：端口已监听=仅初始化慢(保留)；端口未监听=启动失败，
# 清掉误导性的死 PID 文件并打印日志尾部，避免“记录了 PID 却没服务”的静默失败。
verify_started() {
  local pid_file="$1"
  local name="$2"
  local port="$3"
  local log_file="$4"

  if [ -n "$(port_pids "$port")" ]; then
    log_warn "$name 健康检查未通过但端口 $port 已监听，可能仍在初始化，请查看日志: $log_file"
    return 0
  fi

  log_error "$name 启动失败：端口 $port 未监听。日志尾部："
  tail -n 20 "$log_file" 2>/dev/null || true
  rm -f "$pid_file"
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

    # 启动前清理占用端口的陈旧进程（PID 文件可能因 wrapper 退出而失效，按端口兜底）。
    kill_port "$BACKEND_PORT"

    log_info "启动后端 (FastAPI :$BACKEND_PORT) ..."
    nohup bash -c '
      cd "'"$BACKEND_DIR"'" &&
      . .venv/bin/activate &&
      uvicorn app.main:app --reload --reload-dir app --host 0.0.0.0 --port '"$BACKEND_PORT"'
    ' > "$LOG_DIR/backend.log" 2>&1 &
    echo "$!" > "$BACKEND_PID_FILE"
    log_info "后端已启动 (PID: $(cat "$BACKEND_PID_FILE"))，日志: $LOG_DIR/backend.log"

    wait_for_health "http://localhost:$BACKEND_PORT/health" "后端 (Backend)" 30 || \
      verify_started "$BACKEND_PID_FILE" "后端 (Backend)" "$BACKEND_PORT" "$LOG_DIR/backend.log" || true
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

    # 启动前清理占用端口的陈旧进程（曾出现两个陈旧 vite 各绑 IPv4/IPv6 同占 5180）。
    kill_port "$FRONTEND_PORT"

    log_info "启动前端 (Vite :$FRONTEND_PORT) ..."
    nohup bash -c '
      cd "'"$FRONTEND_DIR"'" &&
      npm run dev
    ' > "$LOG_DIR/frontend.log" 2>&1 &
    echo "$!" > "$FRONTEND_PID_FILE"
    log_info "前端已启动 (PID: $(cat "$FRONTEND_PID_FILE"))，日志: $LOG_DIR/frontend.log"

    # 用 localhost 探活：vite 默认只监听 IPv6 [::1]，写死 127.0.0.1 会假超时。
    wait_for_health "http://localhost:$FRONTEND_PORT" "前端 (Frontend)" 20 || \
      verify_started "$FRONTEND_PID_FILE" "前端 (Frontend)" "$FRONTEND_PORT" "$LOG_DIR/frontend.log" || true
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
  # 注意：即使 PID 目录缺失也要继续——残留进程常在 wrapper 退出、PID 文件被清后
  # 仍按端口占用；_stop_one 内部会对缺失的 PID 文件安全跳过并按端口兜底清理。
  _stop_one() {
    local pid_file="$1"
    local name="$2"
    local port="$3"

    # 1) 按 PID 文件优雅停止（wrapper 或直接进程）
    if [ -f "$pid_file" ]; then
      local pid
      pid="$(cat "$pid_file")"
      if kill -0 "$pid" 2>/dev/null; then
        echo -n "[INFO] 停止 $name (PID: $pid) ..."
        kill "$pid" 2>/dev/null || true
        for i in $(seq 1 10); do
          if ! kill -0 "$pid" 2>/dev/null; then
            echo " 已停止"
            break
          fi
          sleep 1
        done
        if kill -0 "$pid" 2>/dev/null; then
          echo -n " 优雅关闭超时，强制终止 ..."
          kill -9 "$pid" 2>/dev/null || true
          echo " 已强制停止"
        fi
      fi
      rm -f "$pid_file"
    fi

    # 2) 端口兜底：wrapper 退出后真身 reparent 到 init，PID 文件失效，只能按端口清。
    local leftover
    leftover="$(port_pids "$port")"
    if [ -n "$leftover" ]; then
      echo "[INFO] 清理占用 $name 端口 $port 的残留进程 (PID: $(echo $leftover | tr '\n' ' '))"
      kill_port "$port"
    fi
    return 0
  }

  _stop_one "$BACKEND_PID_FILE" "后端 (Backend)" "$BACKEND_PORT"
  _stop_one "$FRONTEND_PID_FILE" "前端 (Frontend)" "$FRONTEND_PORT"

  # 草稿生成跑在分离子进程（start_new_session），不随后端一起退出。stop/restart 时
  # 一并清理，避免残留 worker 继续写库；其 DB 任务状态由下次启动的陈旧宽限窗口回收。
  if pgrep -f "app.jobs.draft_worker" >/dev/null 2>&1; then
    pkill -f "app.jobs.draft_worker" 2>/dev/null || true
    log_info "已清理分离的草稿生成子进程 (app.jobs.draft_worker)"
  fi

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
    local port="$3"

    if is_running "$pid_file"; then
      local pid
      pid="$(cat "$pid_file")"
      echo -e "  $name: ${GREEN}运行中${NC} (PID: $pid)"
    elif [ -n "$(port_pids "$port")" ]; then
      # PID 文件失效但端口仍被占：多为 reparent 到 init 的残留进程，需 stop 清理。
      echo -e "  $name: ${YELLOW}残留进程占用端口 $port${NC} (PID: $(port_pids "$port" | tr '\n' ' ')) — 建议 ./service.sh stop"
    else
      echo -e "  $name: ${RED}未运行${NC}"
    fi
  }

  _status_one "$BACKEND_PID_FILE" "后端 (Backend :$BACKEND_PORT)" "$BACKEND_PORT"
  _status_one "$FRONTEND_PID_FILE" "前端 (Frontend :$FRONTEND_PORT)" "$FRONTEND_PORT"
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
