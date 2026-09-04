#!/usr/bin/env bash
# ============================================================================
# vultr_deploy.sh —— 加密监控双服务一键部署（Vultr / 任意 Ubuntu/Debian 云主机）
# ============================================================================
# 部署两个服务：
#   1) erc20-monitor        七链 ERC20 大额转账告警（/opt/erc20-monitor）
#   2) smart-wallet-monitor 聪明钱包共买监控 Solana/EVM（/opt/smart-wallet）
#
# ----------------------------------------------------------------------------
# 【第一步：Windows 本地打包上传】（PowerShell 执行，把 SERVER_IP 换成你的服务器）
#
#   # 1. 在服务器上准备 staging 目录（先 ssh 上去执行一次）：
#   ssh root@SERVER_IP "mkdir -p /root/crypto-staging/erc20 /root/crypto-staging/smart-wallet"
#
#   # 2. 上传七链告警程序（F:\Trac_crypto 的文件，不含 venv/日志/缓存）
#   scp F:\Trac_crypto\*.py F:\Trac_crypto\requirements.txt F:\Trac_crypto\config_v2.json `
#       F:\Trac_crypto\exchanges.json F:\Trac_crypto\custom_labels.json `
#       F:\Trac_crypto\cex_labels.json F:\Trac_crypto\excluded_addresses.json `
#       F:\Trac_crypto\binance_perp_tokens.json F:\Trac_crypto\erc20-monitor.service `
#       F:\Trac_crypto\.env F:\Trac_crypto\monitor_state_v2.json `
#       root@SERVER_IP:/root/crypto-staging/erc20/
#
#   # 3. 上传聪明钱包监控（整个 Smart_Wallet 目录，内含 monitor/ 和 Wallet/）
#   scp -r F:\Trac_all_coin\Smart_Wallet\monitor F:\Trac_all_coin\Smart_Wallet\Wallet `
#       root@SERVER_IP:/root/crypto-staging/smart-wallet/
#
#   # 4. 上传本部署脚本并执行
#   scp F:\Trac_crypto\vultr_deploy.sh root@SERVER_IP:/root/
#   ssh root@SERVER_IP "bash /root/vultr_deploy.sh"
#
# 【可选】staging 目录默认 /root/crypto-staging，也可传参自定义：
#   bash vultr_deploy.sh /path/to/staging
#
# 【重复执行安全】：脚本幂等，可反复运行；会保留 /opt 下已有的 .env / config.yaml
#   （staging 中同名文件会覆盖，注意不要把空模板传上去）。
# ============================================================================

set -euo pipefail

# Windows 编辑过的脚本可能带 CRLF，自动转换后重跑
if grep -q $'\r' "$0" 2>/dev/null; then
    sed -i 's/\r$//' "$0"
    exec bash "$0" "$@"
fi

