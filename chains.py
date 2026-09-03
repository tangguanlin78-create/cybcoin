"""多链 EVM 配置加载、代币符号解析、地址标签查询。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
CONFIG_EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.yaml")
TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.yaml")
TAGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "address_tags.yaml")


@dataclass
class ChainConfig:
    chain_id: str          # 我们内部用的链标识（ethereum/bsc/...）
    label: str
    etherscan_chainid: int  # Etherscan V2 的 chainid（1/56/137/...）
    etherscan_base: str
    etherscan_key: str
    explorer_tx: str
    explorer_address: str
    blockscout_base: str
    rpc_url: str


def load_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config() -> dict:
    """优先加载 config.yaml，缺失则回退 config.example.yaml。"""
    if os.path.exists(CONFIG_PATH):
        return load_yaml(CONFIG_PATH)
    logger.warning("未找到 config.yaml，回退到 config.example.yaml，请复制为 config.yaml 并填入真实值")
    return load_yaml(CONFIG_EXAMPLE)


def list_chains(config: dict) -> list:
    """返回 [(chain_id, label), ...]，用于下拉框。"""
    chains = config.get("chains") or {}
    return [(cid, c.get("label", cid)) for cid, c in chains.items()]


def get_chain(config: dict, chain_id: str) -> Optional[ChainConfig]:
    chains = config.get("chains") or {}
    c = chains.get(chain_id)
    if not c:
        return None
    return ChainConfig(
        chain_id=chain_id,
        label=c.get("label", chain_id),
        etherscan_chainid=int(c.get("chainid") or 0),
        etherscan_base=c.get("etherscan_base", ""),
        etherscan_key=(c.get("etherscan_key") or "").strip(),
        explorer_tx=c.get("explorer_tx", ""),
        explorer_address=c.get("explorer_address", ""),
        blockscout_base=c.get("blockscout_base", ""),
        rpc_url=(c.get("rpc_url") or "").strip(),
    )


def resolve_token_address(chain_id: str, symbol: str) -> Optional[str]:
    """从 tokens.yaml 反查合约地址。symbol 大小写不敏感。"""
    table = load_yaml(TOKENS_PATH).get(chain_id) or {}
    if not isinstance(table, dict):
        return None
    # 精确匹配
    for k, v in table.items():
        if k.upper() == symbol.upper().strip():
            return _extract_addr(v)
    return None


def _extract_addr(v) -> Optional[str]:
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        a = (v.get("address") or "").strip()
        return a or None
    return None


def lookup_address_tag(chain_id: str, address: str) -> Optional[dict]:
    """从 address_tags.yaml 查地址标签。返回 {"label","type"} 或 None。"""
    if not address:
        return None
    addr_lower = address.lower()
    table = load_yaml(TAGS_PATH).get(chain_id) or {}
    if not isinstance(table, dict):
        return None
    for k, v in table.items():
        if k.lower() == addr_lower:
            if isinstance(v, dict):
                return {"label": v.get("label", "已知"), "type": v.get("type", "unknown")}
            if isinstance(v, str):
                return {"label": v, "type": "unknown"}
    return None


def get_query_defaults(config: dict) -> dict:
    q = config.get("query") or {}
    return {
        "threshold_usd": float(q.get("default_large_threshold_usd", 100000)),
        "window_hours": int(q.get("default_window_hours", 24)),
        "top_holders_limit": int(q.get("top_holders_limit", 10)),
        "page_size": int(q.get("transfer_page_size", 10000)),
        "timeout": int(q.get("request_timeout", 15)),
        "rate_limit_sleep": float(q.get("rate_limit_sleep", 0.25)),
    }


def setup_logging(config: dict) -> None:
    cfg = config.get("logging") or {}
    level = getattr(logging, str(cfg.get("level", "INFO")).upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file = cfg.get("file")
    handlers = [logging.StreamHandler()]
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as e:
            logger.warning("无法创建日志文件 %s: %s", log_file, e)
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
