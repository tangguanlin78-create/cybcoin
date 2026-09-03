"""飞书自定义机器人 Webhook 推送：大额转账与 Top10 持有人。

复用项目 Smart_Wallet/monitor/feishu.py 的签名算法与富文本消息构造。
文档: https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class FeishuBot:
    def __init__(self, webhook: str, secret: str = "", timeout: int = 10):
        self.webhook = webhook
        self.secret = secret.strip()
        self.timeout = timeout
        self._session = requests.Session()

    def _sign(self, timestamp: int) -> str:
        msg = f"{timestamp}\n{self.secret}".encode("utf-8")
        digest = hmac.new(msg, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _build_url(self) -> str:
        if not self.secret:
            return self.webhook
        ts = int(time.time())
        sign = self._sign(ts)
        sep = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{sep}timestamp={ts}&sign={sign}"

    def send(self, payload: dict) -> bool:
        url = self._build_url()
        try:
            resp = self._session.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error("飞书推送网络错误: %s", e)
            return False
        except ValueError:
            logger.error("飞书返回非 JSON: %s", resp.text[:200])
            return False

        if data.get("code", 0) != 0 or data.get("StatusCode", 0) != 0:
            logger.error("飞书推送失败: %s", data)
            return False
        return True

    def send_post(self, title: str, content_lines: list) -> bool:
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": content_lines,
                    }
                }
            },
        }
        return self.send(payload)


def _line(*items: dict) -> list:
    return list(items)


def _text(t: str, bold: bool = False) -> dict:
    if bold:
        t = f"【{t}】"
    return {"tag": "text", "text": t}


def _a(text: str, href: str) -> dict:
    return {"tag": "a", "text": text, "href": href}


def _push_time() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _short_addr(addr: str) -> str:
    if len(addr) > 16:
        return f"{addr[:8]}...{addr[-6:]}"
    return addr


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"${v:,.2f}"


def _fmt_amount(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}"


def _pct(share: Optional[float]) -> str:
    if share is None:
        return "N/A"
    return f"{share * 100:.2f}%"


def build_whale_message(chain_label: str,
                        symbol: str,
                        token_addr: str,
                        explorer_address_base: str,
                        large_transfers: list,
                        top_holders: list,
                        threshold_usd: float,
                        window_hours: int,
                        total_supply: Optional[float] = None,
                        token_price: Optional[float] = None) -> tuple:
    """构造大额转账 + Top10 持有人富文本消息。

    Args:
        chain_label: 链名（"以太坊 ETH"）
        symbol: 代币符号
        token_addr: 代币合约地址
        explorer_address_base: 浏览器地址 URL 前缀
        large_transfers: 大额转账列表，每项含
            {hash, from, to, value, value_usd, timestamp, from_tag, to_tag}
        top_holders: Top10 持有人列表，每项含
            {address, balance, share, label, type}
        threshold_usd: 大额阈值 USD
        window_hours: 查询窗口小时
        total_supply: 代币总供应量（人类可读）
        token_price: 代币单价 USD
    Returns:
        (title, content_lines)
    """
    title = f"🐋 {chain_label} 大额转账 | {symbol} | {len(large_transfers)}笔大额 | Top10 已聚合"

    addr_short = _short_addr(token_addr)
    lines = [
        _line(_text("━━━ 代币概览 ━━━", bold=True)),
        _line(_text(f"链: {chain_label}    符号: "), _text(symbol, bold=True)),
        _line(_text("合约: "), _a(addr_short, f"{explorer_address_base}{token_addr}")),
        _line(_text("单价: "), _text(_fmt_usd(token_price), bold=True),
              _text("    总供应: "), _text(_fmt_amount(total_supply) if total_supply else "N/A")),
        _line(_text("大额阈值: "), _text(_fmt_usd(threshold_usd), bold=True),
              _text(f"    统计窗口: 近 {window_hours} 小时")),
        _line(_text("推送时间: "), _text(_push_time(), bold=True)),
    ]

    # 大额转账（最多展示 8 笔）
    lines.append(_line(_text("━━━ 大额转账记录 ━━━", bold=True)))
    if not large_transfers:
        lines.append(_line(_text("窗口内无超过阈值的大额转账")))
    else:
        show = large_transfers[:8]
        for i, t in enumerate(show, 1):
            ts = t.get("timestamp", "")
            val = _fmt_usd(t.get("value_usd"))
            amt = _fmt_amount(t.get("value"))
            from_tag = t.get("from_tag") or "未知"
            to_tag = t.get("to_tag") or "未知"
            from_addr = _short_addr(t.get("from", ""))
            to_addr = _short_addr(t.get("to", ""))
            tx_short = t.get("hash", "")[:10] + "..."
            lines.append(_line(_text(f"{i}. {ts}"), _text(f"  {val}", bold=True),
                               _text(f" ({amt} {symbol})")))
            lines.append(_line(_text(f"   from: {from_addr} [{from_tag}]"),
                               _text("  to: "), _text(f"{to_addr} [{to_tag}]")))
            if t.get("hash"):
                lines.append(_line(_text("   tx: "), _a(tx_short, t.get("hash", ""))))
        if len(large_transfers) > 8:
            lines.append(_line(_text(f"   ...还有 {len(large_transfers) - 8} 笔，详见 GUI/日志")))

    # Top10 持有人占比
    lines.append(_line(_text("━━━ Top10 持有人占比 ━━━", bold=True)))
    if not top_holders:
        lines.append(_line(_text("未能获取持有人列表（需 Blockscout/Etherscan Pro 支持）")))
    else:
        agg = sum(h.get("share", 0) for h in top_holders)
        for i, h in enumerate(top_holders, 1):
            addr = h.get("address", "")
            label = h.get("label") or "未知"
            share = _pct(h.get("share"))
            bal = _fmt_amount(h.get("balance"))
            lines.append(_line(
                _text(f"{i:>2}. "), _text(share, bold=True),
                _text(f"  {_short_addr(addr)}"),
                _text(f"  [{label}]"),
                _text(f"  {bal} {symbol}"),
            ))
        lines.append(_line(_text("Top10 合计占比: "), _text(_pct(agg), bold=True)))

    return title, lines


def push_whale_report(bot: FeishuBot, **kwargs) -> bool:
    title, lines = build_whale_message(**kwargs)
    ok = bot.send_post(title, lines)
    if ok:
        logger.info("飞书推送成功: %s", title)
    else:
        logger.error("飞书推送失败: %s", title)
    return ok
