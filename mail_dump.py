# -*- coding: utf-8 -*-
"""邮件 dump（一次性）：拉最近 80 封邮件的主题+发件人+正文纯文本，
存 butler-data/mails_dump.json 供管家知识库建设与事实核对。只读。"""

import base64
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ORIGIN = "https://outlook.office.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def get_access_token():
    body = urllib.parse.urlencode({
        "client_id": os.environ["OUTLOOK_CLIENT_ID"],
        "grant_type": "refresh_token",
        "refresh_token": os.environ["OUTLOOK_REFRESH_TOKEN"],
        "scope": "https://outlook.office.com/.default offline_access",
        "client_info": "1",
    }).encode()
    req = urllib.request.Request(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": ORIGIN, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def strip_html(raw):
    if not raw:
        return ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|tr|li|h\d)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def main():
    token = get_access_token()
    print("✅ Outlook token 已获取", flush=True)
    url = ("https://outlook.office.com/api/v2.0/me/messages"
           "?$top=80&$select=Subject,ReceivedDateTime,From,Body"
           "&$orderby=" + urllib.parse.quote("ReceivedDateTime DESC"))
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json",
        "Origin": ORIGIN, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    msgs = []
    for m in data.get("value", []):
        body = (m.get("Body") or {}).get("Content", "")
        msgs.append({
            "subject": m.get("Subject", ""),
            "from": ((m.get("From") or {}).get("EmailAddress") or {}).get("Address", ""),
            "received": m.get("ReceivedDateTime", ""),
            "body": strip_html(body)[:5000],
        })
    out = {"dumped_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(msgs), "mails": msgs}
    state_file = os.environ.get("DATA_DIR", "data") + "/xjtlu_state.json"
    out_file = os.path.join(os.path.dirname(state_file), "mails_dump.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"✅ dump {len(msgs)} 封邮件 -> {out_file} ({os.path.getsize(out_file)} bytes)", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ {e}", flush=True)
        sys.exit(1)
