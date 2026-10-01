#!/usr/bin/env bash
# ops/tunnel_gpu.sh — GPU 隧道管理（start/restart/keepalive/stop/status）
#   用法: TUNNEL_LOCAL_PORT=9001 TUNNEL_REMOTE_PORT=9001 bash ops/tunnel_gpu.sh start
set -uo pipefail

cd "$(cd "$(dirname "$0")" && pwd)" || exit 1

: "${TUNNEL_LOCAL_PORT:=9001}"
: "${TUNNEL_REMOTE_PORT:=9001}"
: "${TUNNEL_SSH_HOST:=dev-env-with-gpu}"

PIDFILE="tmp/tunnel_${TUNNEL_LOCAL_PORT}.pid"
LOGFILE="tmp/tunnel_${TUNNEL_LOCAL_PORT}.log"
mkdir -p tmp

say() { echo "$@" | tee -a "$LOGFILE"; }

tunnel_pid() {
  if [ -f "$PIDFILE" ]; then
    cat "$PIDFILE" 2>/dev/null
  fi
}

is_alive() {
  local pid
  pid=$(tunnel_pid)
  if [ -n "$pid" ]; then
    # 优先用 kill -0（MSYS2 进程间可用）；失败则回退到端口探测
    if kill -0 "$pid" 2>/dev/null; then
      curl -sf -m 2 "http://127.0.0.1:${TUNNEL_LOCAL_PORT}/health" >/dev/null 2>&1
      return $?
    fi
  fi
  # PID 不存在或 kill -0 不可靠时，直接端口探测
  curl -sf -m 2 "http://127.0.0.1:${TUNNEL_LOCAL_PORT}/health" >/dev/null 2>&1
  return $?
}

start_tunnel() {
  local ssh_opts="-o StrictHostKeyChecking=no -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes"

  # 幂等：如果 already UP，直接返回
  if is_alive; then
    say "隧道 :${TUNNEL_LOCAL_PORT} 已就绪（PID=$(tunnel_pid)）"
    return 0
  fi

  say "启动隧道 :${TUNNEL_LOCAL_PORT} -> ${TUNNEL_SSH_HOST}:${TUNNEL_REMOTE_PORT}"

  # 清理旧隧道
  local old_pid
  old_pid=$(tunnel_pid)
  if [ -n "$old_pid" ]; then
    kill "$old_pid" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi

  nohup ssh -N -L "${TUNNEL_LOCAL_PORT}:127.0.0.1:${TUNNEL_REMOTE_PORT}" \
    $ssh_opts "$TUNNEL_SSH_HOST" >> "$LOGFILE" 2>&1 &

  local pid=$!
  echo "$pid" > "$PIDFILE"

  # 验证 PID 是否存活（Windows 下 nohup 子进程可能瞬时退出）
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    say "隧道 :${TUNNEL_LOCAL_PORT} 启动失败（PID=$pid 不存在，日志: $LOGFILE）"
    rm -f "$PIDFILE"
    return 1
  fi

  local i
  for i in 1 2 3 4 5 6; do
    sleep 2
    if curl -sf -m 2 "http://127.0.0.1:${TUNNEL_LOCAL_PORT}/health" >/dev/null 2>&1; then
      say "隧道 :${TUNNEL_LOCAL_PORT} 已就绪 (PID=$pid)"
      return 0
    fi
  done

  say "隧道 :${TUNNEL_LOCAL_PORT} 启动后 health check 失败（日志: $LOGFILE）"
  return 1
}

stop_tunnel() {
  local pid
  pid=$(tunnel_pid)
  if [ -n "$pid" ]; then
    say "停止隧道 :${TUNNEL_LOCAL_PORT} (PID=$pid)"
    kill "$pid" 2>/dev/null || true
    local i
    for i in 1 2 3; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
}

restart_tunnel() {
  say "重启隧道 :${TUNNEL_LOCAL_PORT}"
  stop_tunnel
  sleep 2
  start_tunnel
}

keepalive() {
  local interval="${KEEPALIVE_INTERVAL:-10}"

  # 单实例锁：同端口不并发叠加
  local lockfile="${PIDFILE}.lock"
  if [ -f "$lockfile" ]; then
    local holder
    holder=$(cat "$lockfile" 2>/dev/null || echo "")
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
      echo "keepalive :${TUNNEL_LOCAL_PORT} 已有实例 PID=$holder 在运行，退出"
      exit 0
    fi
    rm -f "$lockfile"
  fi
  echo $$ > "$lockfile"

  say "keepalive :${TUNNEL_LOCAL_PORT} 已启动 (PID=$$, interval=${interval}s)"

  while true; do
    if ! is_alive; then
      say "$(date '+%Y-%m-%d %H:%M:%S') 隧道 :${TUNNEL_LOCAL_PORT} 不可达，执行 restart..."
      restart_tunnel >> "$LOGFILE" 2>&1 || true
    fi
    sleep "$interval"
  done
}

case "${1:-}" in
  start)    start_tunnel ;;
  restart)  restart_tunnel ;;
  keepalive) keepalive ;;
  stop)     stop_tunnel ;;
  status)
    if is_alive; then
      say "隧道 :${TUNNEL_LOCAL_PORT} 运行中 (PID=$(tunnel_pid))"
      exit 0
    else
      say "隧道 :${TUNNEL_LOCAL_PORT} 已停止"
      exit 1
    fi
    ;;
  *)
    echo "Usage: $0 {start|restart|keepalive|stop|status}" >&2
    echo "Env: TUNNEL_LOCAL_PORT (default 9001), TUNNEL_REMOTE_PORT (default 9001), TUNNEL_SSH_HOST (default dev-env-with-gpu)" >&2
    exit 1
    ;;
esac
