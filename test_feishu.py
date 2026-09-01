#!/usr/bin/env python3
"""test_feishu.py —— 独立测试飞书 webhook 连通性 + 签名校验

用法：
    # 方式 1：自动从 .env 读取（推荐）
    python test_feishu.py

    # 方式 2：命令行参数
    python test_feishu.py --url <WEBHOOK_URL> [--secret <SECRET>]

    # 方式 3：环境变量
    FEISHU_WEBHOOK_URL=... FEISHU_WEBHOOK_SECRET=... python test_feishu.py

成功时飞书群里会收到一条测试卡片消息，包含当前时间和签名信息。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import sys
import time

import requests
from dotenv import load_dotenv


def feishu_sign(secret: str) -> tuple[str, str]:
    """飞书签名算法（同 monitor.py _feishu_sign）"""
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    return timestamp, sign


def send_card(webhook_url: str, secret: str | None) -> bool:
    """发送一条测试卡片"""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    content_lines = [
        f"**测试时间**: {now}",
        f"**签名校验**: {'启用 ✅' if secret else '未启用'}",
        "**说明**: 这是一条来自 monitor.py 的连通性测试消息。",
    ]

    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🔔 ERC20 监控测试"},
                "template": "green",
            },
            "elements": [
                {"tag": "div", "text": {
                    "tag": "lark_md",
                    "content": "\n".join(content_lines),
                }},
            ],
        },
    }

    if secret:
        ts, sign = feishu_sign(secret)
        payload["timestamp"] = ts
        payload["sign"] = sign
        print(f"[签名] timestamp={ts}  sign={sign}")

    print(f"[POST] {webhook_url}")
    resp = requests.post(
        webhook_url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload),
        timeout=10,
    )

    print(f"[响应] status={resp.status_code}  body={resp.text[:300]}")

    if resp.status_code != 200:
        print("❌ HTTP 状态码非 200")
        return False

    try:
        body = resp.json()
    except ValueError:
        print("❌ 返回非 JSON")
        return False

    code = body.get("StatusCode", body.get("code", -1))
    msg = body.get("StatusMessage", body.get("msg", ""))
    if code == 0:
        print("✅ 推送成功！请检查飞书群是否收到消息")
        return True
    else:
        print(f"❌ 飞书业务错误 code={code} msg={msg}")
        # 常见错误提示
        if code == 19001:
            print("   → 签名校验失败：检查 FEISHU_WEBHOOK_SECRET 是否正确")
        elif code == 19002:
            print("   → webhook URL 无效：检查 URL 是否完整复制")
        elif code == 19021:
            print("   → 关键词校验失败：机器人安全设置要求消息包含特定关键词")
        return False


def main() -> int:
    # 先加载 .env
    load_dotenv()

    parser = argparse.ArgumentParser(description="测试飞书 webhook 连通性")
    parser.add_argument("--url", help="飞书 webhook URL（默认读 .env）")
    parser.add_argument("--secret", help="签名密钥（默认读 .env，不需要可留空）")
    args = parser.parse_args()

    import os
    webhook_url = args.url or os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    secret = args.secret or os.getenv("FEISHU_WEBHOOK_SECRET", "").strip() or None

    if not webhook_url:
        print("❌ 未找到 webhook URL。请在 .env 设置 FEISHU_WEBHOOK_URL 或用 --url 传入")
        return 1

    print(f"[配置] webhook={'*' * 20}{webhook_url[-8:]}  secret={'已设置' if secret else '未设置'}")

    ok = send_card(webhook_url, secret)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
