#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
All Coin Alarm —— ERC20 代币转账监控告警程序（只读 / Read-Only On-chain Monitor）
================================================================================

功能概述：
    1. 监控指定 ERC20 代币合约的所有 Transfer(address,address,uint256) 事件
    2. 采用轮询区块方式（不使用 websocket），避免长连接网络抖动
    3. 已处理区块 / 已告警交易持久化到本地，不重复处理、不重复告警
    4. 转账金额折算 USD 超过阈值才告警；过滤灰尘小额交易
    5. 解析每笔转账的 from / to / 数量 / USD 价值，识别接收方是否交易所
    6. 触发告警后调用飞书 webhook 推送卡片消息，附带区块浏览器交易链接
    7. 异常捕获：RPC 限流、网络失败、价格获取失败等，自动重试 / 降级

安全声明：
    - 本程序严格只读，不访问任何私钥 / 助记词
    - 不签名、不广播任何交易
    - 仅依赖一个只读 RPC 节点 URL 即可工作

--------------------------------------------------------------------------------
需要申请 / 配置的 API Key 与资源（详见 config.example.json）
--------------------------------------------------------------------------------
1) RPC 节点 URL（必须）
   - 推荐 Alchemy   : https://www.alchemy.com/        注册后创建 App 拷贝 https URL
   - 或   Infura    : https://infura.io/             注册后拷贝 HTTPS endpoint
   - 或   Ankr      : https://www.ankr.com/          提供免费公共多链 RPC
   - 只读节点即可，无需任何账户私钥。把 URL 填入 .env 的 RPC_URL
     （或 config.json 的 rpc_url，但推荐用 .env）。

2) CoinGecko API（用于代币 USD 价格，可选 API key）
   - 免费版免 key，限流约 30 次/分钟（足够监控使用，本程序已做缓存）
   - 官网： https://www.coingecko.com/api/pricing
   - Demo / Pro key 申请后填入 .env 的 COINGECKO_API_KEY（可留空）
   - 必须手动获取代币的 coin_id（例如 USDT = tether），在 coingecko.com
     搜索代币后从 URL 最后一段取得，填入 config.json 的 coingecko_id。

3) 飞书自定义机器人 webhook（必须，用于推送告警）
   - 飞书群 -> 设置 -> 群机器人 -> 添加"自定义机器人"
   - 拷贝生成的 webhook URL，填入 .env 的 FEISHU_WEBHOOK_URL
     （或 config.json 的 feishu_webhook_url，但推荐用 .env）
   - 官方文档： https://www.feishu.cn/hc/zh-CN/articles/360049389073

4) 配置文件：
   - 复制 config.example.json 为 config.json，填入非敏感配置（代币地址、链类型、阈值等）
   - 复制 .env.example 为 .env，填入敏感配置（RPC_URL / FEISHU_WEBHOOK_URL / COINGECKO_API_KEY）
   - 优先级：环境变量 > .env 文件 > config.json
   - .env 不要提交到 Git（仓库已提供 .gitignore 模板）

--------------------------------------------------------------------------------
依赖库（见 requirements.txt）
--------------------------------------------------------------------------------
    web3>=6.0.0,<7.0.0
    requests>=2.28.0
    python-dotenv>=1.0.0

运行：
    pip install -r requirements.txt
    python all_coin_alarm.py

systemd 守护（Linux 服务器常驻）：
    sudo cp erc20-monitor.service /etc/systemd/system/
    sudo cp erc20-monitor.env   /etc/systemd/system/   # 填敏感配置
    sudo systemctl daemon-reload
    sudo systemctl enable --now erc20-monitor
    journalctl -u erc20-monitor -f      # 查看实时日志
================================================================================
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

# Transfer(address indexed from, address indexed to, uint256 value) 的 keccak256 主题
# 纯 JSON-RPC 实现，不依赖 web3 库。此值为 ERC20 标准事件签名的 keccak256，全局唯一。
TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# ERC20 decimals() 函数选择器：keccak256("decimals()") 的前 4 字节
DECIMALS_SELECTOR = "0x313ce567"

# 默认文件名常量（避免在多处硬编码默认值，确保将来改路径只需改一处）
DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_STATE_FILE = "monitor_state.json"
DEFAULT_LOG_FILE = "alerts.log"
DEFAULT_EXCHANGES_FILE = "exchanges.json"
DEFAULT_SOURCES_FILE = "sources.json"

# 各链区块浏览器交易页前缀，用于拼接可点击的 tx 链接
EXPLORER_TX_PREFIX = {
    "ethereum": "https://etherscan.io/tx/",
    "bsc": "https://bscscan.com/tx/",
    "polygon": "https://polygonscan.com/tx/",
    "arbitrum": "https://arbiscan.io/tx/",
    "optimism": "https://optimistic.etherscan.io/tx/",
    "base": "https://basescan.org/tx/",
    "avalanche": "https://snowtrace.io/tx/",
}

# 各链在 CoinGecko 的 platform id，用于 token contract 价格查询（备用方案）
COINGECKO_PLATFORM = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
    "optimism": "optimistic-ethereum",
    "base": "base",
    "avalanche": "avalanche",
}

# CoinGecko 价格端点（免费版）
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

# HTTP 重试相关
MAX_RETRIES = 5           # 单次外部调用最大重试次数
RETRY_BACKOFF_BASE = 2.0  # 指数退避基数（秒）


# ------------------------------------------------------------------
# 配置加载
# ------------------------------------------------------------------

# 敏感配置项 → 对应的环境变量名。优先从 .env / 环境变量读取，覆盖 config.json 中的值。
# 这样 secret 不进 JSON、不进 Git，systemd 也可通过 EnvironmentFile 注入。
SENSITIVE_ENV_MAP = {
    "rpc_url": "RPC_URL",
    "feishu_webhook_url": "FEISHU_WEBHOOK_URL",
    "feishu_webhook_secret": "FEISHU_WEBHOOK_SECRET",
    "coingecko_api_key": "COINGECKO_API_KEY",
}

# 路径类配置（state_file / log_file）也可通过环境变量覆盖，
# 主要用于 Docker 部署：把状态文件指向挂载卷，保证容器重启后不丢失。
PATH_ENV_MAP = {
    "state_file": "STATE_FILE",
    "log_file": "LOG_FILE",
}


