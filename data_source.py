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

# CoinGecko 各链 platform 名（用于按合约地址查价格）
COINGECKO_PLATFORMS = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
    "base": "base",
    "avalanche": "avalanche",
}


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
        # Etherscan V2：带 chainid（ETH=1, BSC=56, ...）
        params = {**params, "apikey": self.chain.etherscan_key}
        if self.chain.etherscan_chainid:
            params["chainid"] = self.chain.etherscan_chainid
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
        """优先用 Etherscan proxy 模块做 eth_call（同一个 key 鉴权，无需外部 RPC），
        rpc_url 仅作兜底。"""
        if self.chain.etherscan_base and self.chain.etherscan_key and self.chain.etherscan_chainid:
            params = {
                "chainid": self.chain.etherscan_chainid,
                "module": "proxy", "action": "eth_call",
                "to": to, "data": data, "tag": "latest",
                "apikey": self.chain.etherscan_key,
            }
            try:
                r = self._session.get(self.chain.etherscan_base, params=params,
                                      timeout=self.timeout)
                r.raise_for_status()
                res = r.json()
                return (res.get("result") or "").strip()
            except Exception as e:
                logger.warning("Etherscan proxy eth_call 失败 (%s): %s", to, _short_err(e))
        if self.chain.rpc_url:
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
        result = eth_info.get("result")
        if isinstance(result, list):
            for row in result:
                if isinstance(row, dict) and row.get("symbol"):
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

        # 5) 价格兜底：CoinGecko 公开 API（etherscan tokeninfo 是 Pro 接口，免费版拿不到）
        if info.price_usd is None:
            info.price_usd = self._coingecko_price(address)

        return info

    def _coingecko_price(self, address: str) -> Optional[float]:
        """从 CoinGecko 公开 API 获取代币 USD 单价（无需 key，按合约地址查询）。
        每条链对应一个 platform。
        """
        platform = COINGECKO_PLATFORMS.get(self.chain.chain_id)
        if not platform:
            return None
        url = f"https://api.coingecko.com/api/v3/simple/token_price/{platform}"
        try:
            r = self._session.get(url, params={
                "contract_addresses": address,
                "vs_currencies": "usd",
            }, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            logger.warning("CoinGecko 请求失败: %s", _short_err(e))
            return None
        except ValueError:
            return None
        time.sleep(self.rate_limit_sleep)
        entry = data.get(address.lower()) or {}
        try:
            return float(entry["usd"]) if entry.get("usd") is not None else None
        except (TypeError, ValueError):
            return None

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
                          total_supply: float, limit: int = 10,
                          transfers: Optional[List["Transfer"]] = None) -> List[Holder]:
        token_addr = token_addr.strip()
        holders_raw = self._fetch_blockscout_holders(token_addr, limit)
        if holders_raw is None:
            holders_raw = self._fetch_etherscan_holders(token_addr, limit)

        if holders_raw:
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

        # 兜底：Blockscout/Etherscan 都拿不到时，从近期转账聚合 Top 地址
        if transfers:
            return self._aggregate_top_from_transfers(transfers, limit)

        return []

    def _aggregate_top_from_transfers(self, transfers: List["Transfer"],
                                      limit: int) -> List[Holder]:
        """近期转账聚合：按地址涉及金额(USD)排名，share=占近期转账总额比例。
        非真实持有人占比，仅作 Blockscout 不可用时的参考（对 USDT 这类
        持有人过多的 token 尤其有用）。"""
        agg: dict = {}
        for t in transfers:
            for addr in (t.from_addr, t.to_addr):
                if not addr:
                    continue
                agg[addr] = agg.get(addr, 0) + (t.value_usd or 0)
        ranked = sorted(agg.items(), key=lambda x: x[1], reverse=True)[:limit]
        total = sum(v for _, v in ranked) or 1.0
        out: List[Holder] = []
        for addr, val in ranked:
            tag = lookup_address_tag(self.chain.chain_id, addr) or {}
            out.append(Holder(
                address=addr,
                balance=val,   # USD 金额（非 token 数量）
                share=val / total,
                label=tag.get("label"),
                type=tag.get("type"),
            ))
        return out

    def _fetch_blockscout_holders(self, token_addr: str, limit: int) -> Optional[list]:
        # Blockscout v2: base 已含 /api/v2，path 用 /tokens/{addr}/holders
        # 注意：v2 不接受 page_size 参数（会 422），默认返回前 50 条
        base = (self.chain.blockscout_base or "").rstrip("/")
        if not base:
            return None
        url = base + f"/tokens/{token_addr.lower()}/holders"
        try:
            # holders 接口对大 token 可能慢，给更长超时
            r = self._session.get(url, timeout=max(self.timeout, 40))
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("Blockscout holders 请求失败 (%s): %s",
                           token_addr[:10], _short_err(e))
            return None
        time.sleep(self.rate_limit_sleep)
        rows = data.get("items") or data.get("holders") or []
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            addr_obj = r.get("address")
            if isinstance(addr_obj, dict):
                addr = addr_obj.get("hash") or ""
            else:
                addr = str(addr_obj or "")
            raw = r.get("value") or r.get("balance") or "0"
            try:
                if isinstance(raw, str) and raw.startswith("0x"):
                    bal = int(raw, 16)
                else:
                    bal = int(raw)
            except (ValueError, TypeError):
                bal = 0
            out.append({"address": addr, "balance_raw": bal})
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
