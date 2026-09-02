#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
交易所地址标签库 CI 同步脚本  (sync_exchanges.py)
================================================================================

用途：
    从多个外部源定期拉取交易所 / 机构热钱包地址标签，合并去重后写回
    exchanges.json，供 all_coin_alarm.py 使用。设计为在 GitHub Actions 中每日运行，
    自动 commit 推送更新；也可本地手动运行。

设计原则：
    - 独立脚本，依赖仅 requests（CI 免装复杂环境）
    - 源列表可配置（sources.json 或环境变量），失败源跳过不影响整体
    - 合并去重：同一地址以更具体的标签为准
    - 原子写入 exchanges.json，避免中途崩溃损坏文件
    - 不修改 exchanges.json 中的 _comment 字段（保留人工说明）

源数据格式兼容：
    每个源返回 JSON，自动识别三种结构（与 all_coin_alarm.py 的解析器一致）：
      1) {"addresses": {"0x...": "Binance 14", ...}}
      2) {"0x...": "Binance 14", ...}
      3) [{"address":"0x...", "label":"Binance"}, ...]   # label 也接受 name/tag/owner

运行：
    python sync_exchanges.py                      # 用 sources.json
    python sync_exchanges.py --sources s.json --out exchanges.json
    SOURCES_FILE=s.json OUT_FILE=e.json python sync_exchanges.py

CI（见 .github/workflows/sync-exchanges.yml）：
    每日 UTC 02:00 自动运行，有变更则 commit 推回 main 分支。

数据源说明：
    公开交易所地址标签源较分散且许可证各异，使用前请确认：
    - GitHub 社区维护仓库（raw JSON URL）
    - Etherscan Label Cloud 导出（需自行整理为 JSON）
    - whale-alert / Chainalysis 等商业数据（多为付费，需 API key）
    - CSV 数据集（如 DataDr69/labeled_ethereum_addresses_dataset）——
      先用 csv_to_labels.py 转为 JSON，再作为 sync_exchanges.py 的源
    默认 sources.example.json 内置已验证可用的社区源（VHRanger/tether，MIT 许可），
    可在此基础上增删。
================================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests  # 首选，CI 中已 pip install
except ImportError:  # 本地无 requests 时退化为标准库 urllib（功能等价，仅无连接池）
    requests = None  # type: ignore[assignment]

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

HTTP_TIMEOUT = 30           # 单源拉取超时
HTTP_MAX_RETRIES = 3        # 单源重试次数
HTTP_BACKOFF_BASE = 2.0     # 指数退避基数

# 默认文件名常量（避免在多处硬编码默认值）
DEFAULT_SOURCES_FILE = "sources.json"
DEFAULT_OUT_FILE = "exchanges.json"


def _is_valid_address(s: Any) -> bool:
    s = str(s)
    return s.startswith("0x") and len(s) == 42


# ------------------------------------------------------------------
# JSON 解析（与 all_coin_alarm.py 保持一致，兼容三种格式）
# ------------------------------------------------------------------

def parse_labels(data: Any) -> Dict[str, str]:
    """从已解析的 JSON 提取 {小写地址: 标签}。
    兼容：
      1) {"addresses": {"0x...": "..."}}
      2) {"0x...": "..."}
      3) [{"address":"0x...", "label":"..."}]  # label 也接受 name/tag/owner
    """
    result: Dict[str, str] = {}

    if isinstance(data, dict) and "addresses" in data and isinstance(data["addresses"], dict):
        addrs = data["addresses"]
        for k, v in addrs.items():
            if _is_valid_address(k):
                result[str(k).lower()] = str(v)
    elif isinstance(data, dict):
        for k, v in data.items():
            # 跳过明显的元数据字段（_comment / meta / count 等）
            if k.startswith("_") or k in ("meta", "count", "source", "updated"):
                continue
            if _is_valid_address(k):
                result[str(k).lower()] = str(v)
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            addr = item.get("address") or item.get("addr")
            label = (item.get("label") or item.get("name")
                     or item.get("tag") or item.get("owner"))
            if addr and label and _is_valid_address(addr):
                result[str(addr).lower()] = str(label)

    return result


# ------------------------------------------------------------------
# HTTP 拉取（带指数退避重试）
# ------------------------------------------------------------------