def _parse_tokens(cfg: Dict[str, Any], rpc: Optional[EthRpcClient] = None) -> List[Dict[str, Any]]:
    """从 config 提取代币列表，兼容两种配置方式。

    方式 A（新，推荐）: "tokens": [{contract, decimals, symbol, coingecko_id, alert_usd_threshold?}, ...]
    方式 B（旧，单代币）: token_contract + token_decimals + token_symbol + coingecko_id

    每种代币返回标准化 dict：
        {contract, decimals, symbol, coingecko_id, alert_threshold, dust_threshold}
    """
    if cfg.get("tokens") and isinstance(cfg["tokens"], list):
        raw_tokens = cfg["tokens"]
    else:
        # 向后兼容：把单代币字段转为 tokens 列表
        if not cfg.get("token_contract"):
            raise ValueError("未配置 tokens[] 或 token_contract，请在 config.json 至少提供一种")
        raw_tokens = [{
            "contract": cfg["token_contract"],
            "decimals": cfg.get("token_decimals", 0),
            "symbol": cfg.get("token_symbol", "TOKEN"),
            "coingecko_id": cfg.get("coingecko_id", ""),
        }]

    tokens: List[Dict[str, Any]] = []
    global_alert = float(cfg.get("alert_usd_threshold", 100_000))
    global_dust = float(cfg.get("dust_usd_threshold", 100))

    for t in raw_tokens:
        contract = str(t.get("contract") or t.get("token_contract") or "").lower()
        if not contract.startswith("0x"):
            raise ValueError(f"代币合约地址无效: {t}")
        decimals = int(t.get("decimals", 0))
        # 若 decimals=0 且有 rpc，则自动获取；否则用 fallback=18
        if decimals == 0 and rpc is not None:
            decimals = get_token_decimals(rpc, contract, fallback=18)
        elif decimals == 0:
            decimals = 18  # 先占位，init 阶段再用 rpc 覆盖
        symbol = str(t.get("symbol") or "TOKEN")
        coin_id = str(t.get("coingecko_id") or t.get("coin_id") or "")
        alert_th = float(t.get("alert_usd_threshold") or t.get("alert_threshold") or global_alert)
        dust_th = float(t.get("dust_usd_threshold") or t.get("dust_threshold") or global_dust)
        skip_alert = bool(t.get("skip_alert", False))

        tokens.append({
            # 多链模式：tokens[] 中显式 chain 字段
            # 单链模式（旧 config.json 兼容）：缺省从顶层 chain 取
            "chain": str(t.get("chain") or cfg.get("chain", "ethereum")).lower(),
            "contract": contract,
            "decimals": decimals,
            "symbol": symbol,
            "coingecko_id": coin_id,
            "alert_threshold": alert_th,
            "dust_threshold": dust_th,
            "skip_alert": skip_alert,
        })

    if not tokens:
        raise ValueError("代币列表为空，请在 config.json 的 tokens[] 中至少配置一个代币")

    logging.info("解析代币配置: %d 个 [%s]", len(tokens),
                 ", ".join(f"{t['symbol']}@{t['contract'][:10]}..." for t in tokens))
    return tokens


def load_config(path: str) -> Dict[str, Any]:
    """读取 JSON 配置文件，并用 .env / 环境变量覆盖敏感字段。

    优先级：环境变量 > .env 文件 > config.json
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"配置文件 {path} 不存在。请复制 config.example.json 为 config.json 并填写。"
        )
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 1) 加载 .env（若存在），将其中变量注入 os.environ，但不覆盖已存在的环境变量
    load_dotenv()

    # 2) 环境变量覆盖 config.json 中对应字段
    for cfg_key, env_key in SENSITIVE_ENV_MAP.items():
        env_val = os.getenv(env_key)
        if env_val:  # 非空字符串才覆盖（空字符串视为未设置）
            cfg[cfg_key] = env_val.strip()
    # 路径类配置（state_file / log_file）同样支持环境变量覆盖
    for cfg_key, env_key in PATH_ENV_MAP.items():
        env_val = os.getenv(env_key)
        if env_val:
            cfg[cfg_key] = env_val.strip()

    # 3) 基本校验
    # 两种模式：
    #   多链模式：rpc_urls(dict) + chains(list) + tokens[].chain
    #   单链模式（旧兼容）：rpc_url(str) + chain(str) + tokens[]
    is_multi_chain = isinstance(cfg.get("rpc_urls"), dict) and bool(cfg.get("rpc_urls"))
    if is_multi_chain:
        # 多链模式校验
        if not cfg.get("chains") or not isinstance(cfg["chains"], list):
            raise ValueError(
                "多链模式（rpc_urls）下必须配置 chains 列表，"
                "如 [\"ethereum\",\"bsc\",\"polygon\"]"
            )
        cfg["chains"] = [c.lower() for c in cfg["chains"]]
        # 环境变量覆盖 rpc_urls：RPC_URL_<大写链名>
        for chain_name in list(cfg["rpc_urls"].keys()):
            env_key = f"RPC_URL_{chain_name.upper()}"
            env_val = os.getenv(env_key)
            if env_val:
                cfg["rpc_urls"][chain_name] = env_val.strip()
        # 校验启用的每条链都有 URL
        for chain_name in cfg["chains"]:
            url = cfg["rpc_urls"].get(chain_name, "")
            if not url:
                raise ValueError(
                    f"多链模式: 链 {chain_name} 的 RPC URL 未配置。"
                    f"请在 rpc_urls 中填写，或在 .env / 环境变量 "
                    f"RPC_URL_{chain_name.upper()} 中设置。"
                )
        # 代币配置必须有 tokens[]
        if not (isinstance(cfg.get("tokens"), list) and len(cfg["tokens"]) > 0):
            raise ValueError("多链模式下必须配置 tokens[] 数组")
    else:
        # 单链模式（旧逻辑兼容）
        required = ["rpc_url"]
        # 代币配置二选一：tokens[] 或 token_contract
        has_tokens_array = isinstance(cfg.get("tokens"), list) and len(cfg["tokens"]) > 0
        has_single = bool(cfg.get("token_contract"))
        if not has_tokens_array and not has_single:
            required.extend(["tokens (数组)" , "token_contract (单代币)"])
        for key in required:
            if isinstance(key, str) and not cfg.get(key):
                raise ValueError(
                    f"配置项 {key} 不能为空。请在 config.json 中填写，或在 .env / 环境变量 "
                    f"{SENSITIVE_ENV_MAP.get(key, key.upper())} 中设置。"
                )
    # 飞书 webhook 可选：未配置时跳过推送（方便先测试 RPC / 监控逻辑）
    return cfg


# ------------------------------------------------------------------
# 状态持久化：已处理区块 + 已告警交易哈希，避免重复处理 / 重复告警
# ------------------------------------------------------------------

def load_state(path: str) -> Dict[str, Any]:
    """加载本地状态。结构：
        {
          "last_processed_block": <int>,
          "alerted_txs": { "<tx_hash>": <iso timestamp>, ... }
        }
    """
    if not os.path.exists(path):
        return {"last_processed_block": 0, "alerted_txs": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("last_processed_block", 0)
        state.setdefault("alerted_txs", {})
        return state
    except (json.JSONDecodeError, OSError):
        # 状态文件损坏时从 0 开始，宁可漏告警也不能崩溃
        logging.exception("状态文件 %s 损坏，已重置", path)
        return {"last_processed_block": 0, "alerted_txs": {}}


def save_state(path: str, state: Dict[str, Any]) -> None:
    """原子写入状态文件，防止写入中途崩溃导致文件损坏。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------
