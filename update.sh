#!/usr/bin/env bash
# ============================================================================
# update.sh —— 拉取最新代码 + 更新依赖 + 重启服务
# ============================================================================
# 用法：sudo bash update.sh
# 保留 config.json / .env / 状态文件，只更新代码和依赖

set -euo pipefail

INSTALL_DIR="/opt/erc20-monitor"
SERVICE_USER="erc20mon"
SERVICE_GROUP="erc20mon"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
NC='\033[0m'
log_info()  { echo -e "${GRN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

if [[ $EUID -ne 0 ]]; then
    log_error "请用 root 运行: sudo bash update.sh"
    exit 1
fi

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    log_error "$INSTALL_DIR 不是 git 仓库，请先运行 deploy.sh"
    exit 1
fi

# 1. 停止服务
log_info "停止服务..."
systemctl stop erc20-monitor || true

# 2. 保存用户数据（保险起见，虽然 git pull 不会覆盖未跟踪文件）
BACKUP_FILES=("config.json" "monitor_state.json" "alerts.log" "exchanges.json")
for f in "${BACKUP_FILES[@]}"; do
    if [[ -f "$INSTALL_DIR/$f" ]]; then
        log_info "保留用户文件: $f"
    fi
done

# 3. 拉取代码
log_info "拉取最新代码..."
git -C "$INSTALL_DIR" fetch origin main
git -C "$INSTALL_DIR" reset --hard origin/main

# 4. 更新依赖
log_info "更新 Python 依赖..."
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# 5. 更新 systemd 单元文件（如有变更）
cp "$INSTALL_DIR/erc20-monitor.service" /etc/systemd/system/erc20-monitor.service
systemctl daemon-reload

# 6. 修正权限
chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/venv/bin/"* 2>/dev/null || true

# 7. 启动服务
log_info "启动服务..."
systemctl start erc20-monitor
sleep 2

if systemctl is-active --quiet erc20-monitor; then
    log_info "更新完成，服务已运行 ✅"
else
    log_error "服务启动失败，查看日志: journalctl -u erc20-monitor -n 50"
    exit 1
fi
