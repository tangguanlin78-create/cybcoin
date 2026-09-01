#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
CSV 转 JSON  (csv_to_labels.py)
================================================================================

用途：
    将外部 CSV 格式的地址标签表转换为 sync_exchanges.py 可消费的扁平 JSON
    （{"0x...": "标签", ...}），或直接写为 exchanges.json 的完整格式。

设计目标 CSV 格式（兼容 DataDr69/labeled_ethereum_addresses_dataset）：
    列：,Address,Name Tag,Balance,Txn Count,Label
    示例行：
      0,0x3f5CE5FBFe3E9af3971dD833D26bA9b5C936f0bE,Binance,0.2 ETH,"17,017,385",binance

    - 必须有 Address 列（0x 开头 42 字符才保留）
    - 标签列优先级：Name Tag > Label（Name Tag 更具体，如 "Binance 14"）
    - 若 Name Tag 缺失则用 Label（如 "binance"）

同时支持更通用的 CSV：
    - 任意列名，只要包含 address / addr / wallet 之一即识别为地址列
    - 任意列名，只要包含 label / name / tag / owner 之一即识别为标签列

输入：
    - 本地 CSV 文件（可多个，会合并）
    - 远程 CSV URL（http/https，自动下载）
    - 可混合本地与远程

输出：
    - 默认输出扁平 JSON  { "0x...": "标签", ... }  —— 可作为 sync_exchanges.py 的源
    - 加 --full 参数输出完整 exchanges.json 格式
      { "_comment":..., "_updated":..., "addresses": {...} }

运行示例：
    # 本地单文件转扁平 JSON
    python csv_to_labels.py -i cex.csv -o cex_labels.json

    # 远程 DataDr69 CEX CSV 下载并转完整 exchanges.json
    python csv_to_labels.py \\
      -i https://raw.githubusercontent.com/DataDr69/labeled_ethereum_addresses_dataset/main/csv/01_cex_labels.csv \\
      -o exchanges_from_csv.json --full

    # 多个源合并
    python csv_to_labels.py -i cex.csv others.csv -o merged.json

依赖：仅标准库（csv / json / urllib），无需 pip install。
================================================================================
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

# 地址列名候选（小写包含匹配）
ADDRESS_COL_HINTS = ("address", "addr", "wallet")
# 标签列名候选（小写包含匹配）
LABEL_COL_HINTS = ("name tag", "label", "name", "tag", "owner")
# EVM 地址合法性
def _is_valid_address(s: str) -> bool:
    s = s.strip()
    return s.startswith("0x") and len(s) == 42


# ------------------------------------------------------------------
# 列识别：从表头自动定位地址列与标签列
# ------------------------------------------------------------------

def detect_columns(header: List[str]) -> Tuple[Optional[int], Optional[int]]:
    """返回 (address_col_index, label_col_index)。找不到返回 None。"""
    addr_idx: Optional[int] = None
    label_idx: Optional[int] = None

    for i, col in enumerate(header):
        col_lower = col.strip().lower()
        if addr_idx is None and any(h in col_lower for h in ADDRESS_COL_HINTS):
            addr_idx = i
        if label_idx is None and any(h in col_lower for h in LABEL_COL_HINTS):
            label_idx = i

    # 若未明确匹配，退一步：第一列若像 0x 也算地址列
    if addr_idx is None and header and _is_valid_address(header[0]):
        addr_idx = 0

    return addr_idx, label_idx


# ------------------------------------------------------------------
# CSV 读取（本地或远程）
# ------------------------------------------------------------------