# 多链状态持久化（按 chain 分桶，兼容 v1 单链格式迁移）
# ------------------------------------------------------------------

def load_chain_state(path: str, chain: str) -> Dict[str, Any]:
    """加载单条链的状态。多链模式下 state 文件结构：
        {"_version": 2, "chains": {<chain>: {last_processed_block, alerted_txs}}}

    兼容旧 v1 格式（顶层 last_processed_block）：若旧文件恰好属于该 chain，
    数据会被自然继承；若不属于该 chain（多链模式下其他链），返回空状态。

    Args:
        path: 状态文件路径
        chain: 链名（如 ethereum / bsc）
    """
    empty = {"last_processed_block": 0, "alerted_txs": {}}
    if not os.path.exists(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logging.exception("状态文件 %s 损坏，已重置", path)
        return empty
    # v2 格式：{chains: {<chain>: {...}}}
    if isinstance(data.get("chains"), dict):
        chain_state = data["chains"].get(chain)
        if not isinstance(chain_state, dict):
            return empty
        chain_state.setdefault("last_processed_block", 0)
        chain_state.setdefault("alerted_txs", {})
        return chain_state
    # v1 格式：顶层 last_processed_block + alerted_txs
    # 仅当本进程也只跑单链 ethereum 时，旧状态可继承；
    # 多链模式下旧 v1 状态大概率属于原 cfg["chain"]，由调用方按 chain 匹配
    if isinstance(data.get("last_processed_block"), int):
        data.setdefault("alerted_txs", {})
        return data
    return empty


def save_chain_state(path: str, chain: str, chain_state: Dict[str, Any]) -> None:
    """原子写入单条链状态到多链 state 文件。

    读取现有文件 → 替换该 chain 的状态 → 原子写回。其他链状态保持不变。
    """
    data: Dict[str, Any] = {"_version": 2, "chains": {}}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old.get("chains"), dict):
                data["chains"] = dict(old["chains"])
            # v1 格式不在此合并：多链模式下旧 v1 数据已被 load_chain_state
            # 作为单链状态读取，重写时按 v2 写入即可，避免污染其他链
        except (json.JSONDecodeError, OSError):
            pass
    data["chains"][chain] = chain_state
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ------------------------------------------------------------------
# 交易所地址标签库
# ------------------------------------------------------------------

def _parse_exchanges_payload(data: Any) -> Dict[str, str]:
    """从已解析的 JSON 结构提取 {小写地址: 标签}。
    兼容三种常见格式：
      1) {"addresses": {"0x...": "Binance 14", ...}}   # 本项目 exchanges.json 格式
      2) {"0x...": "Binance 14", ...}                  # 扁平 map（部分公开标签源）
      3) [{"address":"0x...", "label":"Binance"}, ...] # 列表格式（Etherscan 导出等）
    """
    result: Dict[str, str] = {}

    if isinstance(data, dict) and "addresses" in data and isinstance(data["addresses"], dict):
        addrs = data["addresses"]
        for k, v in addrs.items():
            if str(k).startswith("0x") and len(str(k)) == 42:
                result[str(k).lower()] = str(v)
    elif isinstance(data, dict):
        for k, v in data.items():
            if str(k).startswith("0x") and len(str(k)) == 42:
                result[str(k).lower()] = str(v)
    elif isinstance(data, list):
        # 列表格式：每项含 address / label 字段（label 也可能叫 name / tag / owner）
        for item in data:
            if not isinstance(item, dict):
                continue
            addr = item.get("address") or item.get("addr")
            label = item.get("label") or item.get("name") or item.get("tag") or item.get("owner")
            if addr and label and str(addr).startswith("0x") and len(str(addr)) == 42:
                result[str(addr).lower()] = str(label)

    return result


