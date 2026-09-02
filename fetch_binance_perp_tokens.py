#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
币安 U 本位永续合约 → 多链 ERC20 合约地址抓取脚本
================================================================================

用途：
    1. 从币安 fapi 拉取所有 USDT 本位永续合约的 base asset 列表
       （如 BTC / ETH / SOL / LINK / PEPE ...，约 500+ 个标的）
    2. 调 CoinGecko coins/list?include_platform=true（单次请求即可拿到所有币种
       在所有链上的合约地址）构建 symbol -> [{coin_id, platforms}] 映射
    3. 为每个 base asset 选定正确的 coin_id（多候选时优先取在 ethereum 上
       有合约地址的，避免把 ETH 错配成 "anubis-bridged-eth-anubis" 这种同名山寨）
    4. 输出 binance_perp_tokens.json：每币在 7 条 EVM 链上的合约地址
       ethereum / bsc / polygon / arbitrum / optimism / base / avalanche

为何独立成脚本而不是塞进 all_coin_alarm.py：
    - 这是周期性数据抓取，不属于监控主循环
    - 输出文件作为 all_coin_alarm.py config.json 的数据源
    - 失败可重跑，不影响 all_coin_alarm.py 在跑的进程

为何用 coins/list?include_platform=true 而不是逐个 coins/{id}：
    - 一次请求拿全所有币种的所有链上合约地址，秒级完成
    - 免费版限流宽松（Demo tier ~30 calls/min，这里只发 1 次调用）
    - coins/{id} 端点需要 500+ 次请求，至少 15 分钟且易被限流

输出结构 binance_perp_tokens.json：
    {
      "fetched_at": 1234567890,
      "binance_perp_count": 526,
      "matched_with_contract": 187,
      "tokens": [
        {
          "base": "BTC",
          "binance_symbol": "BTCUSDT",
          "coingecko_id": "bitcoin",
          "platforms": {
            "ethereum": {"contract": "0x2260fac5e5554a1c4c4c4c4c4c4c4c4c4c4c4c4c4", "decimals": 0},
            ...
          }
        },
        ...
      ],
      "missed": [
        {"base": "XRP", "reason": "no target-chain contract on CoinGecko"},
        ...
      ]
    }

用法：
    # 全量抓取（约 5-10 秒，CoinGecko 一次请求即完成）
    python fetch_binance_perp_tokens.py

    # 用 CoinGecko Demo API key（推荐，限流更宽松）
    python fetch_binance_perp_tokens.py --api-key CG-xxxx

    # 指定输出文件
    python fetch_binance_perp_tokens.py --out my_tokens.json

    # 调试单个币种
    python fetch_binance_perp_tokens.py --bases BTC,ETH,SOL

注意：
    - 该脚本只读取公开数据，不涉及任何私钥/签名
    - 输出的合约地址仍建议人工抽检后再合并进 config.json
    - decimals 字段输出为 0，由 all_coin_alarm.py 启动时通过 RPC 读 decimals() 自动补全
================================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

BINANCE_FAPI_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# 带 include_platform=true 参数，一次返回所有币种在所有链上的合约地址
COINGECKO_COINS_LIST_WITH_PLATFORMS = (
    "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
)

# CoinGecko platform id -> 本项目 all_coin_alarm.py 使用的 chain 名
# 仅保留 all_coin_alarm.py 支持的 7 条链
PLATFORM_TO_CHAIN: Dict[str, str] = {
    "ethereum":           "ethereum",
    "binance-smart-chain": "bsc",
    "polygon-pos":        "polygon",
    "arbitrum-one":       "arbitrum",
    "optimistic-ethereum": "optimism",
    "base":               "base",
    "avalanche":          "avalanche",
}

MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 2.0


# ------------------------------------------------------------------
# HTTP 重试
# ------------------------------------------------------------------