def _read_text(source: str) -> str:
    """从本地路径或 HTTP URL 读取文本。"""
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(
            source, headers={"User-Agent": "erc20-csv-convert/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            # 远程可能是 bytes，按 UTF-8 解码
            data = resp.read()
            return data.decode("utf-8", errors="replace")
    # 本地文件
    with open(source, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def parse_csv(source: str) -> Dict[str, str]:
    """解析单个 CSV 源为 {小写地址: 标签}。
    标签优先级：Name Tag > Label（更具体的在前）。
    """
    text = _read_text(source)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        print(f"  [{source}] 空文件，跳过", file=sys.stderr)
        return {}

    addr_idx, label_idx = detect_columns(header)
    if addr_idx is None:
        print(f"  [{source}] 未找到地址列，跳过。表头: {header}", file=sys.stderr)
        return {}
    if label_idx is None:
        print(f"  [{source}] 未找到标签列，跳过。表头: {header}", file=sys.stderr)
        return {}

    # 收集候选标签列：所有匹配 LABEL_COL_HINTS 的列都纳入
    label_indices: List[int] = []
    for i, col in enumerate(header):
        col_lower = col.strip().lower()
        if any(h in col_lower for h in LABEL_COL_HINTS):
            label_indices.append(i)
    # 优先把 "name tag" 类放最前
    label_indices.sort(
        key=lambda i: (0 if "name tag" in header[i].strip().lower() else 1, i)
    )

    result: Dict[str, str] = {}
    skipped = 0
    for row in reader:
        if len(row) <= addr_idx:
            continue
        addr = row[addr_idx].strip()
        if not _is_valid_address(addr):
            skipped += 1
            continue
        # 取第一个非空标签列
        label = ""
        for li in label_indices:
            if li < len(row) and row[li].strip():
                label = row[li].strip()
                break
        if not label:
            continue
        addr_l = addr.lower()
        # 同地址取更长标签
        if addr_l not in result or len(label) > len(result[addr_l]):
            result[addr_l] = label

    print(f"  [{source}] 解析到 {len(result)} 个 EVM 地址"
          f"（跳过 {skipped} 个非 EVM 行）")
    return result


# ------------------------------------------------------------------
# 合并去重（与 sync_exchanges.py 同策略）
# ------------------------------------------------------------------

def merge_labels(batches: List[Tuple[str, Dict[str, str]]]) -> Dict[str, str]:
    """合并多源，同地址取更长标签。"""
    merged: Dict[str, str] = {}
    for source_name, labels in batches:
        for addr, label in labels.items():
            if addr not in merged:
                merged[addr] = label
            elif len(label) > len(merged[addr]):
                merged[addr] = label
    return merged


# ------------------------------------------------------------------
# 写出
# ------------------------------------------------------------------

def write_flat(path: str, labels: Dict[str, str]) -> None:
    """写出扁平 JSON { "0x...": "标签" }，按地址排序。"""
    sorted_labels = dict(sorted(labels.items()))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted_labels, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def write_full(path: str, labels: Dict[str, str], comment: str) -> None:
    """写出完整 exchanges.json 格式 { _comment, _updated, addresses }。"""
    sorted_labels = dict(sorted(labels.items()))
    payload = {
        "_comment": comment,
        "_updated": int(time.time()),
        "addresses": sorted_labels,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="将 CSV 地址标签表转换为 JSON（供 sync_exchanges.py 消费）")
    parser.add_argument("-i", "--inputs", nargs="+", required=True,
                        help="输入 CSV（本地路径或 HTTP URL，可多个）")
    parser.add_argument("-o", "--out", required=True,
                        help="输出 JSON 文件路径")
    parser.add_argument("--full", action="store_true",
                        help="输出完整 exchanges.json 格式（含 _comment/_updated/addresses）")
    parser.add_argument("--comment",
                        default="由 CSV 转换生成的交易所 / 机构地址标签库。",
                        help="--full 模式下写入的 _comment 字段")
    args = parser.parse_args()

    print(f"开始转换，共 {len(args.inputs)} 个输入")
    batches: List[Tuple[str, Dict[str, str]]] = []
    for src in args.inputs:
        print(f"- 处理 {src}")
        try:
            labels = parse_csv(src)
        except Exception as e:  # noqa: BLE001
            print(f"  [{src}] 处理失败: {e}", file=sys.stderr)
            continue
        if labels:
            batches.append((src, labels))

    if not batches:
        print("所有输入均失败或无数据，不生成输出", file=sys.stderr)
        return 2

    merged = merge_labels(batches)
    print(f"合并去重后共 {len(merged)} 个地址")

    if args.full:
        write_full(args.out, merged, args.comment)
    else:
        write_flat(args.out, merged)
    print(f"已写入 {args.out}（{'完整格式' if args.full else '扁平格式'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