class ExchangeLabelStore:
    """交易所地址标签库，支持从外部 URL 定期拉取 + 本地兜底 + 内存缓存。

    设计：
      - 启动时先加载本地 exchanges_file 作为兜底，保证 URL 不可用时仍可识别
      - 周期性从 exchanges_url 拉取最新标签，成功则更新内存缓存与本地文件
      - 拉取失败 / 解析失败时继续使用上一次缓存，不影响主流程
      - 拉取使用带退避重试的 http_request_with_retry，容忍偶发网络抖动
    """

    def __init__(
        self,
        url: Optional[str],
        local_path: str,
        refresh_interval_seconds: int,
        cache_path: Optional[str] = None,
    ):
        self.url = url.strip() if url else None
        self.local_path = local_path
        self.refresh_interval = max(int(refresh_interval_seconds), 60)  # 最低 1 分钟
        # 拉取成功后落地的缓存文件，下次启动可优先用（若 URL 失败则回退 local_path）
        self.cache_path = cache_path or (local_path + ".cache")
        self._labels: Dict[str, str] = {}
        self._last_refresh: float = 0.0

    def _load_from_file(self, path: str) -> Dict[str, str]:
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _parse_exchanges_payload(json.load(f))
        except (json.JSONDecodeError, OSError):
            logging.exception("交易所标签文件 %s 解析失败", path)
            return {}

    def _save_to_file(self, path: str, labels: Dict[str, str]) -> None:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"addresses": labels}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            logging.exception("写入交易所标签缓存 %s 失败", path)

    def _fetch_remote(self) -> Optional[Dict[str, str]]:
        """从 URL 拉取并解析。成功返回 dict，失败返回 None。"""
        if not self.url:
            return None
        resp = http_request_with_retry("GET", self.url, timeout=20)
        if resp is None or resp.status_code != 200:
            logging.error(
                "交易所标签库远程拉取失败 status=%s url=%s",
                getattr(resp, "status_code", None), self.url,
            )
            return None
        try:
            labels = _parse_exchanges_payload(resp.json())
        except (ValueError, TypeError):
            logging.exception("交易所标签库远程响应解析失败 url=%s", self.url)
            return None
        logging.info("交易所标签库远程拉取成功: %d 个地址", len(labels))
        return labels

    def _do_refresh(self) -> None:
        """执行一次拉取，成功则更新缓存与落地文件。"""
        labels = self._fetch_remote()
        if labels is None or not labels:
            # 远程失败：保持现有缓存不变，等下个周期再试
            return
        self._labels = labels
        self._last_refresh = time.time()
        # 落地缓存文件，供下次启动优先加载
        self._save_to_file(self.cache_path, labels)

    def init(self) -> None:
        """启动初始化：优先用远程缓存文件，其次本地 exchanges.json 兜底，然后立即尝试远程拉取。"""
        if os.path.exists(self.cache_path):
            self._labels = self._load_from_file(self.cache_path)
            logging.info("从缓存 %s 加载交易所标签 %d 个", self.cache_path, len(self._labels))
        elif self.local_path and os.path.exists(self.local_path):
            self._labels = self._load_from_file(self.local_path)
            logging.info("从本地 %s 加载交易所标签 %d 个（兜底）", self.local_path, len(self._labels))
        else:
            logging.warning("无可用交易所标签来源，启动后将以空表运行")

        if self.url:
            self._do_refresh()  # 启动时立即拉一次最新
        else:
            logging.info("未配置 exchanges_url，仅使用本地标签库")

    def maybe_refresh(self) -> None:
        """主循环周期性调用：超过 refresh_interval 才真正拉取。"""
        if not self.url:
            return
        if time.time() - self._last_refresh >= self.refresh_interval:
            self._do_refresh()

    def lookup(self, address: str) -> Optional[str]:
        """查询地址是否为已知交易所。是则返回标签，否则返回 None。"""
        return self._labels.get(address.lower())


# ------------------------------------------------------------------
# 通用 HTTP 请求（含指数退避重试，处理 429 限流 / 5xx / 网络错误）
# ------------------------------------------------------------------

