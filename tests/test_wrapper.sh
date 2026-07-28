#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
test_home="$(mktemp -d)"
trap 'rm -rf -- "${test_home}"' EXIT

mkdir -p "${test_home}/state" "${test_home}/workspace"
fake_codex="${test_home}/fake-codex"
args_file="${test_home}/args"

cat >"${fake_codex}" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"${FAKE_CODEX_ARGS}"
exit 0
EOF
chmod +x "${fake_codex}"

cat >"${test_home}/state/config.env" <<EOF
ENABLED=false
LARK_USER_OPEN_ID='ou_test_user'
LARK_CLI='${REPO_ROOT}/bin/lark-cli'
CODEX_TASK_NOTIFY_HOME='${test_home}/state'
CODEX_TASK_WORKSPACE='${test_home}/workspace'
CODEX_TASK_SESSIONS_HOME='${test_home}/sessions'
CODEX_TASK_GOALS_DB='${test_home}/goals_1.sqlite'
EOF

CODEX_TASK_NOTIFY_HOME="${test_home}/state" \
CODEX_BIN="${fake_codex}" \
FAKE_CODEX_ARGS="${args_file}" \
"${REPO_ROOT}/bin/codex-feishu" -C "${test_home}/workspace" --version

grep -F 'notify=["python3"' "${args_file}" >/dev/null
grep -F "${REPO_ROOT}/notifier.py" "${args_file}" >/dev/null
grep -Fx -- '-C' "${args_file}" >/dev/null
grep -Fx -- "${test_home}/workspace" "${args_file}" >/dev/null

echo "wrapper integration test passed"
