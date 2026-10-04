# -*- coding: utf-8 -*-
"""
LM/eBridge 网页查询器 (v26)
==========================
由飞书管家云函数调度（web-fetch.yml workflow_dispatch）：
  WEB_FETCH_OP = {"url": "...", "question": "...", "chat_id": "..."}

流程：加载 Cookie（butler-data 文件优先）→ 无头浏览器打开页面 → 提取正文
     → DeepSeek 按用户问题做摘要回答 → 飞书回贴结果 → 原文存档 butler-data
安全：仅允许西浦域名；Cookie 失效时提示刷新方式。
"""
import json
import os
import sys
import time
import urllib.request

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
ALLOWED_DOMAINS = ("xjtlu.edu.cn", "learningmall.cn")
DATA_DIR = os.environ.get("XJTLU_DATA_DIR", "data")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def get_feishu_token():
    data = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15))["tenant_access_token"]


def feishu_send(chat_id, text, card_title="🌐 网页查询"):
    if not (chat_id and FEISHU_APP_ID):
        return
    try:
        token = get_feishu_token()
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text", "content": card_title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": "🤖 管家 · 云端浏览器实地抓取"}
                ]},
            ],
        }
        data = json.dumps({"receive_id": chat_id, "msg_type": "interactive",
                           "content": json.dumps(card)}).encode()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=data, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        log(f"✅ 飞书消息已发送：{text[:50]}")
    except Exception as e:
        log(f"⚠️ 飞书发送失败: {e}")


def deepseek_answer(title, url, page_text, question):
    """用页面正文回答用户问题；没有 key 或无问题时返回 None"""
    if not (DEEPSEEK_API_KEY and question):
        return None
    try:
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system",
                 "content": "你是西浦学生的飞书管家。下面是你刚用云端浏览器实地打开页面抓取的正文。"
                            "请基于正文回答用户问题，简洁分点、引用关键时间/数字/标题；"
                            "正文里没有的信息如实说「页面上没有」，绝不编造。中文回答，300字以内。"},
                {"role": "user",
                 "content": f"页面标题：{title}\n页面URL：{url}\n\n===页面正文===\n{page_text[:11000]}\n"
                            f"===用户问题===\n{question}"},
            ],
            "max_tokens": 600, "temperature": 0.3,
        }).encode()
        req = urllib.request.Request("https://api.deepseek.com/v1/chat/completions", data=body,
                                     headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                                              "Content-Type": "application/json"})
        resp = json.load(urllib.request.urlopen(req, timeout=90))
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"⚠️ DeepSeek 摘要失败（回退原文）: {e}")
        return None


def load_cookies():
    """优先读 butler-data 文件（飞书 cookie 命令维护），回退环境变量"""
    cf = os.environ.get("XJTLU_COOKIES_FILE", "")
    if cf and os.path.exists(cf):
        try:
            s = open(cf, encoding="utf-8").read().strip()
            if s.startswith("["):
                log(f"🍪 使用文件 Cookie（butler-data，{len(s)} 字节）")
                return s
        except Exception as e:
            log(f"⚠️ Cookie 文件读取失败: {e}")
    return os.environ.get("EBRIDGE_COOKIES", "")


