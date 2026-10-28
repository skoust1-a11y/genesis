#!/usr/bin/env bash
# ==============================================================================
# Genesis Platform Manager (Backend: 8841 | Frontend: 3000 | Live Scanner)
# Usage:
#   ./genesis.sh start    - Start backend, frontend, and live scanner in background
#   ./genesis.sh stop     - Stop backend, frontend, and live scanner services
#   ./genesis.sh restart  - Restart all services
#   ./genesis.sh status   - Show running status and health
#   ./genesis.sh logs [backend|frontend|scanner] - Tail logs
# ==============================================================================

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
FRONTEND_DIR="${ROOT_DIR}/frontend"
LOGS_DIR="${ROOT_DIR}/logs"
PID_DIR="${LOGS_DIR}/pids"

BACKEND_PORT=8841
FRONTEND_PORT=3000

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

mkdir -p "${LOGS_DIR}" "${PID_DIR}"

find_port_pid() {
  local port=$1
  lsof -ti :"$port" 2>/dev/null || true
}

find_scanner_pids() {
  if [ -f "${PID_DIR}/scanner.pid" ]; then
    local pid
    pid=$(cat "${PID_DIR}/scanner.pid" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "$pid"
      return
    fi
  fi
  pgrep -f "scripts/live_scanner.py" 2>/dev/null || true
}

get_backend_cmd() {
  if command -v uv >/dev/null 2>&1; then
    echo "uv run uvicorn app.main:app --host 0.0.0.0 --port ${BACKEND_PORT} --reload --reload-dir app"
  elif [ -f "${BACKEND_DIR}/.venv/bin/uvicorn" ]; then
    echo "${BACKEND_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${BACKEND_PORT} --reload --reload-dir app"
  else
    echo "python3 -m uvicorn app.main:app --host 0.0.0.0 --port ${BACKEND_PORT} --reload --reload-dir app"
  fi
}

get_scanner_cmd() {
  if [ -f "${BACKEND_DIR}/.venv/bin/python" ]; then
    echo "${BACKEND_DIR}/.venv/bin/python ${ROOT_DIR}/scripts/live_scanner.py"
  elif command -v uv >/dev/null 2>&1; then
    echo "uv run --project ${BACKEND_DIR} python ${ROOT_DIR}/scripts/live_scanner.py"
  else
    echo "python3 ${ROOT_DIR}/scripts/live_scanner.py"
  fi
}

start_services() {
  echo -e "${BLUE}=== Starting Genesis Services ===${NC}"

  # 1. Start Backend
  local existing_backend_pid
  existing_backend_pid=$(find_port_pid "${BACKEND_PORT}")
  if [ -n "${existing_backend_pid}" ]; then
    echo -e "${YELLOW}Backend is already running on port ${BACKEND_PORT} (PID: ${existing_backend_pid})${NC}"
  else
    echo -e "Starting Backend on ${CYAN}http://127.0.0.1:${BACKEND_PORT}${NC}..."
    local backend_cmd
    backend_cmd=$(get_backend_cmd)
    (
      cd "${BACKEND_DIR}"
      nohup ${backend_cmd} > "${LOGS_DIR}/backend.log" 2>&1 &
      echo $! > "${PID_DIR}/backend.pid"
    )
  fi

  # 2. Start Frontend
  local existing_frontend_pid
  existing_frontend_pid=$(find_port_pid "${FRONTEND_PORT}")
  if [ -n "${existing_frontend_pid}" ]; then
    echo -e "${YELLOW}Frontend is already running on port ${FRONTEND_PORT} (PID: ${existing_frontend_pid})${NC}"
  else
    echo -e "Starting Frontend on ${CYAN}http://localhost:${FRONTEND_PORT}${NC}..."
    (
      cd "${FRONTEND_DIR}"
      if [ -x "./node_modules/.bin/next" ]; then
        nohup ./node_modules/.bin/next dev -p "${FRONTEND_PORT}" </dev/null > "${LOGS_DIR}/frontend.log" 2>&1 &
      else
        nohup npx next dev -p "${FRONTEND_PORT}" </dev/null > "${LOGS_DIR}/frontend.log" 2>&1 &
      fi
      echo $! > "${PID_DIR}/frontend.pid"
    )
  fi

  # 3. Start Live Scanner Daemon
  local existing_scanner_pid
  existing_scanner_pid=$(find_scanner_pids)
  if [ -n "${existing_scanner_pid}" ]; then
    echo -e "${YELLOW}Live Scanner is already running (PID: ${existing_scanner_pid})${NC}"
  else
    echo -e "Starting Live Scanner Daemon..."
    local scanner_cmd
    scanner_cmd=$(get_scanner_cmd)
    (
      cd "${ROOT_DIR}"
      nohup env PYTHONUNBUFFERED=1 ${scanner_cmd} > "${LOGS_DIR}/live_scanner.log" 2>&1 &
      echo $! > "${PID_DIR}/scanner.pid"
    )
  fi

  # 4. Wait and Verify Health
  echo -e "\nWaiting for services to become healthy..."
  local backend_ready=false
  for _ in {1..20}; do
    if curl -s -f "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1; then
      backend_ready=true
      break
    fi
    sleep 0.5
  done

  local frontend_ready=false
  for _ in {1..20}; do
    if curl -s -f "http://localhost:${FRONTEND_PORT}" >/dev/null 2>&1; then
      frontend_ready=true
      break
    fi
    sleep 0.5
  done

  echo ""
  if [ "$backend_ready" = true ]; then
    echo -e "  [${GREEN}OK${NC}] Backend API:   ${GREEN}http://127.0.0.1:${BACKEND_PORT}${NC} (Docs: http://127.0.0.1:${BACKEND_PORT}/docs)"
  else
    echo -e "  [${RED}FAIL${NC}] Backend API: Not responding yet. Check: ${LOGS_DIR}/backend.log"
  fi

  if [ "$frontend_ready" = true ]; then
    echo -e "  [${GREEN}OK${NC}] Frontend UI:   ${GREEN}http://localhost:${FRONTEND_PORT}${NC}"
  else
    echo -e "  [${YELLOW}WAIT${NC}] Frontend UI: Still starting up or building. Check: ${LOGS_DIR}/frontend.log"
  fi

  local scanner_pids
  scanner_pids=$(find_scanner_pids)
  if [ -n "${scanner_pids}" ]; then
    echo -e "  [${GREEN}OK${NC}] Live Scanner:  ${GREEN}RUNNING${NC} (PID: ${scanner_pids}) | Log: ${LOGS_DIR}/live_scanner.log"
  else
    echo -e "  [${YELLOW}WARN${NC}] Live Scanner: Not responding. Check: ${LOGS_DIR}/live_scanner.log"
  fi

  echo -e "\n${GREEN}Genesis services are now running.${NC}"
}

stop_services() {
  echo -e "${BLUE}=== Stopping Genesis Services ===${NC}"

  # Stop Live Scanner
  local scanner_pids
  scanner_pids=$(find_scanner_pids)
  if [ -n "${scanner_pids}" ]; then
    echo -e "Stopping Live Scanner (PIDs: ${scanner_pids})..."
    kill ${scanner_pids} 2>/dev/null || true
    sleep 1
    scanner_pids=$(find_scanner_pids)
    if [ -n "${scanner_pids}" ]; then
      kill -9 ${scanner_pids} 2>/dev/null || true
    fi
    echo -e "  [${GREEN}STOPPED${NC}] Live Scanner"
  else
    echo -e "  Live Scanner was not running."
  fi

  # Stop Backend
  local backend_pids
  backend_pids=$(find_port_pid "${BACKEND_PORT}")
  if [ -n "${backend_pids}" ]; then
    echo -e "Stopping Backend on port ${BACKEND_PORT} (PIDs: ${backend_pids})..."
    kill ${backend_pids} 2>/dev/null || true
    sleep 1
    backend_pids=$(find_port_pid "${BACKEND_PORT}")
    if [ -n "${backend_pids}" ]; then
      kill -9 ${backend_pids} 2>/dev/null || true
    fi
    echo -e "  [${GREEN}STOPPED${NC}] Backend (Port ${BACKEND_PORT})"
  else
    echo -e "  Backend was not running on port ${BACKEND_PORT}."
  fi

  # Stop Frontend
  local frontend_pids
  frontend_pids=$(find_port_pid "${FRONTEND_PORT}")
  if [ -n "${frontend_pids}" ]; then
    echo -e "Stopping Frontend on port ${FRONTEND_PORT} (PIDs: ${frontend_pids})..."
    kill ${frontend_pids} 2>/dev/null || true
    sleep 1
    frontend_pids=$(find_port_pid "${FRONTEND_PORT}")
    if [ -n "${frontend_pids}" ]; then
      kill -9 ${frontend_pids} 2>/dev/null || true
    fi
    echo -e "  [${GREEN}STOPPED${NC}] Frontend (Port ${FRONTEND_PORT})"
  else
    echo -e "  Frontend was not running on port ${FRONTEND_PORT}."
  fi

  # Cleanup PID files
  rm -f "${PID_DIR}/backend.pid" "${PID_DIR}/frontend.pid" "${PID_DIR}/scanner.pid"
  echo -e "${GREEN}All services stopped cleanly.${NC}"
}

status_services() {
  echo -e "${BLUE}=== Genesis Service Status ===${NC}"

  local backend_pids
  backend_pids=$(find_port_pid "${BACKEND_PORT}")
  if [ -n "${backend_pids}" ]; then
    local health
    health=$(curl -s "http://127.0.0.1:${BACKEND_PORT}/api/health" 2>/dev/null || echo "offline")
    echo -e "Backend:  [${GREEN}RUNNING${NC}] Port: ${BACKEND_PORT} | PIDs: ${backend_pids} | Health: ${health}"
  else
    echo -e "Backend:  [${RED}STOPPED${NC}] Port: ${BACKEND_PORT}"
  fi

  local frontend_pids
  frontend_pids=$(find_port_pid "${FRONTEND_PORT}")
  if [ -n "${frontend_pids}" ]; then
    echo -e "Frontend: [${GREEN}RUNNING${NC}] Port: ${FRONTEND_PORT} | PIDs: ${frontend_pids}"
  else
    echo -e "Frontend: [${RED}STOPPED${NC}] Port: ${FRONTEND_PORT}"
  fi

  local scanner_pids
  scanner_pids=$(find_scanner_pids)
  if [ -n "${scanner_pids}" ]; then
    echo -e "Scanner:  [${GREEN}RUNNING${NC}] PIDs: ${scanner_pids} | Log: ${LOGS_DIR}/live_scanner.log"
  else
    echo -e "Scanner:  [${RED}STOPPED${NC}]"
  fi
}

tail_logs() {
  local target=${1:-all}
  case "$target" in
    backend|api)
      tail -f "${LOGS_DIR}/backend.log"
      ;;
    frontend|ui)
      tail -f "${LOGS_DIR}/frontend.log"
      ;;
    scanner|live)
      touch "${LOGS_DIR}/live_scanner.log"
      tail -f "${LOGS_DIR}/live_scanner.log"
      ;;
    *)
      touch "${LOGS_DIR}/live_scanner.log"
      tail -f "${LOGS_DIR}/backend.log" "${LOGS_DIR}/frontend.log" "${LOGS_DIR}/live_scanner.log"
      ;;
  esac
}

case "$1" in
  start)
    start_services
    ;;
  stop)
    stop_services
    ;;
  restart)
    stop_services
    sleep 1
    start_services
    ;;
  status)
    status_services
    ;;
  logs)
    tail_logs "$2"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs [backend|frontend|scanner]}"
    exit 1
    ;;
esac