def http_request_with_retry(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
) -> Optional[requests.Response]:
    """带指数退避的 HTTP 请求。
    成功返回 Response；多次重试仍失败则返回 None（由调用方决定降级行为）。
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(
                method, url,
                headers=headers, json=json_body, params=params, timeout=timeout,
            )
            # 429 限流或 5xx 服务端错误 -> 退避重试
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = RETRY_BACKOFF_BASE ** attempt
                logging.warning(
                    "HTTP %s %s 返回 %d，%ds 后重试 (%d/%d)",
                    method, url, resp.status_code, wait, attempt, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            return resp
        except requests.RequestException as e:
            last_err = e
            wait = RETRY_BACKOFF_BASE ** attempt
            logging.warning(
                "HTTP 请求异常 %s: %s，%ds 后重试 (%d/%d)",
                url, e, wait, attempt, MAX_RETRIES,
            )
            time.sleep(wait)
    logging.error("HTTP 请求最终失败 %s: %s", url, last_err)
    return None


# ------------------------------------------------------------------
# 价格获取（带本地缓存，避免高频请求 CoinGecko）
# ------------------------------------------------------------------

class PriceOracle:
    """从 CoinGecko 获取多代币 USD 价格，批量拉取 + TTL 缓存。

    CoinGecko simple/price 端点支持 ids=id1,id2,id3 批量查询，比逐个请求高效得多。
    内部维护 {coin_id: price} 字典，按统一 TTL 刷新。
    """

    def __init__(self, coin_ids: List[str], api_key: str, ttl_seconds: int):
        # 去重并过滤空值
        self.coin_ids: List[str] = list(dict.fromkeys(
            cid.strip() for cid in coin_ids if cid and cid.strip()
        ))
        self.api_key = api_key.strip() if api_key else ""
        self.ttl = max(ttl_seconds, 10)
        self._prices: Dict[str, Optional[float]] = {cid: None for cid in self.coin_ids}
        self._fetched_at: float = 0.0

    def _fetch(self) -> None:
        """批量拉取所有 coin_id 的 USD 价格，失败时保持旧缓存。"""
        if not self.coin_ids:
            return
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        params = {"ids": ",".join(self.coin_ids), "vs_currencies": "usd"}
        resp = http_request_with_retry(
            "GET", COINGECKO_PRICE_URL,
            headers=headers, params=params, timeout=15,
        )
        if resp is None or resp.status_code != 200:
            logging.error(
                "CoinGecko 批量价格获取失败 status=%s body=%s",
                getattr(resp, "status_code", None),
                getattr(resp, "text", None)[:200],
            )
            return

        try:
            data = resp.json()
            now = time.time()
            fetched_any = False
            for cid in self.coin_ids:
                price_data = data.get(cid)
                price_val = price_data.get("usd") if isinstance(price_data, dict) else None
                if price_val is not None:
                    self._prices[cid] = float(price_val)
                    fetched_any = True
                else:
                    # CoinGecko 返回的 coin_id 无 usd 字段：保持旧值或 None
                    if self._prices.get(cid) is None:
                        logging.warning("CoinGecko 未返回 %s.usd 字段（代币可能无 USD 价格）", cid)
            if fetched_any:
                self._fetched_at = now
                logging.info("代币价格刷新: %s",
                             ", ".join(f"{cid}=${self._prices[cid]:,.6f}"
                                       for cid in self.coin_ids
                                       if self._prices.get(cid) is not None))
        except (ValueError, KeyError, TypeError) as e:
            logging.exception("CoinGecko 批量价格解析失败: %s", e)

    def get_price_usd(self, coin_id: str) -> Optional[float]:
        """返回指定 coin_id 的 USD 单价。缓存过期则批量刷新全部。"""
        if coin_id not in self._prices:
            # 动态新增（理论上不会，tokens 初始化时已全部覆盖）
            self._prices[coin_id] = None

        now = time.time()
        if (now - self._fetched_at) >= self.ttl:
            self._fetch()

        return self._prices.get(coin_id)


# ------------------------------------------------------------------
# 纯 JSON-RPC 客户端（替代 web3 库，无 C 扩展编译依赖）
# ------------------------------------------------------------------

class EthRpcClient:
    """轻量以太坊 JSON-RPC 客户端，覆盖本程序所需的全部调用。"""

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url.rstrip("/")
        self._session = requests.Session()

    def _call(self, method: str, params: list, timeout: int = 20) -> Any:
        """发送 JSON-RPC 请求并返回 result 字段。失败返回 None。"""
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        resp = http_request_with_retry(
            "POST", self.rpc_url,
            headers={"Content-Type": "application/json"},
            json_body=payload, timeout=timeout,
        )
        if resp is None or resp.status_code != 200:
            logging.error(
                "RPC %s 失败 status=%s body=%s",
                method, getattr(resp, "status_code", None),
                getattr(resp, "text", "")[:200],
            )
            return None
        try:
            body = resp.json()
        except ValueError:
            logging.error("RPC %s 响应非 JSON: %s", method, resp.text[:200])
            return None
        if "error" in body:
            logging.error("RPC %s 返回错误: %s", method, body["error"])
            return None
        return body.get("result")

    def is_connected(self) -> bool:
        """通过 web3_clientVersion 检查连接。"""
        result = self._call("web3_clientVersion", [])
        return result is not None

    def block_number(self) -> int:
        """获取最新区块号。"""
        result = self._call("eth_blockNumber", [])
        if result is None:
            return 0
        return int(result, 16)

    def get_logs(self, from_block: int, to_block: int, address: str,
                 topics: List[str]) -> List[Dict[str, Any]]:
        """拉取事件日志。params 中的 fromBlock / toBlock 需传十六进制字符串。"""
        params = [{
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": address,
            "topics": topics,
        }]
        # eth_getLogs 偶发会因 RPC 节点限制 (block range) 失败，外层重试
        for attempt in range(1, MAX_RETRIES + 1):
            result = self._call("eth_getLogs", params, timeout=30)
            if result is None:
                wait = RETRY_BACKOFF_BASE ** attempt
                logging.warning(
                    "eth_getLogs [%d,%d] 失败，%ds 后重试 (%d/%d)",
                    from_block, to_block, wait, attempt, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            return result
        logging.error("eth_getLogs 多次重试失败，跳过区间 [%d,%d]", from_block, to_block)
        return []

    def call_decimals(self, token_address: str) -> Optional[int]:
        """调用 ERC20 decimals()，返回 uint8。失败返回 None。"""
        params = [{
            "to": token_address,
            "data": DECIMALS_SELECTOR,
        }, "latest"]
        result = self._call("eth_call", params)
        if result is None or result in ("0x", "0x0"):
            return None
        try:
            return int(result, 16)
        except ValueError:
            logging.error("解析 decimals 失败 raw=%s", result)
            return None


def init_rpc(rpc_url: str) -> EthRpcClient:
    """初始化 RPC 客户端并校验连接。失败直接退出。"""
    client = EthRpcClient(rpc_url)
    if not client.is_connected():
        raise ConnectionError(f"无法连接 RPC 节点: {rpc_url}")
    return client


def get_token_decimals(rpc: EthRpcClient, token_address: str, fallback: int) -> int:
    """读取代币合约 decimals()。若调用失败则使用 fallback。"""
    if fallback and fallback > 0:
        return fallback
    decimals = rpc.call_decimals(token_address)
    if decimals is not None:
        return decimals
    logging.warning("读取 decimals() 失败，使用 fallback=%d", fallback)
    return fallback if fallback > 0 else 18


def fetch_transfer_logs(
    rpc: EthRpcClient, token_addresses: List[str], from_block: int, to_block: int
) -> List[Dict[str, Any]]:
    """拉取 [from_block, to_block] 区间内多个代币合约的 Transfer 事件日志。

    JSON-RPC 一次 eth_getLogs 可传 address 数组: ["0xaaa...", "0xbbb..."]，
    比分别调用更高效。返回的每条 log 自带 address 字段，可区分是哪个代币。
    """
    if not token_addresses:
        return []
    if len(token_addresses) == 1:
        # 单合约时直接传字符串（部分 RPC 节点对数组兼容性差）
        return rpc.get_logs(from_block, to_block, token_addresses[0], [TRANSFER_EVENT_TOPIC])
    else:
        return rpc.get_logs(from_block, to_block, token_addresses, [TRANSFER_EVENT_TOPIC])


def _topic_to_address(topic: Any) -> str:
    """从 32 字节 indexed topic 中提取 20 字节地址，统一返回小写 0x 前缀。

    JSON-RPC 返回的 topics 是 hex 字符串（如 "0x000...000abcd"），
    左侧补零到 66 字符，地址在最后 40 个 hex 字符。
    """
    hex_str = str(topic)
    # 去掉 0x 前缀，取最后 40 个字符（20 字节地址）
    if hex_str.startswith("0x"):
        hex_str = hex_str[2:]
    return "0x" + hex_str[-40:].lower()


def parse_transfer_log(log: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析单条 Transfer 事件日志。
    返回 {tx_hash, block, from, to, raw_value}，解析失败返回 None。

    JSON-RPC 返回的 log 字段：
      - address: 合约地址
      - topics: [event_topic, from_topic, to_topic]  (hex 字符串)
      - data: uint256 数量 (hex 字符串，66 字符含 0x)
      - transactionHash: tx hash (hex)
      - blockNumber: 区块号 (hex)
      - logIndex: 日志序号 (hex)
    """
    try:
        topics = log.get("topics") or []
        if len(topics) < 3:
            return None
        from_addr = _topic_to_address(topics[1])
        to_addr = _topic_to_address(topics[2])

        # data = uint256 数量（hex 字符串）
        data_str = (log.get("data") or "0x").strip()
        if data_str in ("", "0x", "0X"):
            raw_value = 0
        else:
            raw_value = int(data_str, 16)

        tx_hash = log.get("transactionHash")
        if tx_hash and not str(tx_hash).startswith("0x"):
            tx_hash = "0x" + str(tx_hash)
        block = int(log.get("blockNumber", "0x0"), 16)
        log_index = int(log.get("logIndex", "0x0"), 16)

        return {
            "tx_hash": tx_hash,
            "block": block,
            "log_index": log_index,
            "from": from_addr,
            "to": to_addr,
            "raw_value": raw_value,
        }
    except Exception as e:  # noqa: BLE001
        logging.exception("解析 Transfer 日志失败: %s", e)
        return None


