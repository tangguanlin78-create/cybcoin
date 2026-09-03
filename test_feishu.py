"""飞书自定义机器人连通性测试。

用法：python test_feishu.py
从 config.yaml 读取 feishu 配置，发送一条测试富文本消息。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chains import load_config  # noqa: E402
from feishu import FeishuBot, _line, _text  # noqa: E402


def main():
    cfg = load_config()
    fe = cfg.get("feishu") or {}
    webhook = (fe.get("webhook") or "").strip()
    secret = (fe.get("secret") or "").strip()

    if not webhook or "PASTE" in webhook:
        print("[!] config.yaml 中 feishu.webhook 仍是占位符，请先填入真实 webhook。")
        sys.exit(1)

    print(f"[*] webhook: {webhook[:60]}...")
    print(f"[*] 加签 secret: {'已设置 (' + str(len(secret)) + ' 字符)' if secret else '未启用加签'}")

    bot = FeishuBot(webhook, secret, timeout=int(fe.get("timeout", 10)))
    title = "加密货币大额转账查询 - 连通测试"
    lines = [
        _line(_text("━━━ 测试消息 ━━━", bold=True)),
        _line(_text("如果你看到这条消息，说明飞书自定义机器人配置正确。")),
        _line(_text("加签状态: "), _text("已启用" if secret else "未启用", bold=True)),
        _line(_text("后续在 GUI 点【推送到飞书】即可推送大额转账与 Top10 持有人报告。")),
    ]
    ok = bot.send_post(title, lines)
    if ok:
        print("[+] 推送成功，请到飞书群查看。")
    else:
        print("[x] 推送失败，详见上方日志。常见原因：")
        print("    - webhook URL 错误或已过期")
        print("    - 加签 secret 与机器人设置不匹配")
        print("    - 机器人被限制了 IP 或关键词安全设置")
        sys.exit(2)


if __name__ == "__main__":
    main()