def save_cache(entry):
    """把最近 5 次查询存到 butler-data/web_fetch_cache.json（workflow 的 push 步骤会提交）"""
    try:
        path = os.path.join(DATA_DIR, "web_fetch_cache.json")
        cache = {"items": []}
        if os.path.exists(path):
            cache = json.load(open(path, encoding="utf-8"))
        items = cache.get("items") or []
        items.insert(0, entry)
        cache["items"] = items[:5]
        json.dump(cache, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log(f"💾 已存档 web_fetch_cache.json")
    except Exception as e:
        log(f"⚠️ 存档失败（不影响主流程）: {e}")


def main():
    try:
        op = json.loads(os.environ.get("WEB_FETCH_OP") or "{}")
    except Exception:
        op = {}
    url = str(op.get("url") or "").strip()
    question = str(op.get("question") or "").strip()
    chat_id = str(op.get("chat_id") or "")
    if not (url and chat_id):
        log("❌ 参数缺失：需要 url 和 chat_id")
        return

    # 域名白名单
    if not any(d in url for d in ALLOWED_DOMAINS):
        feishu_send(chat_id, f"⚠️ 只支持西浦校内页面（xjtlu.edu.cn / learningmall.cn），该地址不在白名单内：\n{url[:80]}")
        return

    cookie_str = load_cookies()
    if not cookie_str:
        feishu_send(chat_id, "⚠️ 管家这边没有西浦 Cookie，查不了网页。\n\n刷新方法：飞书发「cookie <导出的JSON>」或找星辰直接抓。")
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 900})
        try:
            ctx.add_cookies(json.loads(cookie_str))
        except Exception as e:
            feishu_send(chat_id, f"⚠️ Cookie 格式异常，请重新导出：{str(e)[:60]}")
            browser.close()
            return
        page = ctx.new_page()
        try:
            log(f"🌐 打开: {url[:80]}")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # SSO 跳转链（LM→uim→LM / eBridge→uim→eBridge）可能要几秒，循环等它落地（最多30秒）
            for i in range(10):
                page.wait_for_timeout(3000)
                u = page.url
                if "login" not in u.lower() and "siw_lgn" not in u and "esc-sso" not in u:
                    break
                if i % 3 == 2:
                    log(f"⏳ SSO 链跳转中... URL: {u[:70]}")
        except Exception as e:
            feishu_send(chat_id, f"⚠️ 页面打开失败：{str(e)[:80]}\n\n地址：{url[:80]}")
            browser.close()
            return

        final_url = page.url
        if "login" in final_url.lower() or "siw_lgn" in final_url or "esc-sso" in final_url:
            feishu_send(chat_id, "⚠️ Cookie 已失效（页面跳回了登录页）。\n\n刷新方法：飞书发「cookie <导出的JSON>」或找星辰直接抓，然后再查一次。")
            browser.close()
            return
        # 再等 2 秒让页面 JS 完成渲染
        page.wait_for_timeout(2000)

        info = page.evaluate(
            """() => {
                const el = document.querySelector('[data-region="course-content"]')
                         || document.querySelector('main') || document.body;
                let text = el ? el.innerText : '';
                text = text.replace(/[ \\t]+\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
                const links = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const t = (a.innerText || '').trim();
                    if (t && t.length < 60 && links.length < 30 &&
                        !links.some(l => l.t === t)) links.push({t: t, h: a.href.split('#')[0]});
                });
                return {title: document.title, url: location.href,
                        text: text.slice(0, 12000), links: links};
            }""")
        browser.close()

    title = info.get("title") or "(无标题)"
    page_text = info.get("text") or ""
    log(f"📄 标题: {title[:50]} | 正文 {len(page_text)} 字符")

    save_cache({"time": time.strftime("%Y-%m-%d %H:%M"),
                "url": url, "question": question, "title": title,
                "text": page_text[:3000]})

    if not page_text:
        feishu_send(chat_id, f"✅ 打开了「{title[:40]}」，但页面是空的（可能是纯文件下载页或需要点击进入）。")
        return

    ans = deepseek_answer(title, final_url, page_text, question)
    if ans:
        feishu_send(chat_id, f"**{title[:60]}**\n\n{ans}\n\n---\n📎 [页面原文]({final_url})")
    else:
        # 无问题或摘要失败：直接贴正文精简版
        preview = page_text[:1800] + ("…" if len(page_text) > 1800 else "")
        feishu_send(chat_id, f"**{title[:60]}**\n\n{preview}\n\n---\n📎 [页面原文]({final_url})")
    log("✅ 查询完成")


if __name__ == "__main__":
    main()