# ------------------------------------------------------------------
# 飞书 webhook 推送
# ------------------------------------------------------------------

def _feishu_sign(secret: str) -> tuple[str, str]:
    """飞书机器人签名算法（安全设置 → 签名校验时需要）。

    文档: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
    1. timestamp = 当前秒级时间戳
    2. string_to_sign = f"{timestamp}\n{secret}"
    3. sign = base64(hmac_sha256(string_to_sign, secret))
    """
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign


def send_feishu_alert(
    webhook_url: str,
    title: str,
    content_lines: List[str],
    link: str,
    secret: Optional[str] = None,
) -> bool:
    """组装飞书交互式卡片消息并 POST 到 webhook。
    成功返回 True。失败返回 False（不阻塞主循环，下一轮还会尝试但会被去重）。

    Args:
        webhook_url: 飞书机器人 webhook URL
        title: 卡片标题
        content_lines: 正文行列表
        link: 交易浏览器链接（卡片底部按钮）
        secret: 签名密钥（可选，仅当机器人开启签名校验时传入）
    """
    # 卡片正文：每行一段 + 末尾交易链接
    content_elements = [{"tag": "div", "text": {"tag": "lark_md",
                          "content": "\n".join(content_lines)}}]
    content_elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "查看交易 ↗"},
            "type": "primary",
            "url": link,
        }],
    })

    payload: Dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red",
            },
            "elements": content_elements,
        },
    }

    # 签名校验（机器人安全设置开启"签名校验"时必须携带 timestamp + sign）
    if secret:
        timestamp, sign = _feishu_sign(secret)
        payload["timestamp"] = timestamp
        payload["sign"] = sign

    resp = http_request_with_retry(
        "POST", webhook_url,
        headers={"Content-Type": "application/json"},
        json_body=payload,
        timeout=10,
    )
    if resp is None or resp.status_code != 200:
        logging.error("飞书 webhook 推送失败 status=%s body=%s",
                      getattr(resp, "status_code", None),
                      getattr(resp, "text", None)[:200])
        return False
    # 飞书成功时返回 {"StatusCode":0} 或 {"code":0}
    try:
        body = resp.json()
        code = body.get("StatusCode", body.get("code", 0))
        if code != 0:
            logging.error("飞书返回业务错误: %s", body)
            return False
    except ValueError:
        logging.error("飞书返回非 JSON: %s", getattr(resp, "text", "")[:200])
        return False
    return True


# ------------------------------------------------------------------
# 主监控器
# ------------------------------------------------------------------