def http_get_with_retry(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Optional[requests.Response]:
    """带指数退避的 HTTP GET。429/5xx 重试，最终失败返回 None。

    429 时额外多睡 30s，应对 CoinGecko 限流。
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = RETRY_BACKOFF_BASE ** attempt
                if resp.status_code == 429:
                    wait = max(wait, 30.0)  # 限流时多睡
                print(f"[WARN] HTTP {resp.status_code} {url}, {wait}s 后重试 "
                      f"({attempt}/{MAX_RETRIES})", flush=True)
                time.sleep(wait)
                continue
            return resp
        except requests.RequestException as e:
            last_err = e
            wait = RETRY_BACKOFF_BASE ** attempt
            print(f"[WARN] {url} 异常: {e}, {wait}s 后重试 "
                  f"({attempt}/{MAX_RETRIES})", flush=True)
            time.sleep(wait)
    print(f"[ERROR] {url} 最终失败: {last_err}", flush=True)
    return None


# ------------------------------------------------------------------
# 币安 U 本位合约列表
# ------------------------------------------------------------------

def fetch_binance_perp_bases() -> List[str]:
    """拉币安 fapi exchangeInfo，返回 USDT 本位永续合约的 base asset 列表。

    过滤条件：
      - contractType == "PERPETUAL"
      - quoteAsset == "USDT"
      - status == "TRADING"
    """
    resp = http_get_with_retry(BINANCE_FAPI_EXCHANGE_INFO, timeout=30)
    if resp is None or resp.status_code != 200:
        raise RuntimeError(
            f"币安 fapi exchangeInfo 拉取失败: status="
            f"{getattr(resp, 'status_code', None)}"
        )
    data = resp.json()
    symbols = data.get("symbols", [])
    bases: set = set()
    for s in symbols:
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            bases.add(s["baseAsset"])
    return sorted(bases)


# ------------------------------------------------------------------
# CoinGecko coins/list?include_platform=true
# ------------------------------------------------------------------

def fetch_coingecko_coins_with_platforms(api_key: str) -> Dict[str, List[Dict[str, Any]]]:
    """单次请求拉取所有币种及其在各链上的合约地址。

    返回 symbol(大写) -> [ {coin_id, platforms}, ... ] 映射。

    CoinGecko symbol 不唯一，每个 symbol 可能对应多个项目（如 BTC 有 bitcoin
    和几十个桥接/山寨版本），所以每个 symbol 映射到一个候选列表，
    调用方按"是否在 ethereum 上有合约地址"挑选正确的 coin_id。

    返回的 platforms 字段结构：
        {
          "ethereum": "0x2260fac5e5554a1c4c4c4c4c4c4c4c4c4c4c4c4c4",
          "binance-smart-chain": "0x...",
          ...
        }
    """
    headers = {"accept": "application/json"}
    if api_key:
        headers["x-cg-demo-api-key"] = api_key

    resp = http_get_with_retry(
        COINGECKO_COINS_LIST_WITH_PLATFORMS, headers=headers, timeout=60,
    )
    if resp is None or resp.status_code != 200:
        raise RuntimeError(
            f"CoinGecko coins/list?include_platform=true 失败: status="
            f"{getattr(resp, 'status_code', None)}"
        )
    coins = resp.json()
    sym_map: Dict[str, List[Dict[str, Any]]] = {}
    for c in coins:
        sym = (c.get("symbol") or "").upper()
        if sym:
            sym_map.setdefault(sym, []).append({
                "coin_id": c.get("id"),
                "name": c.get("name", ""),
                "platforms": c.get("platforms") or {},
            })
    return sym_map


def pick_correct_candidate(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从同 symbol 的多个候选中挑出"正确的"那个 coin_id。

    策略（按优先级）：
      1. 优先返回在 ethereum 上有合约地址的（主流 EVM 原生代币或以太坊 ERC20 主版本）
      2. 次之：在任意目标链上有合约地址的
      3. 都没有：返回第一个候选（多为非 EVM 币，如 BTC/SOL/XRP 原生）

    这能避免把 ETH 错配成 "anubis-bridged-eth-anubis" 这类桥接同名币，
    因为桥接版本通常没有 ethereum 平台合约（它们是在 Anubis 链上发行 ETH 的包装）。
    """
    if not candidates:
        return None
    # 1) 优先：在 ethereum 上有合约地址
    for c in candidates:
        plats = c.get("platforms") or {}
        eth_addr = plats.get("ethereum")
        if isinstance(eth_addr, str) and eth_addr.startswith("0x") and len(eth_addr) == 42:
            return c
    # 2) 次之：在任意目标链上有合约地址
    target_plats = set(PLATFORM_TO_CHAIN.keys())
    for c in candidates:
        plats = c.get("platforms") or {}
        for plat_id in plats.keys():
            addr = plats.get(plat_id)
            if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
                return c
    # 3) 都没有：第一个
    return candidates[0]


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="拉取币安 U 本位永续合约标的多链 ERC20 合约地址",
    )
    parser.add_argument(
        "--api-key", default=os.getenv("COINGECKO_API_KEY", ""),
        help="CoinGecko Demo API key（可选，免费版留空即可）",
    )
    parser.add_argument(
        "--out", default="binance_perp_tokens.json",
        help="输出文件路径（默认 binance_perp_tokens.json）",
    )
    parser.add_argument(
        "--bases", default="",
        help="只处理指定的 base asset 列表（逗号分隔，如 BTC,ETH,SOL），便于增量调试",
    )
    args = parser.parse_args()

    # 加载 .env 中的 COINGECKO_API_KEY
    load_dotenv()
    api_key = args.api_key or os.getenv("COINGECKO_API_KEY", "")

    # 可选：限定 base 列表（调试用）
    base_filter: Optional[set] = None
    if args.bases:
        base_filter = {b.strip().upper() for b in args.bases.split(",") if b.strip()}

    # Step 1: 币安合约列表
    print("Step 1: 拉币安 U 本位永续合约 base asset 列表...", flush=True)
    all_bases = fetch_binance_perp_bases()
    print(f"  币安 USDT 本位永续合约共 {len(all_bases)} 个", flush=True)
    if base_filter:
        bases = [b for b in all_bases if b in base_filter]
        print(f"  --bases 过滤后保留 {len(bases)} 个: {bases}", flush=True)
    else:
        bases = all_bases

    # Step 2: CoinGecko coins/list?include_platform=true（一次请求拿全部）
    print("\nStep 2: 拉 CoinGecko coins/list?include_platform=true...", flush=True)
    sym_map = fetch_coingecko_coins_with_platforms(api_key)
    print(f"  CoinGecko 共 {len(sym_map)} 个不同 symbol（含多候选）", flush=True)

    # Step 3: 匹配
    print("\nStep 3: 逐个匹配 base -> coin_id 并提取 7 链合约地址...", flush=True)
    results: List[Dict[str, Any]] = []
    matched = 0
    missed: List[Dict[str, Any]] = []

    for i, base in enumerate(bases, 1):
        candidates = sym_map.get(base.upper(), [])
        if not candidates:
            missed.append({"base": base, "reason": "no coingecko symbol match"})
            results.append({
                "base": base,
                "binance_symbol": f"{base}USDT",
                "coingecko_id": None,
                "platforms": {},
            })
            print(f"  [{i}/{len(bases)}] {base}: 无 CoinGecko symbol 匹配",
                  flush=True)
            continue

        # 多候选时挑正确的
        chosen = pick_correct_candidate(candidates)
        coin_id = chosen["coin_id"]
        platforms = chosen["platforms"] or {}

        # 只保留 7 条目标链的合约地址
        filtered: Dict[str, Dict[str, Any]] = {}
        for plat_id, chain in PLATFORM_TO_CHAIN.items():
            addr = platforms.get(plat_id)
            if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
                filtered[chain] = {
                    "contract": addr.lower(),
                    # decimals=0 占位，all_coin_alarm.py 启动时通过 RPC 读 decimals() 补全
                    "decimals": 0,
                }

        results.append({
            "base": base,
            "binance_symbol": f"{base}USDT",
            "coingecko_id": coin_id,
            "platforms": filtered,
        })
        if filtered:
            matched += 1
            chains_str = ",".join(sorted(filtered.keys()))
            print(f"  [{i}/{len(bases)}] {base} -> {coin_id} chains=[{chains_str}]",
                  flush=True)
        else:
            missed.append({
                "base": base,
                "coin_id": coin_id,
                "reason": "no target-chain contract on CoinGecko",
            })
            print(f"  [{i}/{len(bases)}] {base} -> {coin_id} (无目标链合约)",
                  flush=True)

    # 写入
    output = {
        "fetched_at": int(time.time()),
        "binance_perp_count": len(bases),
        "matched_with_contract": matched,
        "missed_count": len(missed),
        "tokens": results,
        "missed": missed,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n==== 完成 ====", flush=True)
    print(f"  币安 U 本位合约总数: {len(bases)}", flush=True)
    print(f"  在 7 条目标链上至少有一个合约地址的: {matched}", flush=True)
    print(f"  无目标链合约地址（多为非 EVM 币种）: {len(missed)}", flush=True)
    print(f"  输出文件: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
