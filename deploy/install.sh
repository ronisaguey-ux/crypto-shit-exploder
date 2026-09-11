#!/usr/bin/env bash
# Install crypto-shit-exploder as a user service.
#
#   bash deploy/install.sh            # install and start
#   bash deploy/install.sh --no-start # install without starting
#
# Idempotent: re-running upgrades the venv and restarts the unit. A failed venv
# creation is removed rather than left behind, so the second run starts clean
# instead of failing on a half-made interpreter.
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

# `python3 -m venv` needs the system venv/ensurepip package, which is absent on
# some hosts (this one included). uv carries its own interpreter and is already
# present, so prefer it and fall back to the stdlib with a clear error.
create_venv() {
  local target="$1"
  if command -v uv >/dev/null 2>&1; then
    echo "==> creating venv with uv"
    if uv venv "${target}"; then
      return 0
    fi
    echo "!! uv venv failed; falling back to python3 -m venv" >&2
  fi
  echo "==> creating venv with python3 -m venv"
  if ! python3 -m venv "${target}"; then
    echo "!! python3 -m venv failed." >&2
    echo "!! Install python3-venv (Debian/Ubuntu) or uv, then re-run." >&2
    return 1
  fi
}

if [[ ! -x "${REPO}/.venv/bin/python" ]]; then
  # Either no venv, or a broken one from a previous failed run: start over.
  rm -rf "${REPO}/.venv"
  if ! create_venv "${REPO}/.venv"; then
    rm -rf "${REPO}/.venv"
    exit 1
  fi
fi

echo "==> installing dependencies"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "${REPO}/.venv/bin/python" --quiet --upgrade pip
  uv pip install --python "${REPO}/.venv/bin/python" --quiet -r "${REPO}/requirements.txt"
else
  "${REPO}/.venv/bin/pip" install --quiet --upgrade pip
  "${REPO}/.venv/bin/pip" install --quiet -r "${REPO}/requirements.txt"
fi

if [[ ! -f "${REPO}/.env" && -f "${REPO}/.env.example" ]]; then
  echo "==> no .env found; copying .env.example (the run works without keys)"
  cp "${REPO}/.env.example" "${REPO}/.env"
  chmod 600 "${REPO}/.env"
fi

mkdir -p "${REPO}/data" "${REPO}/logs/traders" "${UNIT_DIR}"
echo "==> installing ${UNIT}"
install -m 644 "${REPO}/deploy/${UNIT}" "${UNIT_DIR}/${UNIT}"

# systemd is best-effort: a sandboxed HOME (or a container without a user
# manager) cannot see the unit, and that must not fail the install.
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable "${UNIT}" 2>/dev/null || echo "!! could not enable ${UNIT} (no user systemd?) — run it manually"
fi

if [[ "${START}" == "1" ]]; then
  echo "==> starting"
  systemctl --user restart "${UNIT}" 2>/dev/null || echo "!! could not start ${UNIT}; start it manually"
  sleep 2
  systemctl --user --no-pager status "${UNIT}" 2>/dev/null || true
  echo
  echo "logs: journalctl --user -u ${UNIT} -f"
  echo "      tail -f ${REPO}/data/cse.log"
else
  echo "==> installed but not started (--no-start)"
fi

echo "==> done"
