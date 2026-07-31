#!/usr/bin/env bash
# 本地开发服务器管理脚本（start / stop / restart / status / logs）
#
# 用 PID 文件精确管理进程，避免 `pkill -f uvicorn` 误伤同名命令行的其他进程
# （在 agent/CI 环境中 pkill 甚至会杀掉自己所在的 shell）。
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="${BACKEND_DIR}/.devserver.pid"
LOG_FILE="${LOG_FILE:-/tmp/agentvalue-backend.log}"
PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
PYTHON="${BACKEND_DIR}/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start() {
  if is_running; then
    echo "already running (pid $(cat "$PID_FILE"))"
    return 0
  fi
  cd "$BACKEND_DIR"
  setsid nohup "$PYTHON" -m uvicorn main:app --host "$HOST" --port "$PORT" \
    > "$LOG_FILE" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
  echo "starting pid $(cat "$PID_FILE") on ${HOST}:${PORT}, log=${LOG_FILE}"

  for _ in $(seq 1 40); do
    if curl -sf -m 2 "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
      echo "ready"
      return 0
    fi
    sleep 1
  done
  echo "TIMEOUT: service did not become healthy in 40s" >&2
  tail -30 "$LOG_FILE" >&2
  return 1
}

stop() {
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 15); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    echo "stopped pid $pid"
  else
    echo "not running"
  fi
  rm -f "$PID_FILE"
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  is_running && echo "running (pid $(cat "$PID_FILE"))" || { echo "stopped"; exit 1; } ;;
  logs)    tail -n "${2:-80}" "$LOG_FILE" ;;
  *)       echo "usage: $0 {start|stop|restart|status|logs [N]}" >&2; exit 2 ;;
esac