def fetch_url(url: str) -> Optional[Any]:
    """拉取并解析 JSON。失败返回 None。

    支持两种来源：
      - http(s)://  —— HTTP GET 拉取（带退避重试）
      - file:// 或本地路径 —— 直接读文件（用于 CI 中先 csv_to_labels.py 生成的本地 JSON）
    """
    # ---- 本地文件 ----
    if url.startswith("file://") or os.path.exists(url):
        local_path = url[7:] if url.startswith("file://") else url
        try:
            with open(local_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  [{url}] 本地文件读取失败: {e}", file=sys.stderr)
            return None

    # ---- 远程 HTTP（优先 requests，无则退化为 urllib） ----
    if requests is not None:
        return _fetch_http_requests(url)
    return _fetch_http_urllib(url)


def _fetch_http_requests(url: str) -> Optional[Any]:
    """用 requests 拉取远程 JSON（带退避重试）。"""
    headers = {"accept": "application/json",
               "user-agent": "erc20-monitor-sync/1.0"}
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = HTTP_BACKOFF_BASE ** attempt
                print(f"  [{url}] HTTP {resp.status_code}，{wait:.0f}s 后重试 "
                      f"({attempt}/{HTTP_MAX_RETRIES})", file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"  [{url}] HTTP {resp.status_code}，跳过", file=sys.stderr)
                return None
            try:
                return resp.json()
            except ValueError:
                print(f"  [{url}] 返回非 JSON，跳过", file=sys.stderr)
                return None
        except requests.RequestException as e:  # type: ignore[union-attr]
            wait = HTTP_BACKOFF_BASE ** attempt
            print(f"  [{url}] 网络异常 {e}，{wait:.0f}s 后重试 "
                  f"({attempt}/{HTTP_MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait)
    print(f"  [{url}] 多次重试失败，跳过", file=sys.stderr)
    return None


def _fetch_http_urllib(url: str) -> Optional[Any]:
    """无 requests 时的标准库回退实现（功能等价，无连接池）。"""
    import urllib.request
    import urllib.error
    headers = {"Accept": "application/json",
               "User-Agent": "erc20-monitor-sync/1.0"}
    for attempt in range(1, HTTP_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
                status = resp.status
                if status == 429 or status >= 500:
                    wait = HTTP_BACKOFF_BASE ** attempt
                    print(f"  [{url}] HTTP {status}，{wait:.0f}s 后重试 "
                          f"({attempt}/{HTTP_MAX_RETRIES})", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if status != 200:
                    print(f"  [{url}] HTTP {status}，跳过", file=sys.stderr)
                    return None
                data = resp.read()
                try:
                    return json.loads(data.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, ValueError):
                    print(f"  [{url}] 返回非 JSON，跳过", file=sys.stderr)
                    return None
        except (urllib.error.URLError, OSError) as e:
            wait = HTTP_BACKOFF_BASE ** attempt
            print(f"  [{url}] 网络异常 {e}，{wait:.0f}s 后重试 "
                  f"({attempt}/{HTTP_MAX_RETRIES})", file=sys.stderr)
            time.sleep(wait)
    print(f"  [{url}] 多次重试失败，跳过", file=sys.stderr)
    return None


# ------------------------------------------------------------------
# 合并去重
# ------------------------------------------------------------------

def merge_labels(batches: List[Tuple[str, Dict[str, str]]]) -> Dict[str, str]:
    """合并多个源的标签。
    冲突策略：同一地址取标签更长的一方（通常更具体，如 "Binance 14" > "Binance"）。
    """
    merged: Dict[str, str] = {}
    for source_name, labels in batches:
        for addr, label in labels.items():
            if addr not in merged:
                merged[addr] = label
            else:
                # 取更长标签；等长则保留先到的（稳定）
                if len(label) > len(merged[addr]):
                    merged[addr] = label
    return merged


# ------------------------------------------------------------------
# 源加载
# ------------------------------------------------------------------

def load_sources(path: str) -> List[Dict[str, str]]:
    """加载源列表。结构：
        [
          {"name": "Matrix-Labs", "url": "https://..."},
          ...
        ]
    也接受纯字符串列表 ["url1", "url2"]。
    url 既可以是 http(s) 远程地址，也可以是本地文件路径（fetch_url 会自动识别）。
    """
    if not os.path.exists(path):
        print(f"源文件 {path} 不存在", file=sys.stderr)
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    sources: List[Dict[str, str]] = []
    if isinstance(data, dict):
        data = data.get("sources", [])
    if not isinstance(data, list):
        print(f"源文件 {path} 格式错误：应为列表", file=sys.stderr)
        return []
    for item in data:
        if isinstance(item, str):
            sources.append({"name": item, "url": item})
        elif isinstance(item, dict) and item.get("url"):
            sources.append({
                "name": item.get("name") or item["url"],
                "url": item["url"],
            })
    return sources


# ------------------------------------------------------------------
# 写出 exchanges.json
# ------------------------------------------------------------------

def write_exchanges(path: str, labels: Dict[str, str], comment: str) -> None:
    """原子写入 exchanges.json，保留 _comment 字段。"""
    # 按地址排序，便于 diff 审阅
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
        description="从外部源同步交易所地址标签到 exchanges.json")
    parser.add_argument("--sources", default=os.getenv("SOURCES_FILE", DEFAULT_SOURCES_FILE),
                        help=f"源列表 JSON 文件（默认 {DEFAULT_SOURCES_FILE}）")
    parser.add_argument("--out", default=os.getenv("OUT_FILE", DEFAULT_OUT_FILE),
                        help=f"输出文件路径（默认 {DEFAULT_OUT_FILE}）")
    parser.add_argument("--comment",
                        default="交易所 / 机构已知热钱包地址标签库。由 sync_exchanges.py 自动维护。",
                        help="写入文件的 _comment 字段")
    args = parser.parse_args()

    sources = load_sources(args.sources)
    if not sources:
        print(f"未配置任何源（{args.sources} 为空或不存在），退出", file=sys.stderr)
        return 1

    print(f"开始同步，共 {len(sources)} 个源")
    batches: List[Tuple[str, Dict[str, str]]] = []
    for src in sources:
        print(f"- 拉取 [{src['name']}] {src['url']}")
        data = fetch_url(src["url"])
        if data is None:
            continue
        labels = parse_labels(data)
        print(f"  解析到 {len(labels)} 个地址")
        if labels:
            batches.append((src["name"], labels))

    if not batches:
        print("所有源均失败或无数据，不更新 exchanges.json", file=sys.stderr)
        return 2

    merged = merge_labels(batches)
    print(f"合并去重后共 {len(merged)} 个地址")

    # 读取现有文件做 diff，便于日志
    old_count = 0
    if os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as f:
                old = json.load(f)
            old_count = len(old.get("addresses", {}))
        except (json.JSONDecodeError, OSError):
            pass

    write_exchanges(args.out, merged, args.comment)
    delta = len(merged) - old_count
    print(f"已写入 {args.out}: {old_count} -> {len(merged)} (Δ {delta:+d})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
