#!/usr/bin/env bash
# Install crypto-shit-exploder as a user service.
#
#   bash deploy/install.sh            # install and start
#   bash deploy/install.sh --no-start # install without starting
#
# Idempotent: re-running upgrades the venv and restarts the unit.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT="cse.service"
START=1

for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

echo "==> repo: ${REPO}"

if [[ ! -d "${REPO}/.venv" ]]; then
  echo "==> creating venv"
  python3 -m venv "${REPO}/.venv"
fi

echo "==> installing dependencies"
"${REPO}/.venv/bin/pip" install --quiet --upgrade pip
"${REPO}/.venv/bin/pip" install --quiet -r "${REPO}/requirements.txt"

if [[ ! -f "${REPO}/.env" && -f "${REPO}/.env.example" ]]; then
  echo "==> no .env found; copying .env.example (the run works without keys)"
  cp "${REPO}/.env.example" "${REPO}/.env"
  chmod 600 "${REPO}/.env"
fi

mkdir -p "${REPO}/data" "${UNIT_DIR}"
echo "==> installing ${UNIT}"
install -m 644 "${REPO}/deploy/${UNIT}" "${UNIT_DIR}/${UNIT}"

systemctl --user daemon-reload
systemctl --user enable "${UNIT}"

if [[ "${START}" == "1" ]]; then
  echo "==> starting"
  systemctl --user restart "${UNIT}"
  sleep 2
  systemctl --user --no-pager status "${UNIT}" || true
  echo
  echo "logs: journalctl --user -u ${UNIT} -f"
  echo "      tail -f ${REPO}/data/cse.log"
else
  echo "==> installed but not started (--no-start)"
fi

echo "==> done"
