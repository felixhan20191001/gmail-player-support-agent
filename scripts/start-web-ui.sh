#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="${ROOT_DIR}/scripts"
VENV_PY="${ROOT_DIR}/.venv/bin/python"
WEB_BIN="${ROOT_DIR}/.venv/bin/player-support-web"
PID_DIR="${ROOT_DIR}/var"
LLAMA_PID_FILE="${PID_DIR}/llama-server.web-ui.pid"
LOG_FILE="${PID_DIR}/llama-server.web-ui.log"

# Defaults (override in scripts/web-ui.config.local.sh)
CLOUD_API_KEY=""
CLOUD_MODEL="gpt-4o-mini"
CLOUD_BASE_URL="https://api.openai.com/v1"
LLAMA_SERVER_BIN="/opt/homebrew/bin/llama-server"
GGUF_PATH="/Users/hanpengfei/models/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf"
LLAMA_HOST="127.0.0.1"
LLAMA_PORT="8080"
LLAMA_NGL="999"
WEB_HOST="127.0.0.1"
WEB_PORT="8090"
CONFIG_PATH="config/config.local.toml"

LOCAL_CONFIG="${SCRIPT_DIR}/web-ui.config.local.sh"
if [[ -f "${LOCAL_CONFIG}" ]]; then
  # shellcheck source=/dev/null
  source "${LOCAL_CONFIG}"
else
  echo "提示: 未找到 ${LOCAL_CONFIG}"
  echo "      请复制 scripts/web-ui.config.local.sh.example 并填写云模型 API Key："
  echo "      cp scripts/web-ui.config.local.sh.example scripts/web-ui.config.local.sh"
  echo
fi

started_llama="false"

cleanup() {
  if [[ "${started_llama}" == "true" && -f "${LLAMA_PID_FILE}" ]]; then
    local pid
    pid="$(cat "${LLAMA_PID_FILE}")"
    if kill -0 "${pid}" 2>/dev/null; then
      echo
      echo "正在停止 llama-server (pid=${pid})..."
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
    rm -f "${LLAMA_PID_FILE}"
  fi
}
trap cleanup EXIT INT TERM

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "错误: 未找到命令 $1" >&2
    exit 1
  fi
}

llama_base_url() {
  echo "http://${LLAMA_HOST}:${LLAMA_PORT}/v1"
}

llama_is_healthy() {
  curl -fsS "$(llama_base_url)/models" >/dev/null 2>&1
}

