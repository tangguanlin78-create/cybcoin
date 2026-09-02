"""临时 RPC 连通性测试脚本（Alchemy 版本），跑完即可删。"""
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv('RPC_URL_ETHEREUM', '').split('/v2/')[-1]

# 7 条链的 Alchemy URL（从 .env 读取）
RPCS = {}
for chain, env_key in [
    ('ethereum',  'RPC_URL_ETHEREUM'),
    ('bsc',       'RPC_URL_BSC'),
    ('polygon',   'RPC_URL_POLYGON'),
    ('arbitrum',  'RPC_URL_ARBITRUM'),
    ('optimism',  'RPC_URL_OPTIMISM'),
    ('base',      'RPC_URL_BASE'),
    ('avalanche', 'RPC_URL_AVALANCHE'),
]:
    url = os.getenv(env_key, '')
    if url:
        RPCS[chain] = url

print(f'Alchemy key = {KEY[:8]}...{KEY[-4:]}')
print(f'测试 {len(RPCS)} 条链:\n')

headers = {'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
payload_bn = json.dumps({'jsonrpc': '2.0', 'method': 'eth_blockNumber', 'params': [], 'id': 1})

# 额外测试 eth_getLogs（监控实际会用的重 RPC）
# 跨度 10 块（alchemy 免费档单次上限）—— 用 0x0-0x9 区间 + USDT 合约拉早期 logs
payload_logs_template = json.dumps({
    'jsonrpc': '2.0', 'method': 'eth_getLogs',
    'params': [{'fromBlock': '0x0', 'toBlock': '0x9', 'address': '0xdAC17F958D2ee523a2206206994597C13D831ec7'}],
    'id': 1,
})

all_ok = True
for chain, url in RPCS.items():
    # 1) eth_blockNumber 测试
    try:
        r = requests.post(url, data=payload_bn, headers=headers, timeout=10)
        if r.status_code == 200 and 'result' in r.text:
            bn = int(r.json().get('result', '0x0'), 16)
            print(f'  [{chain:10s}] blockNumber OK  block={bn}  url={url}')
        else:
            print(f'  [{chain:10s}] blockNumber FAIL  status={r.status_code}  body={r.text[:120]}')
            all_ok = False
            continue
    except Exception as e:
        print(f'  [{chain:10s}] blockNumber ERR  {type(e).__name__}: {e}')
        all_ok = False
        continue

    # 2) eth_getLogs 测试（监控会用的重 RPC，确认不限流）
    try:
        r = requests.post(url, data=payload_logs_template, headers=headers, timeout=10)
        if r.status_code == 200 and 'result' in r.text:
            logs = r.json().get('result', [])
            print(f'           getLogs OK  logs_count={len(logs)}  (重 RPC 不限流)')
        else:
            err = r.json().get('error', {}) if 'json' in r.headers.get('content-type','') else r.text[:120]
            print(f'           getLogs FAIL  status={r.status_code}  err={err}')
            all_ok = False
    except Exception as e:
        print(f'           getLogs ERR  {type(e).__name__}: {e}')
        all_ok = False

print(f'\n{"="*40}')
print(f'结果: {"全部通过" if all_ok else "有不通过的链"}')
