#!/usr/bin/env python3
"""飞书诊断：调用 tenant token 接口，打印错误码（不打印任何密钥）。"""
import json, urllib.request, urllib.parse
def req(url, data=None, headers=None, method=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method or ("POST" if data is not None else "GET"))
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if body:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, str(e)

import os
print("=== 1) tenant_access_token 接口 ===")
st, body = req("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
               data={"app_id": os.environ.get("FEISHU_APP_ID",""), "app_secret": os.environ.get("FEISHU_APP_SECRET","")})
print("HTTP", st)
print(body[:800])
PYEOF
echo "脚本写好"