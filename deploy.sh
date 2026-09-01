#!/usr/bin/env bash
# ============================================================================
# deploy.sh —— ERC20 大额转账监控器一键部署脚本
# ============================================================================
# 用法：
#   sudo bash deploy.sh
#
# 脚本会自动完成：
#   1. 检查 root 权限 + 安装 python3 / git（Debian/Ubuntu/CentOS 兼容）
#   2. 创建专用只读用户 erc20mon（无登录 shell，最小权限）
#   3. 克隆仓库到 /opt/erc20-monitor 并 checkout main
#   4. 创建 venv + pip install 依赖（仅 requests / python-dotenv）
#   5. 复制 config.example.json → config.json（需手动编辑代币配置）
#   6. 生成 /etc/systemd/system/erc20-monitor.env（需手动填敏感配置）
#   7. 安装 systemd 单元文件 + daemon-reload + enable --now
#   8. 打印后续需要手动填写的配置项
#
# 前置：服务器能出网（pip install + RPC 节点 + CoinGecko + 飞书）
# 后置：手动编辑 config.json 和 erc20-monitor.env，然后 systemctl restart

set -euo pipefail

# ---------- 颜色输出 ----------
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
NC='\033[0m'
log_info()  { echo -e "${GRN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------- 可配置项 ----------
REPO_URL="https://github.com/tangguanlin78-create/cybcoin.git"
REPO_BRANCH="main"
INSTALL_DIR="/opt/erc20-monitor"
SERVICE_USER="erc20mon"
SERVICE_GROUP="erc20mon"
ENV_FILE="/etc/systemd/system/erc20-monitor.env"
SERVICE_FILE="/etc/systemd/system/erc20-monitor.service"

# ---------- 0. 权限检查 ----------
if [[ $EUID -ne 0 ]]; then
    log_error "请用 root 运行: sudo bash deploy.sh"
    exit 1
fi
log_info "=== ERC20 监控器部署 ==="
log_info "仓库: $REPO_URL ($REPO_BRANCH)"
log_info "安装: $INSTALL_DIR"

# ---------- 1. 安装系统依赖 ----------
install_pkg() {
    local pkgs=("$@")
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq "${pkgs[@]}"
    elif command -v dnf &>/dev/null; then
        dnf install -y "${pkgs[@]}"
    elif command -v yum &>/dev/null; then
        yum install -y "${pkgs[@]}"
    elif command -v pacman &>/dev/null; then
        pacman -Sy --noconfirm "${pkgs[@]}"
    else
        log_error "不支持的包管理器，请手动安装 python3 python3-venv git"
        exit 1
    fi
}

NEED_INSTALL=()
command -v python3 &>/dev/null  || NEED_INSTALL+=(python3 python3-venv python3-pip)
command -v git      &>/dev/null  || NEED_INSTALL+=(git)
command -v systemctl &>/dev/null || { log_error "需要 systemd"; exit 1; }

if [[ ${#NEED_INSTALL[@]} -gt 0 ]]; then
    log_info "安装系统依赖: ${NEED_INSTALL[*]}"
    install_pkg "${NEED_INSTALL[@]}"
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log_info "Python 版本: $PYVER"

# ---------- 2. 创建专用用户 ----------
if ! id "$SERVICE_USER" &>/dev/null; then
    log_info "创建用户 $SERVICE_USER"
    useradd -r -s /usr/sbin/nologin -d "$INSTALL_DIR" -M "$SERVICE_USER"
else
    log_info "用户 $SERVICE_USER 已存在"
fi

# ---------- 3. 克隆仓库 ----------
if [[ -d "$INSTALL_DIR/.git" ]]; then
    log_info "拉取最新代码..."
    git -C "$INSTALL_DIR" fetch origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" reset --hard "origin/$REPO_BRANCH"
else
    log_info "克隆仓库到 $INSTALL_DIR"
    rm -rf "$INSTALL_DIR"
    git clone -b "$REPO_BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

# ---------- 4. 创建 venv + pip install ----------
log_info "安装 Python 依赖..."
rm -rf "$INSTALL_DIR/venv"
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ---------- 5. 准备 config.json ----------
if [[ ! -f "$INSTALL_DIR/config.json" ]]; then
    cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
    log_info "已生成 config.json（代币合约 / 阈值等非敏感配置）"
else
    log_info "config.json 已存在，保留用户配置"
fi

# ---------- 6. 生成 EnvironmentFile ----------
if [[ ! -f "$ENV_FILE" ]]; then
    cat > "$ENV_FILE" <<'ENVEOF'
# 敏感配置 —— 格式 KEY=VALUE，无空格无引号
RPC_URL=
FEISHU_WEBHOOK_URL=
COINGECKO_API_KEY=
ENVEOF
    chmod 600 "$ENV_FILE"
    chown root:"$SERVICE_GROUP" "$ENV_FILE"
    log_info "已生成 $ENV_FILE（敏感配置，需手动填写）"
else
    log_info "$ENV_FILE 已存在，保留用户配置"
fi

# ---------- 7. 安装 systemd 单元文件 ----------
cp "$INSTALL_DIR/erc20-monitor.service" "$SERVICE_FILE"
systemctl daemon-reload
log_info "已安装 systemd 单元文件"

# ---------- 8. 修正目录权限 ----------
chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"
# venv 内二进制需要可执行
chmod +x "$INSTALL_DIR/venv/bin/"* 2>/dev/null || true

# ---------- 9. 启动服务 ----------
log_info "启用并启动 erc20-monitor..."
systemctl enable --now erc20-monitor
sleep 2
if systemctl is-active --quiet erc20-monitor; then
    log_info "服务已启动 ✅"
else
    log_error "服务启动失败，查看日志: journalctl -u erc20-monitor -n 50"
fi

# ---------- 10. 打印后续步骤 ----------
echo
echo "============================================================"
echo -e "${YLW}部署完成！但还需要手动填写两项配置：${NC}"
echo
echo -e "  1) ${RED}编辑代币配置${NC}:  $INSTALL_DIR/config.json"
echo "     - token_contract  (代币合约地址)"
echo "     - token_decimals  (精度，0 则自动获取)"
echo "     - coingecko_id    (CoinGecko 对应 coin_id)"
echo "     - alert_usd_threshold  (告警 USD 阈值)"
echo "     - chain           (ethereum / bsc / polygon ...)"
echo
echo -e "  2) ${RED}编辑敏感配置${NC}:  $ENV_FILE"
echo "     - RPC_URL            (必须，只读 RPC 节点)"
echo "     - FEISHU_WEBHOOK_URL (必须，飞书机器人)"
echo "     - COINGECKO_API_KEY  (可选)"
echo
echo "  3) 重启服务使配置生效:"
echo "     sudo systemctl restart erc20-monitor"
echo
echo "  常用命令:"
echo "     sudo systemctl status erc20-monitor    # 状态"
echo "     journalctl -u erc20-monitor -f        # 实时日志"
echo "     sudo systemctl restart erc20-monitor   # 重启"
echo "     sudo systemctl stop erc20-monitor     # 停止"
echo "============================================================"
