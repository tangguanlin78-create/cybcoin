"""链上数据获取：代币元信息、大额转账、Top10 持有人。

数据源策略：
- 代币 decimals/totalSupply/symbol/name：Etherscan `token` module + RPC `eth_call` 兜底
- 大额转账：Etherscan `account,tokentx` 按 contractaddress 拉取近 10000 笔，按 USD 价值过滤
- Top10 持有人：优先 Blockscout `/v2/tokens/{addr}/holders`，失败则尝试 Etherscan Pro
            `token,tokenholderlist`（需 Pro key），都不可用时返回空列表
- 地址标签：chains.lookup_address_tag 本地表
- 代币单价：Etherscan `token,tokeninfo`（ETH 主网）+ DexScreener 兜底（可选）

所有网络调用统一经过 requests.Session，超时与限速由 config.query 决定。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from chains import ChainConfig, lookup_address_tag

logger = logging.getLogger(__name__)


@dataclass
class TokenInfo:
    address: str
    name: str = ""
    symbol: str = ""
    decimals: int = 18
    total_supply: float = 0.0      # 人类可读
    price_usd: Optional[float] = None


@dataclass
class Transfer:
    hash: str
    from_addr: str
    to_addr: str
    value: float                   # 人类可读数量
    value_usd: Optional[float]
    timestamp: str                 # YYYY-MM-DD HH:MM:SS
    from_tag: Optional[str] = None
    to_tag: Optional[str] = None


@dataclass
class Holder:
    address: str
    balance: float                 # 人类可读数量
    share: float                   # 占总供应比例 0~1
    label: Optional[str] = None
    type: Optional[str] = None


def _short_err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"


class DataSource:
    def __init__(self, chain: ChainConfig, page_size: int = 10000,
                 timeout: int = 15, rate_limit_sleep: float = 0.25):
        self.chain = chain
        self.page_size = page_size
        self.timeout = timeout
        self.rate_limit_sleep = rate_limit_sleep
        self._session = requests.Session()

    # ---------- 低层 ----------

    def _etherscan_get(self, params: dict) -> Optional[dict]:
        if not self.chain.etherscan_base or not self.chain.etherscan_key:
            logger.warning("链 %s 未配置 Etherscan base/key，跳过", self.chain.chain_id)
            return None
        params = {**params, "apikey": self.chain.etherscan_key}
        try:
            r = self._session.get(self.chain.etherscan_base, params=params,
                                   timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            logger.error("Etherscan 请求失败 (%s): %s", params.get("module"), _short_err(e))
            return None
        except ValueError:
            logger.error("Etherscan 返回非 JSON: %s", r.text[:200])
            return None
        time.sleep(self.rate_limit_sleep)
        if data.get("status") == "0":
            # 非错误：仅表示无更多数据
            msg = data.get("message", "")
            if "No transactions" in msg or "No data" in msg:
                return data
            logger.warning("Etherscan 返回 status=0: %s (module=%s action=%s)",
                           msg, params.get("module"), params.get("action"))
        return data

    def _blockscout_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        if not self.chain.blockscout_base:
            return None
        url = self.chain.blockscout_base.rstrip("/") + path
        try:
            r = self._session.get(url, params=params or {}, timeout=self.timeout)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            logger.warning("Blockscout 请求失败 (%s): %s", path, _short_err(e))
            return None
        except ValueError:
            logger.warning("Blockscout 返回非 JSON (%s): %s", path, r.text[:200])
            return None
        time.sleep(self.rate_limit_sleep)
        return data

    def _rpc_call(self, to: str, data: str) -> Optional[str]:
        if not self.chain.rpc_url:
            return None
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": to, "data": data}, "latest"]}
        try:
            r = self._session.post(self.chain.rpc_url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            return (r.json().get("result") or "").strip()
        except Exception as e:
            logger.warning("RPC eth_call 失败 (%s): %s", to, _short_err(e))
            return None

    # ---------- 代币元信息 ----------

    def get_token_info(self, address: str) -> TokenInfo:
        address = address.strip()
        info = TokenInfo(address=address)
        # 1) Etherscan token info（仅 ETH 主网价格）
        eth_info = self._etherscan_get({
            "module": "token", "action": "tokeninfo",
            "contractaddress": address,
        }) or {}
        for row in eth_info.get("result") or []:
            if row.get("symbol"):
                info.symbol = row.get("symbol", "")
                info.name = row.get("name", "")
                if row.get("tokenPriceUSD"):
                    try:
                        info.price_usd = float(row["tokenPriceUSD"])
                    except ValueError:
                        pass
                break

        # 2) decimals via RPC
        # decimals() = 0x313ce567
        dec_hex = self._rpc_call(address, "0x313ce567")
        if dec_hex and dec_hex != "0x":
            try:
                info.decimals = int(dec_hex, 16)
                if info.decimals < 0 or info.decimals > 36:
                    info.decimals = 18
            except ValueError:
                pass

        # 3) symbol via RPC（若 Etherscan 未给）
        if not info.symbol:
            # symbol() = 0x95d89b41
            sym_hex = self._rpc_call(address, "0x95d89b41")
            info.symbol = _decode_string_hex(sym_hex) or info.symbol
        if not info.name:
            name_hex = self._rpc_call(address, "0x06fdde03")
            info.name = _decode_string_hex(name_hex) or info.name

        # 4) totalSupply via RPC
        ts_hex = self._rpc_call(address, "0x18160ddd")
        if ts_hex and ts_hex != "0x":
            try:
                raw = int(ts_hex, 16)
                info.total_supply = raw / (10 ** info.decimals)
            except ValueError:
                pass

        return info

    # ---------- 大额转账 ----------

    def fetch_transfers(self, token_addr: str, decimals: int,
                        symbol: str, price_usd: Optional[float],
                        threshold_usd: float, window_hours: int) -> List[Transfer]:
        """拉取该代币最近 tokentx，按 window_hours 与 threshold_usd 过滤。"""
        token_addr = token_addr.strip()
        data = self._etherscan_get({
            "module": "account", "action": "tokentx",
            "contractaddress": token_addr,
            "page": 1, "offset": self.page_size,
            "sort": "desc",
        }) or {}
        rows = data.get("result") or []
        if not isinstance(rows, list):
            return []

        now = time.time()
        cutoff = now - window_hours * 3600
        out: List[Transfer] = []
        for r in rows:
            try:
                ts = int(r.get("timeStamp", "0"))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            try:
                raw = int(r.get("value", "0"))
            except ValueError:
                continue
            value = raw / (10 ** decimals)
            value_usd = value * price_usd if price_usd else None
            if value_usd is not None and value_usd < threshold_usd:
                continue
            # 无价格时改按数量过滤（用阈值按 1 USD 估，仅当 price 缺失）
            if value_usd is None:
                continue
            from_addr = r.get("from", "")
            to_addr = r.get("to", "")
            out.append(Transfer(
                hash=r.get("hash", ""),
                from_addr=from_addr,
                to_addr=to_addr,
                value=value,
                value_usd=value_usd,
                timestamp=_ts_to_str(ts),
                from_tag=(lookup_address_tag(self.chain.chain_id, from_addr) or {}).get("label"),
                to_tag=(lookup_address_tag(self.chain.chain_id, to_addr) or {}).get("label"),
            ))
        # 按 USD 价值降序
        out.sort(key=lambda x: x.value_usd or 0, reverse=True)
        return out

    # ---------- Top10 持有人 ----------

    def fetch_top_holders(self, token_addr: str, decimals: int,
                          total_supply: float, limit: int = 10) -> List[Holder]:
        token_addr = token_addr.strip()
        holders_raw = self._fetch_blockscout_holders(token_addr, limit)
        if holders_raw is None:
            holders_raw = self._fetch_etherscan_holders(token_addr, limit)

        if not holders_raw:
            return []

        out: List[Holder] = []
        for h in holders_raw[:limit]:
            addr = (h.get("address") or "").strip()
            bal = _to_float(h.get("balance_raw") or h.get("value") or h.get("balance"))
            if decimals and bal:
                bal = bal / (10 ** decimals)
            share = (bal / total_supply) if total_supply else 0.0
            tag = lookup_address_tag(self.chain.chain_id, addr) or {}
            out.append(Holder(
                address=addr,
                balance=bal,
                share=share,
                label=tag.get("label"),
                type=tag.get("type"),
            ))
        # 重新按 share 降序
        out.sort(key=lambda x: x.share, reverse=True)
        return out[:limit]

    def _fetch_blockscout_holders(self, token_addr: str, limit: int) -> Optional[list]:
        # Blockscout v2: /v2/tokens/{addr}/holders?page-size=N
        data = self._blockscout_get(f"/v2/tokens/{token_addr}/holders",
                                    params={"page_size": max(limit, 50)})
        if not data:
            return None
        rows = data.get("items") or data.get("holders") or []
        out = []
        for r in rows:
            addr = (r.get("address") or {}).get("hash") or r.get("address") or ""
            # Blockscout 返回 balance 原始值（字符串）
            bal = r.get("value") or r.get("balance")
            out.append({
                "address": addr,
                "balance_raw": bal,
            })
        return out

    def _fetch_etherscan_holders(self, token_addr: str, limit: int) -> Optional[list]:
        # Etherscan Pro: token,tokenholderlist
        data = self._etherscan_get({
            "module": "token", "action": "tokenholderlist",
            "contractaddress": token_addr,
            "page": 1, "offset": max(limit, 100),
        })
        if not data or data.get("status") == "0":
            return None
        rows = data.get("result") or []
        out = []
        for r in rows:
            out.append({
                "address": r.get("TokenHolderAddress", ""),
                "value": r.get("TokenHolderQuantity", "0"),
            })
        return out


# ---------- 辅助函数 ----------

def _decode_string_hex(hex_str: Optional[str]) -> str:
    """解码 ABI 返回的 string 类型（offset 0x20 + 长度 + UTF-8 数据）。"""
    if not hex_str or hex_str == "0x":
        return ""
    h = hex_str[2:] if hex_str.startswith("0x") else hex_str
    if len(h) < 128:
        return ""
    try:
        length = int(h[64:128], 16)
        data = h[128: 128 + length * 2]
        return bytes.fromhex(data).decode("utf-8", errors="ignore")
    except (ValueError, Exception):
        return ""


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


def _ts_to_str(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
