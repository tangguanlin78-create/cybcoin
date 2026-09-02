#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
从 binance_perp_tokens.json 选 Top N 币种 → 生成 all_coin_alarm.py 多链 config v2
================================================================================

前置：
    先跑 fetch_binance_perp_tokens.py，生成 binance_perp_tokens.json

流程：
    1. 拉币安 fapi 24h ticker，按 quoteVolume（24h 成交额，USDT）排序
    2. 读 binance_perp_tokens.json
    3. 过滤掉没有合约地址的（仅保留 matched 的币种）
    4. 按成交额降序取 Top N（默认 100）
    5. 把每个币种在 7 条链上的合约地址展开为 all_coin_alarm.py 的 tokens[] 记录
       每条记录含 chain 字段，同一币种在多链上会有多条记录
    6. 与原 config.json 的手工 tokens[] 合并（避免 contract+chain 重复）
    7. 输出 config_v2.json（结构兼容多链 all_coin_alarm.py）
    8. 输出 rpc_urls 需要填的链列表（便于用户准备 RPC URL）

为何按 24h 成交额排序而非 CoinGecko 市值：
    - 币安 U 本位合约本身的成交额更贴近"用户关心"的标的活跃度
    - 一次 HTTP 调用即可拿全，无需分页
    - 不依赖 CoinGecko 限流

输出 config_v2.json 关键字段：
    {
      "rpc_urls": {"ethereum":"", "bsc":"", ...},  // 多链 RPC，敏感字段
      "chains":   ["ethereum", "bsc", ...],         // 启用监控的链
      "tokens": [
        {"chain":"ethereum", "contract":"0x...", "decimals":0,
         "symbol":"USDT", "coingecko_id":"tether",
         "alert_usd_threshold":..., "dust_usd_threshold":...,
         "_source":"manual" | "binance_perp_top100"},
        ...
      ],
      // 其他字段同原 config.json
    }

用法：
    # 默认取 Top 100
    python generate_multi_chain_config.py

    # 取 Top 50
    python generate_multi_chain_config.py --top 50

    # 只生成以太坊一条链（先小范围试）
    python generate_multi_chain_config.py --chains ethereum

    # 不保留原 config.json 手工项
    python generate_multi_chain_config.py --no-keep-manual
================================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

BINANCE_FAPI_24H_TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"

# all_coin_alarm.py 支持的链
SUPPORTED_CHAINS = [
    "ethereum", "bsc", "polygon", "arbitrum", "optimism", "base", "avalanche"
]

# 默认阈值（用于新生成的 token 记录，可后续按需调整）
DEFAULT_ALERT_USD = 100_000
DEFAULT_DUST_USD = 1_000  # 多币种跨链监控时调高灰尘阈值，避免小币种噪音

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
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = RETRY_BACKOFF_BASE ** attempt
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
# 币安 24h ticker → base asset 成交额排序
# ------------------------------------------------------------------

def fetch_binance_volume_ranking() -> Dict[str, float]:
    """拉币安 fapi 24h ticker，返回 base_asset -> max_quote_volume_usd 映射。

    同一 base asset 在币安可能有多个合约（如 BTCUSDT / BTCUSDC 等），
    取 quoteVolume 最大值作为该 base 的成交额排序依据。
    quoteVolume 单位是 USDT（计价货币）。
    """
    resp = http_get_with_retry(BINANCE_FAPI_24H_TICKER, timeout=60)
    if resp is None or resp.status_code != 200:
        raise RuntimeError(
            f"币安 fapi 24h ticker 拉取失败: status="
            f"{getattr(resp, 'status_code', None)}"
        )
    data = resp.json()
    base_to_volume: Dict[str, float] = {}
    for t in data:
        sym = t.get("symbol", "")
        # 只看 USDT 本位永续合约（BTCUSDT / ETHUSDT ...）
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]  # 去掉 USDT 后缀
        qv = float(t.get("quoteVolume", 0) or 0)
        # 同一 base 取最大成交额
        if qv > base_to_volume.get(base, 0):
            base_to_volume[base] = qv
    return base_to_volume


# ------------------------------------------------------------------
# 加载原 config.json 手工 tokens
# ------------------------------------------------------------------

