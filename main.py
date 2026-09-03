"""加密货币大额转账查询 - Tkinter 桌面 GUI。

运行：python main.py
依赖：requests, PyYAML，以及 Python 标准库 tkinter（Windows 官方安装包自带）

功能：
  1. 输入链 + 代币符号（或 0x 合约地址）+ 大额阈值 + 窗口，查询大额转账
  2. 聚合 Top10 持有人占比并打标签
  3. 一键推送结果到飞书自定义机器人
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

# 确保本地模块可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from chains import (  # noqa: E402
    get_chain, get_query_defaults, list_chains, load_config,
    resolve_token_address, setup_logging,
)
from data_source import DataSource, Holder, TokenInfo, Transfer  # noqa: E402
from feishu import FeishuBot, push_whale_report  # noqa: E402

logger = logging.getLogger(__name__)


def _short_addr(addr: str) -> str:
    if len(addr) > 16:
        return f"{addr[:8]}...{addr[-6:]}"
    return addr or "-"


def _fmt_usd(v) -> str:
    if v is None:
        return "N/A"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_amount(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.6f}"


def _fmt_pct(share) -> str:
    try:
        return f"{float(share) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


class WhaleAlertApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("加密货币大额转账查询")
        self.geometry("1180x760")
        self.minsize(960, 600)

        self.config = load_config()
        setup_logging(self.config)
        self.qcfg = get_query_defaults(self.config)
        self.feishu_cfg = self.config.get("feishu") or {}

        self._last_result: dict | None = None   # 供飞书推送复用
        self._query_thread: threading.Thread | None = None

        self._build_input_panel()
        self._build_results_panel()
        self._build_status_bar()
        self._populate_chains()

        self.token_addr_var = tk.StringVar()

    # ----------------- UI 构建 -----------------

    def _build_input_panel(self):
        panel = ttk.LabelFrame(self, text="查询条件", padding=10)
        panel.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(10, 6))

        self.chain_var = tk.StringVar()
        self.symbol_var = tk.StringVar()
        self.threshold_var = tk.StringVar(value=str(self.qcfg["threshold_usd"]))
        self.window_var = tk.StringVar(value=str(self.qcfg["window_hours"]))

        ttk.Label(panel, text="链:").grid(row=0, column=0, sticky=tk.W, padx=4)
        self.chain_combo = ttk.Combobox(
            panel, textvariable=self.chain_var, state="readonly", width=22)
        self.chain_combo.grid(row=0, column=1, sticky=tk.W, padx=4)

        ttk.Label(panel, text="代币符号:").grid(row=0, column=2, sticky=tk.W, padx=4)
        ttk.Entry(panel, textvariable=self.symbol_var, width=20).grid(
            row=0, column=3, sticky=tk.W, padx=4)
        ttk.Label(panel, text="(如 USDT；或填 0x 合约地址)",
                  foreground="gray").grid(row=0, column=4, sticky=tk.W, padx=4)

        ttk.Label(panel, text="大额阈值(USD):").grid(
            row=1, column=0, sticky=tk.W, padx=4, pady=(8, 0))
        ttk.Entry(panel, textvariable=self.threshold_var, width=12).grid(
            row=1, column=1, sticky=tk.W, padx=4, pady=(8, 0))

        ttk.Label(panel, text="查询窗口(小时):").grid(
            row=1, column=2, sticky=tk.W, padx=4, pady=(8, 0))
        ttk.Entry(panel, textvariable=self.window_var, width=12).grid(
            row=1, column=3, sticky=tk.W, padx=4, pady=(8, 0))

        self.query_btn = ttk.Button(panel, text="查询", command=self._on_query)
        self.query_btn.grid(row=1, column=5, sticky=tk.W, padx=10, pady=(8, 0))

        self.push_btn = ttk.Button(panel, text="推送到飞书",
                                   command=self._on_push_feishu, state=tk.DISABLED)
        self.push_btn.grid(row=1, column=6, sticky=tk.W, padx=4, pady=(8, 0))

        panel.columnconfigure(7, weight=1)

    def _build_results_panel(self):
        panel = ttk.Frame(self)
        panel.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=6)

        nb = ttk.Notebook(panel)
        nb.pack(fill=tk.BOTH, expand=True)

        # 1) 大额转账
        t1 = ttk.Frame(nb)
        nb.add(t1, text="大额转账记录")
        cols_t = ("ts", "from", "from_tag", "to", "to_tag", "amount", "usd", "tx")
        self.transfers_tree = ttk.Treeview(
            t1, columns=cols_t, show="headings", selectmode="browse")
        headers = {
            "ts": "时间", "from": "转出地址", "from_tag": "转出标签",
            "to": "转入地址", "to_tag": "转入标签",
            "amount": "数量", "usd": "USD价值", "tx": "Tx",
        }
        widths = {"ts": 150, "from": 160, "from_tag": 110, "to": 160,
                  "to_tag": 110, "amount": 140, "usd": 130, "tx": 110}
        for c in cols_t:
            self.transfers_tree.heading(c, text=headers[c])
            self.transfers_tree.column(c, width=widths[c], anchor=tk.W)
        self.transfers_tree.pack(fill=tk.BOTH, expand=True)
        vsb1 = ttk.Scrollbar(t1, orient="vertical",
                             command=self.transfers_tree.yview)
        self.transfers_tree.configure(yscrollcommand=vsb1.set)
        vsb1.pack(side=tk.RIGHT, fill=tk.Y)

        # 2) Top10 持有人
        t2 = ttk.Frame(nb)
        nb.add(t2, text="Top10 持有人占比")
        cols_h = ("rank", "addr", "label", "type", "balance", "share")
        self.holders_tree = ttk.Treeview(
            t2, columns=cols_h, show="headings", selectmode="browse")
        hheaders = {"rank": "#", "addr": "地址", "label": "归属标签",
                    "type": "类型", "balance": "持仓数量", "share": "占比"}
        hwidths = {"rank": 40, "addr": 320, "label": 180, "type": 100,
                   "balance": 200, "share": 120}
        for c in cols_h:
            self.holders_tree.heading(c, text=hheaders[c])
            self.holders_tree.column(c, width=hwidths[c], anchor=tk.W)
        self.holders_tree.pack(fill=tk.BOTH, expand=True)
        vsb2 = ttk.Scrollbar(t2, orient="vertical", command=self.holders_tree.yview)
        self.holders_tree.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side=tk.RIGHT, fill=tk.Y)

        # 3) 代币概览
        t3 = ttk.Frame(nb)
        nb.add(t3, text="代币概览")
        self.token_info_text = tk.Text(t3, height=10, wrap=tk.NONE)
        self.token_info_text.pack(fill=tk.BOTH, expand=True)

    def _build_status_bar(self):
        self.status_var = tk.StringVar(value="就绪")
        bar = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN,
                        anchor=tk.W, padding=(6, 2))
        bar.pack(side=tk.BOTTOM, fill=tk.X)

    # ----------------- 数据填充 -----------------

    def _populate_chains(self):
        chains = list_chains(self.config)
        self._chain_map = {label: cid for cid, label in chains}
        labels = [label for _, label in chains]
        if not labels:
            labels = ["(未配置)"]
            self.chain_combo["values"] = labels
            self.chain_var.set(labels[0])
            return
        self.chain_combo["values"] = labels
        self.chain_var.set(labels[0])

    # ----------------- 查询 -----------------

    def _on_query(self):
        if self._query_thread and self._query_thread.is_alive():
            messagebox.showinfo("提示", "查询正在进行中，请稍候。")
            return

        label = self.chain_var.get()
        chain_id = self._chain_map.get(label)
        if not chain_id:
            messagebox.showerror("错误", "请选择链")
            return
        chain = get_chain(self.config, chain_id)
        if not chain:
            messagebox.showerror("错误", f"链配置缺失: {chain_id}")
            return

        symbol_input = self.symbol_var.get().strip()
        if not symbol_input:
            messagebox.showerror("错误", "请输入代币符号或 0x 合约地址")
            return

        # 符号 or 合约地址
        if symbol_input.lower().startswith("0x") and len(symbol_input) == 42:
            token_addr = symbol_input
            symbol_hint = "(合约地址)"
        else:
            token_addr = resolve_token_address(chain_id, symbol_input)
            symbol_hint = symbol_input.upper()
            if not token_addr:
                messagebox.showwarning(
                    "未找到代币",
                    f"tokens.yaml 中无 {symbol_input} 的合约地址映射。\n"
                    "请改填该代币在 {0} 链上的 0x 合约地址，"
                    "或将映射写入 tokens.yaml 后重试。".format(chain.label))
                return

        try:
            threshold = float(self.threshold_var.get())
            hours = int(self.window_var.get())
            if threshold <= 0 or hours <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "阈值与窗口需为正数")
            return

        self._set_status(f"正在查询 {symbol_hint} 于 {chain.label} ...")
        self.query_btn.config(state=tk.DISABLED)
        self.push_btn.config(state=tk.DISABLED)

        self._query_thread = threading.Thread(
            target=self._query_worker,
            args=(chain, token_addr, symbol_hint, threshold, hours),
            daemon=True,
        )
        self._query_thread.start()

    def _query_worker(self, chain, token_addr, symbol_hint, threshold, hours):
        try:
            ds = DataSource(chain, page_size=self.qcfg["page_size"],
                            timeout=self.qcfg["timeout"],
                            rate_limit_sleep=self.qcfg["rate_limit_sleep"])
            info: TokenInfo = ds.get_token_info(token_addr)
            if not info.symbol:
                info.symbol = symbol_hint
            transfers = ds.fetch_transfers(
                token_addr, info.decimals, info.symbol, info.price_usd,
                threshold, hours)
            holders = ds.fetch_top_holders(
                token_addr, info.decimals, info.total_supply,
                self.qcfg["top_holders_limit"], transfers=transfers)
            self.after(0, self._on_query_done, {
                "chain": chain, "info": info, "transfers": transfers,
                "holders": holders, "threshold": threshold, "hours": hours,
                "token_addr": token_addr,
            })
        except Exception as e:
            logger.exception("查询失败")
            self.after(0, self._on_query_error, f"{type(e).__name__}: {e}")

    def _on_query_error(self, msg: str):
        self.query_btn.config(state=tk.NORMAL)
        self._set_status(f"查询失败: {msg}")
        messagebox.showerror("查询失败", msg)

    def _on_query_done(self, result: dict):
        self.query_btn.config(state=tk.NORMAL)
        self._last_result = result
        self.push_btn.config(state=tk.NORMAL)

        info: TokenInfo = result["info"]
        chain = result["chain"]
        transfers: list[Transfer] = result["transfers"]
        holders: list[Holder] = result["holders"]

        self._render_token_info(info, chain, result)
        self._render_transfers(transfers)
        self._render_holders(holders)

        self._set_status(
            f"完成: {chain.label} {info.symbol} | 大额 {len(transfers)} 笔 | "
            f"持有人 {len(holders)} 个 | {datetime.now():%H:%M:%S}")

    def _render_token_info(self, info: TokenInfo, chain, result: dict):
        self.token_info_text.delete("1.0", tk.END)
        def line(k, v):
            self.token_info_text.insert(tk.END, f"{k}: {v}\n")
        line("链", chain.label)
        line("代币名称", info.name or "N/A")
        line("符号", info.symbol or "N/A")
        line("合约地址", info.address)
        line("decimals", info.decimals)
        line("总供应量", _fmt_amount(info.total_supply))
        line("单价", _fmt_usd(info.price_usd))
        line("大额阈值", _fmt_usd(result["threshold"]))
        line("查询窗口", f"{result['hours']} 小时")

    def _render_transfers(self, transfers: list[Transfer]):
        for iid in self.transfers_tree.get_children():
            self.transfers_tree.delete(iid)
        for t in transfers:
            self.transfers_tree.insert("", tk.END, values=(
                t.timestamp, _short_addr(t.from_addr), t.from_tag or "未知",
                _short_addr(t.to_addr), t.to_tag or "未知",
                _fmt_amount(t.value), _fmt_usd(t.value_usd),
                _short_addr(t.hash),
            ))

    def _render_holders(self, holders: list[Holder]):
        for iid in self.holders_tree.get_children():
            self.holders_tree.delete(iid)
        for i, h in enumerate(holders, 1):
            self.holders_tree.insert("", tk.END, values=(
                i, h.address, h.label or "未知（待核实）", h.type or "-",
                _fmt_amount(h.balance), _fmt_pct(h.share),
            ))

    # ----------------- 飞书推送 -----------------

    def _on_push_feishu(self):
        if not self._last_result:
            messagebox.showinfo("提示", "请先查询，再推送")
            return
        webhook = (self.feishu_cfg.get("webhook") or "").strip()
        if not webhook or webhook.endswith("your-bot-id"):
            messagebox.showwarning(
                "未配置飞书",
                "请在 config.yaml 中填入 feishu.webhook（自定义机器人地址）。\n"
                "若使用加签，也填 feishu.secret。")
            return

        if self._query_thread and self._query_thread.is_alive():
            messagebox.showinfo("提示", "查询进行中，请稍候")
            return

        self.push_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._push_worker, daemon=True).start()

    def _push_worker(self):
        try:
            r = self._last_result
            info: TokenInfo = r["info"]
            chain = r["chain"]
            transfers: list[Transfer] = r["transfers"]
            holders: list[Holder] = r["holders"]
            token_addr = r["token_addr"]

            bot = FeishuBot(
                webhook=self.feishu_cfg.get("webhook", ""),
                secret=self.feishu_cfg.get("secret", ""),
                timeout=int(self.feishu_cfg.get("timeout", 10)),
            )
            ok = push_whale_report(
                bot,
                chain_label=chain.label,
                symbol=info.symbol or "(未知)",
                token_addr=token_addr,
                explorer_address_base=chain.explorer_address,
                large_transfers=[
                    {
                        "hash": chain.explorer_tx + t.hash if t.hash else "",
                        "from": t.from_addr, "to": t.to_addr,
                        "value": t.value, "value_usd": t.value_usd,
                        "timestamp": t.timestamp,
                        "from_tag": t.from_tag, "to_tag": t.to_tag,
                    }
                    for t in transfers
                ],
                top_holders=[
                    {
                        "address": h.address, "balance": h.balance,
                        "share": h.share, "label": h.label, "type": h.type,
                    }
                    for h in holders
                ],
                threshold_usd=r["threshold"],
                window_hours=r["hours"],
                total_supply=info.total_supply,
                token_price=info.price_usd,
            )
            self.after(0, self._on_push_done, ok)
        except Exception as e:
            logger.exception("飞书推送异常")
            self.after(0, self._on_push_done, False, f"{type(e).__name__}: {e}")

    def _on_push_done(self, ok: bool, err: str = ""):
        self.push_btn.config(state=tk.NORMAL)
        if ok:
            self._set_status("飞书推送成功")
            messagebox.showinfo("成功", "已推送到飞书")
        else:
            self._set_status("飞书推送失败" + (f": {err}" if err else ""))
            messagebox.showerror("推送失败", err or "请查看日志")

    # ----------------- 工具 -----------------

    def _set_status(self, text: str):
        self.status_var.set(text)


def main():
    try:
        app = WhaleAlertApp()
        app.mainloop()
    except Exception as e:  # noqa: BLE001
        # tkinter 不可用等极端情况
        import traceback
        traceback.print_exc()
        print(f"\n启动失败: {type(e).__name__}: {e}")
        print("请确保已安装 requests / PyYAML，且 Python 包含 tkinter。")
        sys.exit(1)


if __name__ == "__main__":
    main()