class TransferMonitor:
    def __init__(self, config_path: str = DEFAULT_CONFIG_FILE):
        self.config = load_config(config_path)

        # 日志先初始化，便于后续流程都有日志输出
        self.log_file = self.config.get("log_file", DEFAULT_LOG_FILE)
        self._init_logging(self.log_file)

        # ---- 多链模式 vs 单链模式 ----
        # 多链模式：rpc_urls(dict) + chains(list) + tokens[].chain
        # 单链模式（旧 config.json 兼容）：rpc_url(str) + chain(str) + tokens[]
        self.multi_chain_mode = isinstance(self.config.get("rpc_urls"), dict) \
            and bool(self.config.get("rpc_urls"))

        if self.multi_chain_mode:
            self.chains: List[str] = list(self.config["chains"])
            self.rpcs: Dict[str, "EthRpcClient"] = {
                chain: init_rpc(self.config["rpc_urls"][chain])
                for chain in self.chains
            }
        else:
            # 单链模式：规范化为单元素结构
            self.chains = [self.config.get("chain", "ethereum").lower()]
            self.rpcs = {self.chains[0]: init_rpc(self.config["rpc_url"])}

        # 全局阈值 / 轮询参数
        self.alert_threshold = float(self.config.get("alert_usd_threshold", 100_000))
        self.dust_threshold = float(self.config.get("dust_usd_threshold", 100))
        self.confirmations = int(self.config.get("confirmations", 6))
        self.poll_interval = int(self.config.get("poll_interval_seconds", 12))

        # 解析代币列表（_parse_tokens 已为每条 token 加 chain 字段，
        # 缺省从 cfg["chain"] 取，向后兼容旧 config.json）
        self.tokens = _parse_tokens(self.config, rpc=None)

        # 校验每个 token 的 chain 都有对应 RPC 节点
        for t in self.tokens:
            if t["chain"] not in self.rpcs:
                raise ValueError(
                    f"代币 {t['symbol']}({t['contract'][:10]}...) "
                    f"chain={t['chain']} 未配置对应 RPC 节点"
                )

        # 用各自链的 rpc 修正 decimals（若 config 填了 0）
        for t in self.tokens:
            if t["decimals"] == 0:
                t["decimals"] = get_token_decimals(
                    self.rpcs[t["chain"]], t["contract"], fallback=18
                )

        # 按链分组 tokens + 构建合约地址 → token 映射（每条链独立一份）
        self.tokens_by_chain: Dict[str, List[Dict[str, Any]]] = {
            c: [] for c in self.chains
        }
        for t in self.tokens:
            self.tokens_by_chain[t["chain"]].append(t)
        self._contract_to_token_by_chain: Dict[str, Dict[str, Dict[str, Any]]] = {
            c: {t["contract"]: t for t in self.tokens_by_chain[c]}
            for c in self.chains
        }

        # PriceOracle（多链共用，按 coin_id 去重）
        all_coin_ids = [t["coingecko_id"] for t in self.tokens if t["coingecko_id"]]
        self.price_oracle = PriceOracle(
            coin_ids=all_coin_ids,
            api_key=self.config.get("coingecko_api_key", ""),
            ttl_seconds=int(self.config.get("price_cache_ttl_seconds", 60)),
        )

        # 交易所标签库
        self.exchanges = ExchangeLabelStore(
            url=self.config.get("exchanges_url"),
            local_path=self.config.get("exchanges_file", DEFAULT_EXCHANGES_FILE),
            refresh_interval_seconds=int(self.config.get("exchanges_refresh_seconds", 3600)),
        )
        self.exchanges.init()

        # 状态文件：按 chain 分桶（v2 格式），兼容旧 v1 单链状态自动迁移
        self.state_file = self.config.get("state_file", DEFAULT_STATE_FILE)
        self.states: Dict[str, Dict[str, Any]] = {
            chain: load_chain_state(self.state_file, chain) for chain in self.chains
        }

        self.feishu_webhook = self.config.get("feishu_webhook_url", "")
        self.feishu_webhook_secret = self.config.get("feishu_webhook_secret") or None

        # 每条链的浏览器前缀
        self.tx_link_prefixes: Dict[str, str] = {
            c: EXPLORER_TX_PREFIX.get(c, EXPLORER_TX_PREFIX["ethereum"])
            for c in self.chains
        }

        # ---- 单链兼容别名（指向第一项，便于旧代码路径访问）----
        self.rpc = self.rpcs[self.chains[0]]
        self.chain = self.chains[0]
        self.state = self.states[self.chains[0]]
        self._contract_to_token = self._contract_to_token_by_chain[self.chains[0]]
        self.tx_link_prefix = self.tx_link_prefixes[self.chains[0]]

        # 打印初始化摘要
        logging.info(
            "监控器初始化完成: chains=%s tokens=%d confirmations=%d multi_chain=%s",
            self.chains, len(self.tokens), self.confirmations, self.multi_chain_mode,
        )
        for c in self.chains:
            logging.info("  [%s] tokens=%d", c, len(self.tokens_by_chain[c]))
        for t in self.tokens:
            logging.info(
                "  - %s@%s: contract=%s decimals=%d coingecko_id=%s threshold=$%.2f",
                t["symbol"], t["chain"], t["contract"][:12] + "...", t["decimals"],
                t["coingecko_id"] or "(无)", t["alert_threshold"],
            )

    # ---- 日志 ----
    def _init_logging(self, log_file: str) -> None:
        import os
        fmt = "%(asctime)s [%(levelname)s] %(message)s"
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # pythonw.exe 模式（Windows 后台无 console）下 sys.stdout 是无效 fd，
        # StreamHandler flush 会触发 OSError Errno 22 累积拖垮进程。
        # 用 sys.executable 名字直接判断 pythonw 模式，最可靠。
        is_pythonw = os.path.basename(sys.executable).lower() == "pythonw.exe"
        if not is_pythonw and sys.stdout is not None:
            try:
                sys.stdout.write(" ")  # 非空字符 + flush 测试 stdout 真的可写
                sys.stdout.flush()
                sh = logging.StreamHandler(sys.stdout)
                sh.setFormatter(logging.Formatter(fmt))
                root.addHandler(sh)
            except (OSError, ValueError):
                pass  # stdout 无效，跳过 StreamHandler
        # 文件（追加，作为本地告警留痕）
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)

    # ---- 去重 ----
    def _already_alerted(self, chain: str, tx_hash: str) -> bool:
        """检查某条链上某笔 tx 是否已告警。按 chain 分桶避免跨链误判。"""
        state = self.states[chain]
        return tx_hash in state.get("alerted_txs", {})

    def _mark_alerted(self, chain: str, tx_hash: str) -> None:
        """标记某条链上某笔 tx 已告警。仅改内存，持久化由 _process_chain_range 完成。"""
        state = self.states[chain]
        state.setdefault("alerted_txs", {})[tx_hash] = int(time.time())
        # 控制 alerted_txs 体积，仅保留最近 10000 条
        if len(state["alerted_txs"]) > 10000:
            sorted_items = sorted(state["alerted_txs"].items(), key=lambda kv: kv[1])
            state["alerted_txs"] = dict(sorted_items[-10000:])

    # ---- 单笔转账处理 ----
    def _handle_transfer(self, t: Dict[str, Any], token_info: Dict[str, Any]) -> None:
        """处理单笔转账。

        Args:
            t: parse_transfer_log 的返回值
            token_info: 该代币的标准化配置 dict（含 contract / decimals / symbol / coingecko_id / alert_threshold / dust_threshold）
        """
        tx_hash = t["tx_hash"]
        chain = token_info["chain"]
        # 防重复告警
        if self._already_alerted(chain, tx_hash):
            return

        # 数量与 USD 价值
        amount = t["raw_value"] / (10 ** token_info["decimals"])
        coin_id = token_info.get("coingecko_id", "")
        price = self.price_oracle.get_price_usd(coin_id) if coin_id else None
        if price is None:
            # 价格获取失败时跳过本轮（下一轮还会重新拉到，因为有去重所以不会漏）
            # 若无 coingecko_id（可能是无价格的新币），也跳过 USD 换算告警
            if coin_id:
                logging.warning("价格不可用 %s，跳过 tx=%s，下一轮重试", token_info["symbol"], tx_hash)
            return
        usd_value = amount * price

        # 灰尘过滤（用代币自己的 dust_threshold，回退全局）
        dust_th = token_info.get("dust_threshold") or self.dust_threshold
        alert_th = token_info.get("alert_threshold") or self.alert_threshold
        if usd_value < dust_th:
            return
        if usd_value < alert_th:
            return

        # 若该代币标记了 skip_alert，仅记录金额到日志，不推送飞书也不标记已告警
        if token_info.get("skip_alert"):
            logging.info("[SKIP_ALERT] %s tx=%s usd=%.2f 达到阈值但配置了跳过告警",
                         token_info["symbol"], tx_hash, usd_value)
            return

        # 交易所识别
        to_label = self.exchanges.lookup(t["to"])
        from_label = self.exchanges.lookup(t["from"])

        # 组装告警消息（tx_link_prefix 按代币所在链取）
        tx_link = self.tx_link_prefixes[chain] + tx_hash
        symbol = token_info["symbol"]
        title = f"[{chain.upper()}] 大额 {symbol} 转账 ${usd_value:,.2f}"
        lines = [
            f"**链**: {chain}",
            f"**代币**: {symbol}",
            f"**数量**: {amount:,.4f}",
            f"**USD 价值**: ${usd_value:,.2f}",
            f"**发送方**: `{t['from']}`" + (f" ({from_label})" if from_label else ""),
            f"**接收方**: `{t['to']}`" + (f" ({to_label})" if to_label else ""),
            f"**区块**: {t['block']}",
            f"**TxHash**: `{tx_hash}`",
        ]
        # 发送方/接收方标签分类提示
        # exchanges.json 中可能含非交易所标签（如巨鲸、跨链桥、机构），
        # 通过交易所关键词判断是否为交易所地址，区分显示逻辑。
        EXCHANGE_KEYWORDS = (
            "binance", "okx", "okex", "coinbase", "kraken", "bybit",
            "bitfinex", "huobi", "kucoin", "gate", "mexc", "bitget",
            "upbit", "bithumb", "poloniex", "gemini", "crypto.com",
            "ftx", "bittrex", "bitstamp", "deribit", "bitmex",
        )

        def _is_exchange(label: str) -> bool:
            if not label:
                return False
            low = label.lower()
            return any(kw in low for kw in EXCHANGE_KEYWORDS)

        is_to_exchange = _is_exchange(to_label)
        is_from_exchange = _is_exchange(from_label)

        if is_from_exchange and is_to_exchange:
            lines.insert(0, f"⚠️ 交易所互转: from=**{from_label}** → to=**{to_label}**")
        elif is_to_exchange:
            lines.insert(0, f"⚠️ 接收方为交易所: **{to_label}**（资金流入，关注是否抛售）")
        elif is_from_exchange:
            lines.insert(0, f"⚠️ 发送方为交易所: **{from_label}**（提币流出，关注资金动向）")
        elif to_label and from_label:
            lines.insert(0, f"ℹ️ 标签地址互转: from=**{from_label}** → to=**{to_label}**")
        elif to_label:
            lines.insert(0, f"ℹ️ 接收方命中标签: **{to_label}**")
        elif from_label:
            lines.insert(0, f"ℹ️ 发送方命中标签: **{from_label}**")

        logging.info("触发告警 [%s@%s] tx=%s usd=%.2f from_exchange=%s to_exchange=%s",
                     symbol, chain, tx_hash, usd_value,
                     from_label or "N/A", to_label or "N/A")

        # 推送飞书（未配置 webhook 时跳过，仅记日志）
        if self.feishu_webhook:
            ok = send_feishu_alert(
                self.feishu_webhook, title, lines, tx_link,
                secret=self.feishu_webhook_secret,
            )
            # 无论推送是否成功都标记已告警，防止失败时无限重推刷屏
            self._mark_alerted(chain, tx_hash)
            if not ok:
                logging.error("飞书推送失败但已标记 [%s@%s] tx=%s，需人工核查 %s",
                              symbol, chain, tx_hash, self.log_file)
        else:
            logging.info("未配置飞书 webhook，跳过推送 [%s@%s] tx=%s（已标记为已告警）",
                         symbol, chain, tx_hash)
            self._mark_alerted(chain, tx_hash)

    # ---- 区间处理（按 chain 维度） ----
    def _process_chain_range(self, chain: str, from_block: int, to_block: int) -> None:
        """处理某条链上 [from_block, to_block] 区间内所有监控代币的 Transfer 事件。

        单链模式下 chains=[<chain>]，等价于旧 _process_range；
        多链模式下每条链独立调 eth_getLogs、独立 contract 映射、独立 state。
        """
        logging.info("[%s] 处理区块区间 [%d, %d]", chain, from_block, to_block)
        contracts = [t["contract"] for t in self.tokens_by_chain.get(chain, [])]
        if not contracts:
            return
        rpc = self.rpcs[chain]
        contract_map = self._contract_to_token_by_chain[chain]
        logs = fetch_transfer_logs(rpc, contracts, from_block, to_block)
        if not logs:
            return
        logging.info("[%s] 区间内 Transfer 事件数: %d", chain, len(logs))
        for log in logs:
            parsed = parse_transfer_log(log)
            if parsed is None:
                continue
            # 从 log.address 反查是哪个代币
            contract_addr = str(log.get("address", "")).lower()
            token_info = contract_map.get(contract_addr)
            if token_info is None:
                # 不应该发生，eth_getLogs 只返回指定 address 的日志
                logging.warning("[%s] Transfer 日志来自未监控合约: %s",
                                chain, contract_addr)
                continue
            try:
                self._handle_transfer(parsed, token_info)
            except Exception as e:  # noqa: BLE001  单笔异常不影响整体
                logging.exception("处理单笔转账异常 tx=%s: %s",
                                  parsed.get("tx_hash"), e)

    # ---- 主循环 ----
    def run(self) -> None:
        logging.info("监控器启动。轮询间隔 %ds，确认区块 %d，链数=%d chains=%s",
                     self.poll_interval, self.confirmations,
                     len(self.chains), self.chains)
        # 首次启动：每条链若状态为 0，则从 (当前块 - 确认数) 开始，
        # 避免从头扫描全链（多链模式下各链区块高度独立）
        for chain in self.chains:
            state = self.states[chain]
            if state["last_processed_block"] == 0:
                latest = self.rpcs[chain].block_number()
                state["last_processed_block"] = max(0, latest - self.confirmations)
                logging.info("[%s] 首次启动，从区块 %d 开始",
                             chain, state["last_processed_block"])
                save_chain_state(self.state_file, chain, state)

        while True:
            try:
                # 定期刷新交易所标签库（超过 refresh_interval 才真正发起请求）
                self.exchanges.maybe_refresh()

                # 逐链处理新块（每条链独立 block_number / state / eth_getLogs）
                for chain in self.chains:
                    state = self.states[chain]
                    rpc = self.rpcs[chain]
                    latest = rpc.block_number()
                    safe_block = latest - self.confirmations
                    last = state["last_processed_block"]
                    if safe_block > last:
                        # 单批最多 10 个块：Alchemy 免费档 eth_getLogs 限制
                        # 单次区块跨度 ≤10，超过会 400 错误。出块 ~12s/块，轮询
                        # 间隔 12s，每轮 10 块足够追上实时出块。
                        # 若落后追赶（如重启后），需要多轮才能追上，每轮 10 块。
                        to_block = min(safe_block, last + 10)
                        self._process_chain_range(chain, last + 1, to_block)
                        state["last_processed_block"] = to_block
                        save_chain_state(self.state_file, chain, state)
                    # else: 没有新块，静默跳到下一条链
            except KeyboardInterrupt:
                logging.info("收到中断信号，退出")
                break
            except Exception as e:  # noqa: BLE001  兜底，保证主循环不挂
                logging.exception("主循环异常（忽略并继续）: %s", e)
            time.sleep(self.poll_interval)


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

if __name__ == "__main__":
    # 配置文件路径可由命令行参数指定，默认 config.json
    # 敏感字段（RPC_URL / FEISHU_WEBHOOK_URL / COINGECKO_API_KEY）优先从 .env 或环境变量读取
    config_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_FILE
    monitor = TransferMonitor(config_file)
    monitor.run()
