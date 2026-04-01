#!/usr/bin/env bash
set -euo pipefail

DEFAULT_INSTALL_DIR="/opt/polymarket-bot"
DEFAULT_SERVICE_NAME="polymarket-bot"
DEFAULT_BRANCH="main"

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

run_as_user() {
  local user_name="$1"
  shift

  if [[ "$(id -un)" == "${user_name}" ]]; then
    "$@"
  else
    ${SUDO} -u "${user_name}" "$@"
  fi
}

prompt() {
  local message="$1"
  local default_value="${2:-}"
  local answer

  if [[ -n "${default_value}" ]]; then
    read -r -p "${message} [${default_value}]: " answer
    if [[ -z "${answer}" ]]; then
      answer="${default_value}"
    fi
  else
    read -r -p "${message}: " answer
  fi

  printf "%s" "${answer}"
}

require_command() {
  local name="$1"
  if ! command -v "${name}" >/dev/null 2>&1; then
    echo "Missing required command: ${name}" >&2
    exit 1
  fi
}

ensure_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing system packages..."
    ${SUDO} apt-get update
    ${SUDO} apt-get install -y python3 python3-venv python3-pip git ca-certificates
  else
    echo "apt-get not found. Install python3, python3-venv, pip, git manually."
  fi
}

create_or_update_repo() {
  local repo_url="$1"
  local git_branch="$2"
  local install_dir="$3"
  local run_user="$4"

  if [[ -d "${install_dir}/.git" ]]; then
    echo "Repo already exists at ${install_dir}, pulling latest..."
    run_as_user "${run_user}" git -C "${install_dir}" fetch --all --prune
    run_as_user "${run_user}" git -C "${install_dir}" checkout "${git_branch}"
    run_as_user "${run_user}" git -C "${install_dir}" pull --ff-only
    return
  fi

  if [[ -d "${install_dir}" ]] && [[ -n "$(ls -A "${install_dir}" 2>/dev/null)" ]]; then
    echo "Install dir is not empty: ${install_dir}" >&2
    echo "Please empty/remove it or choose another directory." >&2
    exit 1
  fi

  ${SUDO} mkdir -p "${install_dir}"
  ${SUDO} chown -R "${run_user}:${run_user}" "${install_dir}"
  run_as_user "${run_user}" git clone --branch "${git_branch}" "${repo_url}" "${install_dir}"
}

write_env_file() {
  local install_dir="$1"
  local bot_token="$2"

  local env_path="${install_dir}/.env"
  if [[ -f "${env_path}" ]]; then
    local ts
    ts="$(date +%Y%m%d_%H%M%S)"
    ${SUDO} cp "${env_path}" "${env_path}.bak.${ts}"
  fi

  ${SUDO} tee "${env_path}" >/dev/null <<EOF
BOT_TOKEN=${bot_token}
DB_PATH=bot.db
DATA_API_BASE=https://data-api.polymarket.com
CLOB_BASE=https://clob.polymarket.com
WS_URL=wss://ws-subscriptions-clob.polymarket.com/ws/market
WALLET_SYNC_SECONDS=45
WALLET_BACKFILL_LIMIT=80
WS_MARKET_LOOKUP_LIMIT=50
MAX_WS_ASSETS=600
REQUEST_TIMEOUT_SECONDS=15
EOF
}

write_service_file() {
  local service_name="$1"
  local run_user="$2"
  local install_dir="$3"
  local service_path="/etc/systemd/system/${service_name}.service"

  ${SUDO} tee "${service_path}" >/dev/null <<EOF
[Unit]
Description=Polymarket Wallet Follower Bot
After=network.target

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${install_dir}
EnvironmentFile=${install_dir}/.env
ExecStart=${install_dir}/.venv/bin/python ${install_dir}/main.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

main() {
  require_command bash

  echo "== Polymarket Bot Quick Install =="

  local repo_url
  local bot_token
  local install_dir
  local service_name
  local git_branch
  local run_user

  repo_url="$(prompt "Git repo URL (HTTPS or SSH)")"
  if [[ -z "${repo_url}" ]]; then
    echo "Repo URL is required." >&2
    exit 1
  fi

  bot_token="$(prompt "Telegram BOT_TOKEN")"
  if [[ -z "${bot_token}" ]]; then
    echo "BOT_TOKEN is required." >&2
    exit 1
  fi

  install_dir="$(prompt "Install directory" "${DEFAULT_INSTALL_DIR}")"
  service_name="$(prompt "Systemd service name" "${DEFAULT_SERVICE_NAME}")"
  git_branch="$(prompt "Git branch" "${DEFAULT_BRANCH}")"
  run_user="$(prompt "Linux user to run service" "${USER}")"

  if ! id "${run_user}" >/dev/null 2>&1; then
    echo "User does not exist: ${run_user}" >&2
    exit 1
  fi

  ensure_packages
  require_command git
  require_command python3

  create_or_update_repo "${repo_url}" "${git_branch}" "${install_dir}" "${run_user}"

  echo "Setting ownership and Python environment..."
  ${SUDO} chown -R "${run_user}:${run_user}" "${install_dir}"
  run_as_user "${run_user}" python3 -m venv "${install_dir}/.venv"
  run_as_user "${run_user}" "${install_dir}/.venv/bin/pip" install --upgrade pip
  run_as_user "${run_user}" "${install_dir}/.venv/bin/pip" install -r "${install_dir}/requirements.txt"

  write_env_file "${install_dir}" "${bot_token}"
  ${SUDO} chown "${run_user}:${run_user}" "${install_dir}/.env"

  echo "Creating systemd service..."
  write_service_file "${service_name}" "${run_user}" "${install_dir}"
  echo "${service_name}" | ${SUDO} tee "${install_dir}/.service_name" >/dev/null
  ${SUDO} chown "${run_user}:${run_user}" "${install_dir}/.service_name"

  ${SUDO} systemctl daemon-reload
  ${SUDO} systemctl enable "${service_name}"
  ${SUDO} systemctl restart "${service_name}"

  echo
  echo "Install complete. Useful commands:"
  echo "  sudo systemctl status ${service_name}"
  echo "  journalctl -u ${service_name} -f"
  echo "  cd ${install_dir} && ./update.sh"
}

main "$@"