def load_manual_tokens(config_path: str) -> List[Dict[str, Any]]:
    """读原 config.json 的 tokens[]，返回手工配置项（统一加 chain 字段）。"""
    if not os.path.exists(config_path):
        return []
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    chain = cfg.get("chain", "ethereum")
    manual = []
    for t in cfg.get("tokens", []):
        if not isinstance(t, dict):
            continue
        contract = str(t.get("contract") or "").lower()
        if not contract.startswith("0x"):
            continue
        # 旧 config.json 的 tokens[] 没有 chain 字段，默认用顶层 chain
        record = {
            "chain": t.get("chain") or chain,
            "contract": contract,
            "decimals": int(t.get("decimals", 0)),
            "symbol": str(t.get("symbol", "")),
            "coingecko_id": str(t.get("coingecko_id", "")),
            "alert_usd_threshold": float(
                t.get("alert_usd_threshold", DEFAULT_ALERT_USD)),
            "dust_usd_threshold": float(
                t.get("dust_usd_threshold", DEFAULT_DUST_USD)),
            "skip_alert": bool(t.get("skip_alert", False)),
            "_source": "manual",
        }
        manual.append(record)
    return manual


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="从 binance_perp_tokens.json 选 Top N 生成多链 config v2",
    )
    parser.add_argument(
        "--in", dest="input", default="binance_perp_tokens.json",
        help="输入文件（fetch_binance_perp_tokens.py 的输出）",
    )
    parser.add_argument(
        "--config", default="config.json",
        help="原 config.json 路径（用于保留手工 tokens[]）",
    )
    parser.add_argument(
        "--out", default="config_v2.json",
        help="输出文件路径（默认 config_v2.json）",
    )
    parser.add_argument(
        "--top", type=int, default=100,
        help="按 24h 成交额取前 N 个币种（默认 100）",
    )
    parser.add_argument(
        "--chains", default="",
        help="只展开指定链（逗号分隔，如 ethereum,bsc）。默认全部 7 链",
    )
    parser.add_argument(
        "--no-keep-manual", action="store_true",
        help="不保留原 config.json 的手工 tokens[]",
    )
    parser.add_argument(
        "--alert-usd", type=float, default=DEFAULT_ALERT_USD,
        help=f"新生成 token 的默认 USD 告警阈值（默认 ${DEFAULT_ALERT_USD}）",
    )
    parser.add_argument(
        "--dust-usd", type=float, default=DEFAULT_DUST_USD,
        help=f"新生成 token 的默认灰尘阈值（默认 ${DEFAULT_DUST_USD}）",
    )
    args = parser.parse_args()

    # 链过滤
    chain_filter: Optional[set] = None
    if args.chains:
        chain_filter = {c.strip().lower() for c in args.chains.split(",") if c.strip()}
        invalid = chain_filter - set(SUPPORTED_CHAINS)
        if invalid:
            print(f"[ERROR] 不支持的链: {invalid}，可选: {SUPPORTED_CHAINS}")
            return 1

    # Step 1: 拉 24h ticker 排序
    print("Step 1: 拉币安 fapi 24h ticker...", flush=True)
    base_volume = fetch_binance_volume_ranking()
    print(f"  共 {len(base_volume)} 个 base asset 有成交数据", flush=True)

    # Step 2: 读 binance_perp_tokens.json
    print(f"\nStep 2: 读 {args.input}...", flush=True)
    if not os.path.exists(args.input):
        print(f"[ERROR] 输入文件不存在: {args.input}")
        print("        请先跑: python fetch_binance_perp_tokens.py")
        return 1
    with open(args.input, "r", encoding="utf-8") as f:
        perp_data = json.load(f)
    perp_tokens = perp_data.get("tokens", [])
    matched_count = sum(1 for t in perp_tokens if t.get("platforms"))
    print(f"  共 {len(perp_tokens)} 个币种，其中 {matched_count} 个有目标链合约地址",
          flush=True)

    # Step 3: 过滤 + 按成交额排序 + 取 Top N
    print(f"\nStep 3: 过滤有合约地址的 + 按 24h 成交额排序 + 取 Top {args.top}...",
          flush=True)
    candidates = []
    for t in perp_tokens:
        platforms = t.get("platforms") or {}
        if not platforms:
            continue
        base = t["base"]
        vol = base_volume.get(base, 0)
        candidates.append({
            "base": base,
            "binance_symbol": t.get("binance_symbol", f"{base}USDT"),
            "coingecko_id": t.get("coingecko_id"),
            "platforms": platforms,
            "volume_24h_usd": vol,
        })
    # 按成交额降序
    candidates.sort(key=lambda x: x["volume_24h_usd"], reverse=True)
    top_n = candidates[: args.top]
    print(f"  Top {len(top_n)} 币种（含 0 成交额的也保留排序在后）", flush=True)

    # Step 4: 展开多链合约为 all_coin_alarm.py tokens[]
    print("\nStep 4: 展开多链合约为 tokens[] 记录...", flush=True)
    new_tokens: List[Dict[str, Any]] = []
    chain_usage: Dict[str, int] = {c: 0 for c in SUPPORTED_CHAINS}
    for c in top_n:
        platforms = c["platforms"]
        for chain, info in platforms.items():
            if chain_filter and chain not in chain_filter:
                continue
            if chain not in SUPPORTED_CHAINS:
                continue
            contract = str(info.get("contract", "")).lower()
            if not contract.startswith("0x") or len(contract) != 42:
                continue
            new_tokens.append({
                "chain": chain,
                "contract": contract,
                "decimals": int(info.get("decimals", 0)),
                "symbol": c["base"],  # 用 base 作为 symbol，all_coin_alarm.py 会读
                "coingecko_id": c["coingecko_id"] or "",
                "alert_usd_threshold": args.alert_usd,
                "dust_usd_threshold": args.dust_usd,
                "_source": "binance_perp_top",
                "_binance_symbol": c["binance_symbol"],
                "_volume_24h_usd": c["volume_24h_usd"],
            })
            chain_usage[chain] += 1

    print(f"  展开后共 {len(new_tokens)} 条 token 记录", flush=True)
    print(f"  按链分布:", flush=True)
    for chain, n in chain_usage.items():
        if n > 0:
            print(f"    {chain}: {n} 个合约", flush=True)

    # Step 5: 合并手工 tokens[]（去重：chain + contract）
    print("\nStep 5: 合并原 config.json 的手工 tokens[]...", flush=True)
    manual_tokens = [] if args.no_keep_manual else load_manual_tokens(args.config)
    print(f"  原 config.json 有 {len(manual_tokens)} 条手工记录", flush=True)

    # 用 (chain, contract) 做去重 key。手工优先（覆盖自动生成的）
    seen = set()
    merged: List[Dict[str, Any]] = []
    for t in manual_tokens:
        key = (t["chain"], t["contract"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(t)
    for t in new_tokens:
        key = (t["chain"], t["contract"])
        if key in seen:
            # 手工项已包含，跳过自动生成的
            continue
        seen.add(key)
        merged.append(t)
    print(f"  合并后共 {len(merged)} 条 token 记录", flush=True)

    # Step 6: 生成 config_v2.json
    print(f"\nStep 6: 生成 {args.out}...", flush=True)

    # 启用的链 = 出现在 tokens[] 中的链
    active_chains = sorted({t["chain"] for t in merged})
    # 保留原 config.json 其他字段（confirmations/poll_interval 等）
    other_cfg = {}
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            other_cfg = json.load(f)
    # 删除旧的 rpc_url / chain / tokens，避免与新结构混淆
    for k in ("rpc_url", "chain", "tokens", "token_contract",
              "token_decimals", "token_symbol", "coingecko_id"):
        other_cfg.pop(k, None)

    rpc_urls = {chain: "" for chain in SUPPORTED_CHAINS}

    config_v2 = {
        "_comment_rpc_urls": (
            "多链 RPC 节点 URL，每条链一个只读节点。敏感字段，"
            "推荐放 .env 的 RPC_URL_ETHEREUM / RPC_URL_BSC / ... "
            "（环境变量名格式：RPC_URL_<大写链名>）。"
            "只填实际启用的链即可。"
        ),
        "rpc_urls": rpc_urls,
        "_comment_chains": "本次实际监控的链列表（按需删减）。rpc_urls 中对应链必须填 URL。",
        "chains": active_chains,
        "_comment_tokens": (
            "多链 tokens[] 数组。每条记录含 chain 字段。"
            "同一币种在多链上会有多条记录。"
            "_source=manual 来自原 config.json；_source=binance_perp_top 来自自动抓取。"
            "decimals=0 的，all_coin_alarm.py 启动时通过 RPC 读 decimals() 自动补全。"
            "建议人工审核 _source=binance_perp_top 的项，"
            "CoinGecko symbol 不唯一，可能有错配（如 UNI/XRP/ETH/OP/SOL）。"
        ),
        "tokens": merged,
        # 其他字段沿用原 config.json
        **other_cfg,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(config_v2, f, ensure_ascii=False, indent=2)

    print(f"  输出: {args.out}", flush=True)
    print(f"  tokens[]: {len(merged)} 条", flush=True)
    print(f"  实际启用的链: {active_chains}", flush=True)
    print(f"\n==== 下一步 ====", flush=True)
    print(f"  1. 编辑 {args.out}，把 rpc_urls 里启用的链填上 RPC URL", flush=True)
    print(f"     （或填到 .env 的 RPC_URL_ETHEREUM / RPC_URL_BSC / ...）", flush=True)
    print(f"  2. 人工抽检 tokens[] 中 _source=binance_perp_top 的项，", flush=True)
    print(f"     特别是 UNI / XRP / ETH / OP / SOL / F 这种已知错配", flush=True)
    print(f"  3. 改造后的 all_coin_alarm.py 支持新 config_v2.json 格式（向后兼容旧 config.json）", flush=True)
    print(f"  4. 启动: python all_coin_alarm.py config_v2.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
