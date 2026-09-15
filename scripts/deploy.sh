#!/usr/bin/env bash
set -Eeuo pipefail

# Install/update as a per-user systemd service; no sudo required.
# Run from the repository on Ubuntu: bash scripts/deploy.sh

SERVICE_NAME="preset-sync-worker"
USER_DATA_DIR="${XDG_DATA_HOME:-${HOME:?HOME is not set}/.local/share}"
USER_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME:?HOME is not set}/.config}"
INSTALL_DIR="${USER_DATA_DIR}/${SERVICE_NAME}"
CONFIG_DIR="${USER_CONFIG_DIR}/${SERVICE_NAME}"
ENV_FILE="${CONFIG_DIR}/worker.env"
UNIT_DIR="${USER_CONFIG_DIR}/systemd/user"
UNIT_FILE="${UNIT_DIR}/${SERVICE_NAME}.service"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

[[ -f "${SOURCE_DIR}/pyproject.toml" ]] || die '找不到项目 pyproject.toml，请从仓库内运行脚本'
[[ -f "${SOURCE_DIR}/.env.example" ]] || die '找不到 .env.example'

for command in python3 systemctl install rsync; do
    command -v "$command" >/dev/null 2>&1 || die "缺少命令: ${command}"
done

PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
    || die "项目要求 Python 3.11 或更高版本，当前为 ${PYTHON_VERSION}"

install -d -m 0755 "${INSTALL_DIR}" "${CONFIG_DIR}" "${UNIT_DIR}"

# Sync application files only; credentials remain in CONFIG_DIR and survive updates.
rsync -a --delete \
    --exclude '/.git/' \
    --exclude '/.venv/' \
    --exclude '/.pytest_cache/' \
    --exclude '/__pycache__/' \
    --exclude '/.env' \
    --exclude '/.env.*' \
    "${SOURCE_DIR}/" "${INSTALL_DIR}/"

if [[ ! -e "${ENV_FILE}" ]]; then
    install -m 0600 "${SOURCE_DIR}/.env.example" "${ENV_FILE}"
    printf '已创建配置模板: %s\n请先填写凭证，再重新运行此脚本完成启动。\n' "${ENV_FILE}"
    exit 0
fi

for key in FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_APP_TOKEN FEISHU_TABLE_ID GIT_REPOSITORY_URL GIT_ACCESS_TOKEN; do
    value="$(sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -n 1)"
    [[ -n "${value}" ]] || die "${ENV_FILE} 中 ${key} 尚未配置"
done

python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/.venv/bin/python" -m pip install "${INSTALL_DIR}"

cat >"${UNIT_FILE}" <<EOF
[Unit]
Description=${SERVICE_NAME} (Feishu Bitable to GitHub/Gitea)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
ExecStart=${INSTALL_DIR}/.venv/bin/material-worker
Restart=on-failure
RestartSec=10s
TimeoutStopSec=30s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable "${SERVICE_NAME}.service"
systemctl --user restart "${SERVICE_NAME}.service"

printf '\n部署完成。服务状态:\n'
systemctl --user --no-pager --full status "${SERVICE_NAME}.service" || true
printf '\n查看日志: journalctl --user -u %s -f\n' "${SERVICE_NAME}"
printf '需要管理员开启未登录开机启动时，可执行: sudo loginctl enable-linger %s\n' "$(id -un)"
