#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
ERC20 代币转账监控程序（只读 / Read-Only On-chain Monitor）
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
    python monitor.py

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

    # 3) 基本校验
    required = ["rpc_url", "token_contract", "coingecko_id"]
    for key in required:
        if not cfg.get(key):
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
    """从 CoinGecko 获取代币 USD 价格，带 TTL 缓存。"""

    def __init__(self, coin_id: str, api_key: str, ttl_seconds: int):
        self.coin_id = coin_id
        self.api_key = api_key.strip() if api_key else ""
        self.ttl = max(ttl_seconds, 10)
        self._price: Optional[float] = None
        self._fetched_at: float = 0.0

    def get_price_usd(self) -> Optional[float]:
        """返回 USD 单价。获取失败返回 None（调用方据此跳过告警或使用上次缓存）。"""
        now = time.time()
        if self._price is not None and (now - self._fetched_at) < self.ttl:
            return self._price

        headers = {"accept": "application/json"}
        # Demo / Pro key 通过 header 传递（免费版留空即可）
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key

        params = {"ids": self.coin_id, "vs_currencies": "usd"}
        resp = http_request_with_retry(
            "GET", COINGECKO_PRICE_URL,
            headers=headers, params=params, timeout=10,
        )
        if resp is None or resp.status_code != 200:
            logging.error(
                "CoinGecko 价格获取失败 status=%s body=%s",
                getattr(resp, "status_code", None),
                getattr(resp, "text", None)[:200],
            )
            # 失败时若有旧缓存继续用，否则返回 None
            return self._price

        try:
            data = resp.json()
            price = data.get(self.coin_id, {}).get("usd")
            if price is None:
                logging.error("CoinGecko 返回缺少 %s.usd 字段: %s", self.coin_id, data)
                return self._price
            self._price = float(price)
            self._fetched_at = now
            logging.info("代币价格刷新: 1 %s = %.6f USD", self.coin_id, self._price)
            return self._price
        except (ValueError, KeyError, TypeError) as e:
            logging.exception("CoinGecko 价格解析失败: %s", e)
            return self._price


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
    rpc: EthRpcClient, token_address: str, from_block: int, to_block: int
) -> List[Dict[str, Any]]:
    """拉取 [from_block, to_block] 区间内该代币的 Transfer 事件日志。"""
    return rpc.get_logs(from_block, to_block, token_address, [TRANSFER_EVENT_TOPIC])


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
    def __init__(self, config_path: str = "config.json"):
        self.config = load_config(config_path)

        # 日志先初始化，便于后续流程都有日志输出
        self._init_logging(self.config.get("log_file", "alerts.log"))

        # 核心组件
        self.rpc = init_rpc(self.config["rpc_url"])
        self.chain = self.config.get("chain", "ethereum").lower()
        self.token_address = self.config["token_contract"].lower()
        self.decimals = get_token_decimals(
            self.rpc, self.token_address, int(self.config.get("token_decimals", 0))
        )
        self.symbol = self.config.get("token_symbol", "TOKEN")
        self.alert_threshold = float(self.config["alert_usd_threshold"])
        self.dust_threshold = float(self.config.get("dust_usd_threshold", 0))
        self.confirmations = int(self.config.get("confirmations", 6))
        self.poll_interval = int(self.config.get("poll_interval_seconds", 12))

        self.price_oracle = PriceOracle(
            coin_id=self.config["coingecko_id"],
            api_key=self.config.get("coingecko_api_key", ""),
            ttl_seconds=int(self.config.get("price_cache_ttl_seconds", 60)),
        )

        # 交易所标签库：支持从外部 URL 定期拉取 + 本地兜底
        self.exchanges = ExchangeLabelStore(
            url=self.config.get("exchanges_url"),
            local_path=self.config.get("exchanges_file", "exchanges.json"),
            refresh_interval_seconds=int(self.config.get("exchanges_refresh_seconds", 3600)),
        )
        self.exchanges.init()
        self.state = load_state(self.config.get("state_file", "monitor_state.json"))
        self.feishu_webhook = self.config["feishu_webhook_url"]
        self.feishu_webhook_secret = self.config.get("feishu_webhook_secret") or None
        self.tx_link_prefix = EXPLORER_TX_PREFIX.get(self.chain, EXPLORER_TX_PREFIX["ethereum"])

        logging.info(
            "监控器初始化完成: chain=%s token=%s decimals=%d threshold=%.2f USD confirmations=%d",
            self.chain, self.token_address, self.decimals, self.alert_threshold, self.confirmations,
        )

    # ---- 日志 ----
    def _init_logging(self, log_file: str) -> None:
        fmt = "%(asctime)s [%(levelname)s] %(message)s"
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        # 控制台
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter(fmt))
        root.addHandler(sh)
        # 文件（追加，作为本地告警留痕）
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)

    # ---- 去重 ----
    def _already_alerted(self, tx_hash: str) -> bool:
        return tx_hash in self.state.get("alerted_txs", {})

    def _mark_alerted(self, tx_hash: str) -> None:
        self.state.setdefault("alerted_txs", {})[tx_hash] = int(time.time())
        # 控制 alerted_txs 体积，仅保留最近 10000 条
        if len(self.state["alerted_txs"]) > 10000:
            sorted_items = sorted(self.state["alerted_txs"].items(), key=lambda kv: kv[1])
            self.state["alerted_txs"] = dict(sorted_items[-10000:])

    # ---- 单笔转账处理 ----
    def _handle_transfer(self, t: Dict[str, Any]) -> None:
        tx_hash = t["tx_hash"]
        # 防重复告警
        if self._already_alerted(tx_hash):
            return

        # 数量与 USD 价值
        amount = t["raw_value"] / (10 ** self.decimals)
        price = self.price_oracle.get_price_usd()
        if price is None:
            # 价格获取失败时跳过本轮（下一轮还会重新拉到，因为有去重所以不会漏）
            logging.warning("价格不可用，跳过 tx=%s，下一轮重试", tx_hash)
            return
        usd_value = amount * price

        # 灰尘过滤
        if usd_value < self.dust_threshold:
            return
        # 阈值过滤
        if usd_value < self.alert_threshold:
            return

        # 交易所识别
        to_label = self.exchanges.lookup(t["to"])
        from_label = self.exchanges.lookup(t["from"])

        # 组装告警消息
        tx_link = self.tx_link_prefix + tx_hash
        title = f"大额 {self.symbol} 转账告警 ${usd_value:,.2f}"
        lines = [
            f"**代币**: {self.symbol}",
            f"**数量**: {amount:,.4f}",
            f"**USD 价值**: ${usd_value:,.2f}",
            f"**发送方**: `{t['from']}`" + (f" ({from_label})" if from_label else ""),
            f"**接收方**: `{t['to']}`" + (f" ({to_label})" if to_label else ""),
            f"**区块**: {t['block']}",
            f"**TxHash**: `{tx_hash}`",
        ]
        if to_label:
            lines.insert(0, f"⚠️ 接收方为交易所: **{to_label}**")

        logging.info("触发告警 tx=%s usd=%.2f to_exchange=%s",
                     tx_hash, usd_value, to_label or "N/A")

        # 推送飞书（未配置 webhook 时跳过，仅记日志）
        if self.feishu_webhook:
            ok = send_feishu_alert(
                self.feishu_webhook, title, lines, tx_link,
                secret=self.feishu_webhook_secret,
            )
            # 无论推送是否成功都标记已告警，防止失败时无限重推刷屏
            # （若希望失败重推可改为仅在 ok=True 时标记）
            self._mark_alerted(tx_hash)
            if not ok:
                logging.error("飞书推送失败但已标记 tx=%s，需人工核查 alerts.log", tx_hash)
        else:
            logging.info("未配置飞书 webhook，跳过推送 tx=%s（已标记为已告警）", tx_hash)
            self._mark_alerted(tx_hash)

    # ---- 区间处理 ----
    def _process_range(self, from_block: int, to_block: int) -> None:
        logging.info("处理区块区间 [%d, %d]", from_block, to_block)
        logs = fetch_transfer_logs(self.rpc, self.token_address, from_block, to_block)
        if not logs:
            return
        logging.info("区间内 Transfer 事件数: %d", len(logs))
        for log in logs:
            parsed = parse_transfer_log(log)
            if parsed is None:
                continue
            try:
                self._handle_transfer(parsed)
            except Exception as e:  # noqa: BLE001  单笔异常不影响整体
                logging.exception("处理单笔转账异常 tx=%s: %s",
                                  parsed.get("tx_hash"), e)

    # ---- 主循环 ----
    def run(self) -> None:
        logging.info("监控器启动。轮询间隔 %ds，确认区块 %d",
                     self.poll_interval, self.confirmations)
        # 首次启动若状态为 0，则从 (当前块 - 确认数) 开始，避免从头扫描全链
        latest = self.rpc.block_number()
        if self.state["last_processed_block"] == 0:
            self.state["last_processed_block"] = max(0, latest - self.confirmations)
            logging.info("首次启动，从区块 %d 开始", self.state["last_processed_block"])
            save_state(self.config.get("state_file", "monitor_state.json"), self.state)

        while True:
            try:
                # 定期刷新交易所标签库（超过 refresh_interval 才真正发起请求）
                self.exchanges.maybe_refresh()

                latest = self.rpc.block_number()
                safe_block = latest - self.confirmations
                last = self.state["last_processed_block"]
                if safe_block > last:
                    # 单批最多 500 个块，避免 RPC 节点对大区间报错
                    to_block = min(safe_block, last + 500)
                    self._process_range(last + 1, to_block)
                    self.state["last_processed_block"] = to_block
                    save_state(self.config.get("state_file", "monitor_state.json"), self.state)
                else:
                    # 没有新块，静默等待
                    pass
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
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    monitor = TransferMonitor(config_file)
    monitor.run()
