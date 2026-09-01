#!/usr/bin/env bash
# ============================================================================
# uninstall.sh —— 停止 + 禁用 + 清理所有组件
# ============================================================================
# 用法：sudo bash uninstall.sh
# 默认保留 /opt/erc20-monitor 目录（含用户配置和状态），加 --purge 彻底删除

set -euo pipefail

INSTALL_DIR="/opt/erc20-monitor"
SERVICE_USER="erc20mon"
SERVICE_GROUP="erc20mon"
ENV_FILE="/etc/systemd/system/erc20-monitor.env"
SERVICE_FILE="/etc/systemd/system/erc20-monitor.service"
PURGE=false
for arg in "$@"; do [[ "$arg" == "--purge" ]] && PURGE=true; done

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
NC='\033[0m'
log_info()  { echo -e "${GRN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

if [[ $EUID -ne 0 ]]; then
    log_error "请用 root 运行: sudo bash uninstall.sh [--purge]"
    exit 1
fi

# 1. 停止 + 禁用服务
log_info "停止并禁用服务..."
systemctl stop erc20-monitor 2>/dev/null || true
systemctl disable erc20-monitor 2>/dev/null || true

# 2. 删除 systemd 文件
rm -f "$SERVICE_FILE"
rm -f "$ENV_FILE"
systemctl daemon-reload
log_info "已删除 systemd 单元文件"

# 3. 删除用户
if id "$SERVICE_USER" &>/dev/null; then
    userdel -r "$SERVICE_USER" 2>/dev/null || true
    log_info "已删除用户 $SERVICE_USER"
fi

# 4. 删除安装目录
if [[ "$PURGE" == true ]]; then
    log_warn "--purge: 彻底删除 $INSTALL_DIR（含 config.json / 日志 / 状态）"
    rm -rf "$INSTALL_DIR"
    log_info "已删除 $INSTALL_DIR"
else
    log_info "保留 $INSTALL_DIR（加 --purge 可彻底删除）"
    log_info "  用户配置仍在: $INSTALL_DIR/config.json"
    log_info "  状态/日志仍在: monitor_state.json / alerts.log"
fi

log_info "卸载完成"