web_port_in_use() {
  lsof -nP -iTCP:"${WEB_PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

ensure_web_port_free() {
  if ! web_port_in_use; then
    return 0
  fi
  local pid cmd
  pid="$(lsof -tiTCP:"${WEB_PORT}" -sTCP:LISTEN 2>/dev/null | head -n 1 || true)"
  cmd="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
  echo "错误: 端口 ${WEB_PORT} 已被占用，无法启动 Web 控制台。" >&2
  if [[ -n "${pid}" ]]; then
    echo "占用进程: pid=${pid}" >&2
    if [[ -n "${cmd}" ]]; then
      echo "命令: ${cmd}" >&2
    fi
    echo "可先释放端口再重试，例如:" >&2
    echo "  kill ${pid}" >&2
    echo "或强制结束:" >&2
    echo "  lsof -ti :${WEB_PORT} | xargs kill -9" >&2
  fi
  exit 1
}

start_llama_server() {
  if llama_is_healthy; then
    echo "检测到 llama-server 已在 $(llama_base_url) 运行，跳过启动。"
    return 0
  fi

  if [[ ! -x "${LLAMA_SERVER_BIN}" ]]; then
    echo "错误: llama-server 不可执行: ${LLAMA_SERVER_BIN}" >&2
    echo "请在 scripts/web-ui.config.local.sh 中设置 LLAMA_SERVER_BIN" >&2
    exit 1
  fi
  if [[ ! -f "${GGUF_PATH}" ]]; then
    echo "错误: GGUF 模型文件不存在: ${GGUF_PATH}" >&2
    exit 1
  fi

  mkdir -p "${PID_DIR}"
  echo "正在启动 llama-server..."
  echo "  模型: ${GGUF_PATH}"
  echo "  地址: $(llama_base_url)"
  nohup "${LLAMA_SERVER_BIN}" \
    -m "${GGUF_PATH}" \
    --jinja \
    -ngl "${LLAMA_NGL}" \
    --host "${LLAMA_HOST}" \
    --port "${LLAMA_PORT}" \
    >"${LOG_FILE}" 2>&1 &
  echo $! >"${LLAMA_PID_FILE}"
  started_llama="true"

  local attempt
  for attempt in $(seq 1 60); do
    if llama_is_healthy; then
      echo "llama-server 已就绪。"
      return 0
    fi
    sleep 2
  done

  echo "错误: llama-server 启动超时。查看日志: ${LOG_FILE}" >&2
  exit 1
}

choose_runtime() {
  local choice
  while true; do
    {
      echo
      echo "========================================"
      echo "  Player Support Web 控制台 - 选择模型"
      echo "========================================"
      echo
      echo "  [1] 云模型 (DeepSeek / OpenAI 兼容 API)"
      echo "      模型: ${CLOUD_MODEL}"
      echo "      地址: ${CLOUD_BASE_URL}"
      echo "      说明: 使用 scripts/web-ui.config.local.sh 中的 CLOUD_API_KEY"
      echo
      echo "  [2] 本地模型 (llama-server)"
      echo "      文件: ${GGUF_PATH}"
      echo "      地址: http://${LLAMA_HOST}:${LLAMA_PORT}/v1"
      echo "      说明: 脚本会自动启动 llama-server 并加载 GGUF"
      echo
      echo "----------------------------------------"
    } >&2
    read -r -p "请输入选项 [1=云模型 / 2=本地模型，直接回车默认选 1]: " choice </dev/tty
    choice="${choice:-1}"
    case "${choice}" in
      1)
        echo "cloud"
        return 0
        ;;
      2)
        echo "local"
        return 0
        ;;
      *)
        echo "无效选择「${choice}」，请输入 1 或 2。" >&2
        ;;
    esac
  done
}

main() {
  cd "${ROOT_DIR}"

  if [[ ! -x "${VENV_PY}" || ! -x "${WEB_BIN}" ]]; then
    echo "错误: 未找到虚拟环境，请先在项目根目录执行:" >&2
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'" >&2
    exit 1
  fi

  local runtime
  runtime="$(choose_runtime)"
  echo

  local -a web_args=(
    --config "${CONFIG_PATH}"
    --host "${WEB_HOST}"
    --port "${WEB_PORT}"
  )

  if [[ "${runtime}" == "cloud" ]]; then
    if [[ -z "${CLOUD_API_KEY}" ]]; then
      echo "错误: 云模型 API Key 为空。" >&2
      echo "请在 scripts/web-ui.config.local.sh 中设置 CLOUD_API_KEY。" >&2
      exit 1
    fi
    export OPENAI_API_KEY="${CLOUD_API_KEY}"
    mkdir -p "${PID_DIR}/player_support_agent/cloud_model_keys"
    STARTUP_KEY_FILE="${PID_DIR}/player_support_agent/cloud_model_keys/startup.key"
    printf '%s' "${CLOUD_API_KEY}" >"${STARTUP_KEY_FILE}"
    chmod 600 "${STARTUP_KEY_FILE}" 2>/dev/null || true
    web_args+=(
      --profile cloud
      --model "${CLOUD_MODEL}"
      --base-url "${CLOUD_BASE_URL}"
      --api-key-file "${STARTUP_KEY_FILE}"
    )
    echo "使用云模型: ${CLOUD_MODEL}"
    echo "Base URL: ${CLOUD_BASE_URL}"
  else
    web_args+=(
      --backend llamaserver
      --base-url "$(llama_base_url)"
      --gguf "${GGUF_PATH}"
      --llamafile-mode prompt
    )
    echo "使用本地模型配置: ${GGUF_PATH}"
    echo "llama-server 将由 WebUI 启动并托管: $(llama_base_url)"
  fi

  ensure_web_port_free

  echo
  echo "启动 Web 控制台: http://${WEB_HOST}:${WEB_PORT}"
  echo "按 Ctrl+C 退出（本地模式会同时停止本次 WebUI 启动的 llama-server）"
  echo
  exec "${WEB_BIN}" "${web_args[@]}"
}

main "$@"