# ---------- 颜色输出 ----------
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
log_info()  { echo -e "${GRN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YLW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---------- 路径与变量 ----------
STAGING="${1:-/root/crypto-staging}"
STAGE_ERC20="$STAGING/erc20"
STAGE_SW="$STAGING/smart-wallet"

ERC20_DIR="/opt/erc20-monitor"
SW_DIR="/opt/smart-wallet"
ERC20_USER="erc20mon"
SW_USER="swmon"

# ---------- 0. root 权限检查 ----------
if [[ $EUID -ne 0 ]]; then
    log_error "请用 root 运行: sudo bash vultr_deploy.sh"
    exit 1
fi

echo
log_info "=== 加密监控双服务部署 ==="
log_info "staging 目录: $STAGING"
echo

# ---------- 1. 系统依赖 ----------
log_info "[1/9] 检查系统依赖..."
if ! command -v systemctl &>/dev/null; then
    log_error "需要 systemd（请使用 Ubuntu 22.04/24.04 等正规发行版）"
    exit 1
fi
NEED=()
command -v python3 &>/dev/null || NEED+=(python3 python3-venv python3-pip)
# 注意：Ubuntu 24.04 下 `python3 -m venv --help` 即使缺 python3.12-venv 也能通过，
# 必须用 ensurepip 探测（缺失时 venv 创建会失败）
python3 -c "import ensurepip" &>/dev/null 2>&1 || NEED+=(python3-venv python3.12-venv)
command -v rsync    &>/dev/null || NEED+=(rsync)
command -v curl     &>/dev/null || NEED+=(curl)
if [[ ${#NEED[@]} -gt 0 ]]; then
    log_info "安装: ${NEED[*]}"
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y -qq "${NEED[@]}"
    elif command -v dnf &>/dev/null; then
        dnf install -y "${NEED[@]}"
    elif command -v yum &>/dev/null; then
        yum install -y "${NEED[@]}"
    else
        log_error "不支持的包管理器，请手动安装 python3/venv/rsync/curl"
        exit 1
    fi
fi
log_info "Python: $(python3 --version 2>&1)"

# ---------- 2. 时区 ----------
log_info "[2/9] 设置时区 Asia/Shanghai（保证推送时间为北京时间）..."
timedatectl set-timezone Asia/Shanghai 2>/dev/null || true

# ---------- 3. 币安 API 地区检查 ----------
log_info "[3/9] 检查币安合约 API 可达性（更新币种列表用）..."
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    "https://fapi.binance.com/fapi/v1/ping" || echo "000")
if [[ "$HTTP_CODE" == "451" || "$HTTP_CODE" == "403" ]]; then
    log_warn "币安 fapi 对本机 IP 返回 $HTTP_CODE（地区限制）。"
    log_warn "  -> 监控运行不受影响（只用 RPC/CoinGecko/飞书）；"
    log_warn "  -> 但不要在本机跑 fetch_binance_perp_tokens.py，请在本地 Windows 生成 config 后上传。"
elif [[ "$HTTP_CODE" == "200" ]]; then
    log_info "币安 fapi 可达 ✅（可在服务器上直接更新币种列表）"
else
    log_warn "币安 fapi 探测返回 HTTP=$HTTP_CODE（网络问题？），不影响部署。"
fi

# ---------- 4. 创建专用用户 ----------
log_info "[4/9] 创建服务专用用户..."
id "$ERC20_USER" &>/dev/null || useradd -r -s /usr/sbin/nologin -d "$ERC20_DIR" -M "$ERC20_USER"
id "$SW_USER"     &>/dev/null || useradd -r -s /usr/sbin/nologin -d "$SW_DIR" -M "$SW_USER"

# ---------- 5. 同步程序文件 ----------
log_info "[5/9] 同步程序文件到 /opt ..."

if [[ ! -d "$STAGE_ERC20" ]]; then
    log_error "找不到 $STAGE_ERC20，请先按脚本注释说明从 Windows 上传文件。"
    exit 1
fi
mkdir -p "$ERC20_DIR"
rsync -a --delete \
    --exclude 'venv/' --exclude '__pycache__/' --exclude '.git/' \
    --exclude '*.log' --exclude '*.bak' --exclude '*.pyc' \
    --exclude 'config.json.json' \
    "$STAGE_ERC20/" "$ERC20_DIR/"

if [[ -d "$STAGE_SW/monitor" ]]; then
    mkdir -p "$SW_DIR"
    rsync -a --delete \
        --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.log' \
        "$STAGE_SW/" "$SW_DIR/"
    SW_FOUND=1
else
    log_warn "找不到 $STAGE_SW/monitor，跳过聪明钱包监控部署。"
    SW_FOUND=0
fi

# ---------- 6. venv + 依赖 ----------
log_info "[6/9] 创建虚拟环境并安装依赖..."
python3 -m venv "$ERC20_DIR/venv"
"$ERC20_DIR/venv/bin/pip" install --upgrade pip -q
"$ERC20_DIR/venv/bin/pip" install -r "$ERC20_DIR/requirements.txt" -q

if [[ "$SW_FOUND" == "1" ]]; then
    python3 -m venv "$SW_DIR/venv"
    "$SW_DIR/venv/bin/pip" install --upgrade pip -q
    "$SW_DIR/venv/bin/pip" install -r "$SW_DIR/monitor/requirements.txt" -q
fi

# ---------- 7. 配置检查 ----------
log_info "[7/9] 检查敏感配置..."

START_ERC20=1
if [[ ! -f "$ERC20_DIR/.env" ]]; then
    log_warn "$ERC20_DIR/.env 不存在（RPC_URL_*/FEISHU_WEBHOOK_URL 等密钥）"
    if [[ -f "$ERC20_DIR/erc20-monitor.env" ]]; then
        cp "$ERC20_DIR/erc20-monitor.env" "$ERC20_DIR/.env"
        log_warn "已从模板生成 .env，请填写后重启: sudo systemctl restart erc20-monitor"
    fi
    START_ERC20=0
else
    # 简单校验：至少有一条非空 RPC_URL_
    if ! grep -Eq '^RPC_URL_[A-Z]+=.+' "$ERC20_DIR/.env"; then
        log_warn ".env 中未发现已填写的 RPC_URL_<链名>=...，服务将启动失败。"
        START_ERC20=0
    fi
    if ! grep -Eq '^FEISHU_WEBHOOK_URL=.+' "$ERC20_DIR/.env"; then
        log_warn ".env 中未填写 FEISHU_WEBHOOK_URL，告警无法推送。"
    fi
fi

START_SW=0
if [[ "$SW_FOUND" == "1" ]]; then
    if [[ -f "$SW_DIR/monitor/config.yaml" ]]; then
        START_SW=1
    else
        log_warn "$SW_DIR/monitor/config.yaml 不存在（Helius/Etherscan/飞书密钥）"
        [[ -f "$SW_DIR/monitor/config.example.yaml" ]] && \
            log_warn "可复制模板: cp $SW_DIR/monitor/config.example.yaml $SW_DIR/monitor/config.yaml 后填写"
    fi
fi

# ---------- 8. 权限 + systemd ----------
log_info "[8/9] 安装 systemd 单元并修正权限..."

cp "$ERC20_DIR/erc20-monitor.service" /etc/systemd/system/erc20-monitor.service
chown -R "$ERC20_USER":"$ERC20_USER" "$ERC20_DIR"
[[ -f "$ERC20_DIR/.env" ]] && chmod 600 "$ERC20_DIR/.env"
chmod +x "$ERC20_DIR/venv/bin/"* 2>/dev/null || true

if [[ "$SW_FOUND" == "1" ]]; then
    cp "$SW_DIR/monitor/smart-wallet-monitor.service" /etc/systemd/system/smart-wallet-monitor.service
    chown -R "$SW_USER":"$SW_USER" "$SW_DIR"
    [[ -f "$SW_DIR/monitor/config.yaml" ]] && chmod 600 "$SW_DIR/monitor/config.yaml"
    chmod +x "$SW_DIR/venv/bin/"* 2>/dev/null || true
fi

systemctl daemon-reload

# ---------- 8.5 sync_exchanges.py cron（每日 04:00 更新交易所标签）----------
log_info "[8.5/9] 安装 sync_exchanges.py 定时任务（每日 04:00）..."
if [[ -f "$ERC20_DIR/sync_exchanges.py" ]]; then
    CRON_LINE="0 4 * * * $ERC20_USER $ERC20_DIR/venv/bin/python $ERC20_DIR/sync_exchanges.py --sources $ERC20_DIR/sources.json --out $ERC20_DIR/exchanges.json >> $ERC20_DIR/sync_exchanges.log 2>&1"
    # 用 erc20mon 用户 crontab
    (crontab -u "$ERC20_USER" -l 2>/dev/null | grep -v 'sync_exchanges.py' ; echo "$CRON_LINE") | crontab -u "$ERC20_USER" -
    log_info "sync_exchanges cron 已安装到 $ERC20_USER 的 crontab ✅"
    # sources.json 可能不存在（用户未上传），提示
    [[ ! -f "$ERC20_DIR/sources.json" ]] && \
        log_warn "$ERC20_DIR/sources.json 不存在，请上传或从 sources.example.json 复制填写后：crontab -u $ERC20_USER -l 确认"
else
    log_warn "sync_exchanges.py 未上传，跳过 cron 安装"
fi

# ---------- 9. 启动与健康检查 ----------
log_info "[9/9] 启动服务..."

start_and_check() {
    local svc="$1"
    systemctl enable "$svc" &>/dev/null
    systemctl restart "$svc"
    sleep 5
    if systemctl is-active --quiet "$svc"; then
        log_info "$svc 已启动 ✅"
        return 0
    else
        log_error "$svc 启动失败 ❌  最近日志:"
        journalctl -u "$svc" -n 20 --no-pager | sed 's/^/    /'
        return 1
    fi
}

echo
if [[ "$START_ERC20" == "1" ]]; then
    start_and_check erc20-monitor || true
else
    log_warn "erc20-monitor 配置未就绪，已安装但未启动。填好 $ERC20_DIR/.env 后执行:"
    echo "    sudo systemctl enable --now erc20-monitor"
fi

if [[ "$SW_FOUND" == "1" ]]; then
    if [[ "$START_SW" == "1" ]]; then
        start_and_check smart-wallet-monitor || true
    else
        log_warn "smart-wallet-monitor 配置未就绪，已安装但未启动。填好 config.yaml 后执行:"
        echo "    sudo systemctl enable --now smart-wallet-monitor"
    fi
fi

# ---------- 完成提示 ----------
echo
echo "============================================================"
log_info "部署流程结束。常用命令："
cat <<'EOF'
  journalctl -u erc20-monitor -f           # 七链告警实时日志
  journalctl -u smart-wallet-monitor -f    # 聪明钱包监控实时日志
  sudo systemctl restart erc20-monitor     # 改配置/更新代码后重启
  sudo systemctl status erc20-monitor      # 状态

  【重要】确认服务器正常出块后，再停掉本地 Windows 的 pythonw 进程，
          避免双实例重复推送飞书。

  【日常更新币种】在本地 Windows 执行后上传 config_v2.json：
    python fetch_binance_perp_tokens.py
    python generate_multi_chain_config.py --top 0 --update-into config_v2.json --out config_v2.json
    scp config_v2.json root@SERVER_IP:/opt/erc20-monitor/
    ssh root@SERVER_IP "chown erc20mon:erc20mon /opt/erc20-monitor/config_v2.json && systemctl restart erc20-monitor"

  【交易所地址更新】已安装 cron，每天 04:00 自动运行 sync_exchanges.py：
    /opt/erc20-monitor/venv/bin/python /opt/erc20-monitor/sync_exchanges.py \
      --sources /opt/erc20-monitor/sources.json --out /opt/erc20-monitor/exchanges.json
    手动测试: sudo -u erc20mon /opt/erc20-monitor/venv/bin/python \
      /opt/erc20-monitor/sync_exchanges.py \
      --sources /opt/erc20-monitor/sources.json --out /opt/erc20-monitor/exchanges.json
EOF
echo "============================================================"
