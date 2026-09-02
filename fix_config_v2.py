#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_v2.json 错配 token 修正脚本
================================================================================
用途：
    CoinGecko coins/list 的 symbol 字段不唯一，fetch_binance_perp_tokens.py 自动
    匹配时会有几个错配（如 UNI 选了 unicorn-token，XRP 选了 harrypotterobamapacman8inu）。
    本脚本一次性删掉明显错配的 base，并在 _corrections 字段记录修正历史，
    便于事后审计/回滚。

行为：
    - 删除 WRONG_BASES 集合中所有 base 的 token 记录
    - 在 _corrections 字段写明：删除原因 / 删除前后的 token 数 / 保留的桥接版列表
    - 保留其他 token（包括桥接版，因为监控桥接版大额转账反映跨链资金流）

用法：
    python fix_config_v2.py [--in config_v2.json]
================================================================================
"""
import argparse
import json
import os
from datetime import date

# 必删：CoinGecko symbol 重名 + 选错的，或主网币但选成山寨
WRONG_BASES = {
    "BTC":    "错配 big-tom-coin 山寨，BTC 在以太坊真身是 WBTC（应单独加 WBTC）",
    "ETH":    "ETH 是以太坊 native gas，不是 ERC20，无 Transfer 事件可监听",
    "XRP":    "错配 harrypotterobamapacman8inu 山寨 meme 币",
    "OP":     "错配 one-path，OP 是 optimism 链 native 不是 ERC20",
    "UNI":    "错配 unicorn-token，真 uniswap 合约地址 0x1f9840a85d5aF5bf1D176683a0B0584c1B4c7E15 应单独加",
    "DOGE":   "错配 department-of-government-efficiency meme 币，DOGE 主网无 ERC20",
    "HYPE":   "错配 hyperbolic-protocol，HYPE 是 Hyperliquid 链 native",
    "LIT":    "错配 lighter，真 LIT 是 litentry（应单独加 0x...）",
    "PUMP":   "错配 big-pump，PUMP 是 pump.fun 在 Solana 上的",
    "TUT":    "错配 tutorial，显然是教程占位币",
    "XPL":    "错配 pulse-2，XPL 在币安是别的什么",
    "DASH":   "错配 dash-2，DASH 主网币不是 ERC20",
    "GRAM":   "错配 the-open-network (TON)，GRAM 与 TON 不同",
}

# 保留但标注为桥接/wrapped 版本（监控它们的大额转账仍有意义：反映跨链资金流）
BRIDGED_BASES = {
    "ADA":   "binance-peg-cardano",
    "AVAX":  "avalanche-wormhole",
    "BCH":   "binance-peg-bitcoin-cash",
    "ATOM":  "cosmos (bsc binance-peg)",
    "DOT":   "binance-peg-polkadot",
    "FIL":   "binance-peg-filecoin",
    "ICP":   "internet-computer (ethereum 桥接)",
    "LTC":   "binance-peg-litecoin",
    "NEAR":  "rainbow-bridged-near-ethereum",
    "SEI":   "layerzero-bridged-sei",
    "SOL":   "base-bridged-sol-base",
    "TRX":   "tron-bsc (桥接)",
    "ZEC":   "binance-peg-zcash-token",
    "TAO":   "bittensor (base 桥接)",
    "WIF":   "wif-on-eth (以太坊 wrapped)",
    "BNB":   "binancecoin (以太坊上的 ERC20 BNB 旧版)",
    "JUP":   "jupiter (可能是同名歧义，需人工确认)",
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="input", default="config_v2.json")
    args = p.parse_args()

    if not os.path.exists(args.input):
        print(f"[ERROR] 文件不存在: {args.input}")
        return 1

    with open(args.input, encoding="utf-8") as f:
        cfg = json.load(f)

    before = len(cfg["tokens"])
    removed = []
    kept = []
    for t in cfg["tokens"]:
        sym = t.get("symbol", "")
        if sym in WRONG_BASES:
            removed.append({
                "symbol": sym,
                "chain": t["chain"],
                "contract": t["contract"],
                "coingecko_id": t.get("coingecko_id", ""),
                "reason": WRONG_BASES[sym],
            })
        else:
            kept.append(t)

    cfg["tokens"] = kept
    after = len(cfg["tokens"])

    cfg["_corrections"] = {
        "corrected_at": str(date.today()),
        "before_token_count": before,
        "after_token_count": after,
        "removed_wrong_match": removed,
        "removed_reason": (
            "CoinGecko symbol 不唯一，自动匹配选错 coin_id。"
            "详见 fetch_binance_perp_tokens.py 的输出日志。"
            "如需补回正确的（如 UNI -> uniswap 真合约），应单独手工添加。"
        ),
        "bridged_tokens_kept": [
            {"symbol": s, "note": n} for s, n in sorted(BRIDGED_BASES.items())
        ],
        "bridged_note": (
            "这些是其他主链 native 币在 EVM 链上的桥接/wrapped 版本。"
            "监控它们的大额转账仍有意义（反映跨链资金流），"
            "但请注意告警的是桥接版而不是主网币本身。"
        ),
    }

    with open(args.input, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    print(f"修正完成: {before} -> {after} (删除 {before - after} 条错配)\n")

    print("已删除的 base:")
    for r in removed:
        print(f'  - {r["symbol"]:6s} @ {r["chain"]:10s} '
              f'coin_id={r["coingecko_id"]}')
    print()

    print(f"保留的桥接版 base（共 {len(BRIDGED_BASES)} 个，"
          f"详见 _corrections.bridged_tokens_kept）:")
    for s, n in sorted(BRIDGED_BASES.items()):
        print(f"  - {s:6s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
