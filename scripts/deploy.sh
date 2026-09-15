#!/usr/bin/env bash
set -Eeuo pipefail

# Install/update Material Profile Worker as a system-level systemd service.
# Run from the repository on Ubuntu: sudo bash scripts/deploy.sh

SERVICE_NAME="preset-sync-worker"
SERVICE_USER="${SERVICE_NAME}"
INSTALL_ROOT="/opt"
CONFIG_ROOT="/etc"
INSTALL_DIR="${INSTALL_ROOT}/${SERVICE_NAME}"
CONFIG_DIR="${CONFIG_ROOT}/${SERVICE_NAME}"
ENV_FILE="${CONFIG_DIR}/worker.env"
UNIT_DIR="/etc/systemd/system"
UNIT_FILE="${UNIT_DIR}/${SERVICE_NAME}.service"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

[[ "$(id -u)" -eq 0 ]] || die '请使用 sudo 执行: sudo bash scripts/deploy.sh'
[[ -f "${SOURCE_DIR}/pyproject.toml" ]] || die '找不到项目 pyproject.toml，请从仓库内运行脚本'
[[ -f "${SOURCE_DIR}/.env.example" ]] || die '找不到 .env.example'

for command in python3 systemctl install rsync; do
    command -v "$command" >/dev/null 2>&1 || die "缺少命令: ${command}"
done

PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' \
    || die "项目要求 Python 3.11 或更高版本，当前为 ${PYTHON_VERSION}"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 "${INSTALL_DIR}" "${CONFIG_DIR}"

# Update application files without touching the separate credentials directory.
rsync -a --delete \
    --exclude '/.git/' \
    --exclude '/.venv/' \
    --exclude '/.pytest_cache/' \
    --exclude '/__pycache__/' \
    --exclude '/.env' \
    --exclude '/.env.*' \
    "${SOURCE_DIR}/" "${INSTALL_DIR}/"

if [[ ! -e "${ENV_FILE}" ]]; then
    install -o root -g "${SERVICE_USER}" -m 0640 "${SOURCE_DIR}/.env.example" "${ENV_FILE}"
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

chown -R root:root "${INSTALL_DIR}"
chmod 0755 "${INSTALL_DIR}"
chmod 0640 "${ENV_FILE}"
chown root:"${SERVICE_USER}" "${ENV_FILE}"

cat >"${UNIT_FILE}" <<EOF
[Unit]
Description=${SERVICE_NAME} (Feishu Bitable to GitHub/Gitea)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
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
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

printf '\n部署完成。服务状态:\n'
systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
printf '\n查看日志: sudo journalctl -u %s -f\n' "${SERVICE_NAME}"
