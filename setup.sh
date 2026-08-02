#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

prompt() {
  local label="$1"
  local default_value="$2"
  local value=""
  read -r -p "${label} [${default_value}]: " value
  printf '%s' "${value:-${default_value}}"
}

safe_env_value() {
  local value="$1"
  if [[ "${value}" == *"'"* || "${value}" == *$'\n'* ]]; then
    echo "Configuration values cannot contain a single quote or newline." >&2
    exit 1
  fi
  printf "'%s'" "${value}"
}

require_command python3
require_command node
require_command npm
require_command codex

echo "Installing the official @larksuite/cli dependency..."
npm install --prefix "${REPO_ROOT}"

default_workspace="$(cd -- "${REPO_ROOT}/.." && pwd)"
workspace="$(prompt 'Workspace root monitored by Codex' "${default_workspace}")"
workspace="$(cd -- "${workspace}" && pwd)"
state_home="$(prompt 'Private notifier state directory' "${HOME}/.local/share/codex-feishu-notifier")"
codex_home="${CODEX_HOME:-${HOME}/.codex}"
brand="$(prompt 'Open platform brand (feishu or lark)' 'feishu')"

if [[ "${brand}" != "feishu" && "${brand}" != "lark" ]]; then
  echo "Brand must be feishu or lark." >&2
  exit 1
fi

read -r -p 'Feishu/Lark App ID: ' app_id
read -r -s -p 'Feishu/Lark App Secret: ' app_secret
echo

if [[ -z "${app_id}" || -z "${app_secret}" ]]; then
  echo "App ID and App Secret are required." >&2
  exit 1
fi

mkdir -p "${state_home}"
chmod 700 "${state_home}"
config_path="${state_home}/config.env"

export CODEX_TASK_NOTIFY_HOME="${state_home}"
printf '%s\n' "${app_secret}" | "${REPO_ROOT}/bin/lark-cli" config init \
  --app-id "${app_id}" --app-secret-stdin --brand "${brand}"

read -r -p 'Recipient user Open ID (ou_..., leave blank to authorize yourself): ' recipient
if [[ -z "${recipient}" ]]; then
  echo "Opening Feishu/Lark user authorization to discover your app-specific Open ID..."
  "${REPO_ROOT}/bin/lark-cli" auth login --scope offline_access --json
  recipient="$("${REPO_ROOT}/bin/lark-cli" auth status --json --verify | python3 -c \
    'import json, sys; print(json.load(sys.stdin).get("identities", {}).get("user", {}).get("openId", ""))')"
fi
if [[ "${recipient}" != ou_* ]]; then
  echo "Could not determine a recipient Open ID. See docs/feishu-app.md." >&2
  exit 1
fi

{
  printf 'ENABLED=true\n'
  printf 'LARK_USER_OPEN_ID=%s\n' "$(safe_env_value "${recipient}")"
  printf 'LARK_CLI=%s\n' "$(safe_env_value "${REPO_ROOT}/bin/lark-cli")"
  printf 'CODEX_TASK_NOTIFY_HOME=%s\n' "$(safe_env_value "${state_home}")"
  printf 'CODEX_TASK_WORKSPACE=%s\n' "$(safe_env_value "${workspace}")"
  printf 'CODEX_TASK_SESSIONS_HOME=%s\n' "$(safe_env_value "${codex_home}/sessions")"
  printf 'CODEX_TASK_GOALS_DB=%s\n' "$(safe_env_value "${codex_home}/goals_1.sqlite")"
  printf 'URGENT_ON_STARTED=true\n'
  printf 'URGENT_ON_COMPLETED=true\n'
  printf 'URGENT_ON_STOPPED=true\n'
  printf 'URGENT_ON_RECOVERY=true\n'
  printf 'PROBE_429_ENABLED=true\n'
  printf 'PROBE_429_INTERVAL_SECONDS=300\n'
  printf 'PROBE_429_MAX_HOURS=24\n'
  printf 'PROBE_429_TIMEOUT_SECONDS=20\n'
  printf "PROBE_429_USER_AGENT='codex_cli_rs/0.146.0'\n"
  printf 'RECALL_INACTIVE_CARDS=true\n'
  printf 'RECALL_AFTER_INACTIVE_SECONDS=7200\n'
  printf 'RECALL_MAX_MESSAGE_AGE_SECONDS=84600\n'
  printf 'CARD_CLEANUP_SWEEP_SECONDS=300\n'
} >"${config_path}"
chmod 600 "${config_path}"

set -a
# shellcheck source=/dev/null
source "${config_path}"
set +a
python3 "${REPO_ROOT}/notifier.py" doctor

echo
echo "Setup complete. Start Codex with:"
echo "  ${REPO_ROOT}/bin/codex-feishu"
echo
read -r -p 'Send a test card now? [Y/n]: ' send_test
if [[ ! "${send_test}" =~ ^[Nn]$ ]]; then
  python3 "${REPO_ROOT}/notifier.py" test
fi
