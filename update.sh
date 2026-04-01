#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
SERVICE_NAME_FILE="${SCRIPT_DIR}/.service_name"

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=""
else
  SUDO="sudo"
fi

if [[ -f "${SERVICE_NAME_FILE}" ]]; then
  SERVICE_NAME="$(tr -d '[:space:]' < "${SERVICE_NAME_FILE}")"
else
  SERVICE_NAME="polymarket-bot"
fi

if [[ ! -d "${SCRIPT_DIR}/.git" ]]; then
  echo "Not a git repo: ${SCRIPT_DIR}" >&2
  exit 1
fi

if [[ ! -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
  echo "Missing virtualenv at ${SCRIPT_DIR}/.venv" >&2
  exit 1
fi

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

DB_FILE_REL="${DB_PATH:-bot.db}"
if [[ "${DB_FILE_REL}" = /* ]]; then
  DB_FILE="${DB_FILE_REL}"
else
  DB_FILE="${SCRIPT_DIR}/${DB_FILE_REL}"
fi

BACKUP_DIR="${SCRIPT_DIR}/backups"
mkdir -p "${BACKUP_DIR}"

if [[ -f "${DB_FILE}" ]]; then
  ts="$(date +%Y%m%d_%H%M%S)"
  backup_path="${BACKUP_DIR}/$(basename "${DB_FILE}").${ts}.bak"
  cp "${DB_FILE}" "${backup_path}"
  echo "DB backup created: ${backup_path}"
else
  echo "DB file not found, skipping backup: ${DB_FILE}"
fi

echo "Pulling latest code..."
git -C "${SCRIPT_DIR}" pull --ff-only

echo "Installing/updating dependencies..."
"${SCRIPT_DIR}/.venv/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"

echo "Restarting service: ${SERVICE_NAME}"
${SUDO} systemctl restart "${SERVICE_NAME}"

echo "Update complete."
${SUDO} systemctl --no-pager --lines=0 status "${SERVICE_NAME}" || true
echo
echo "Recent logs:"
${SUDO} journalctl -u "${SERVICE_NAME}" -n 30 --no-pager || true
