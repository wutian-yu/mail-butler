# -*- coding: utf-8 -*-
"""LM 页面扫描器（一次性知识采集）：SAML 登录 LearningMall（完全复刻 xjtlu_monitor 的登录方式），
抓取 dashboard 全部课程/模块 + 关键子页（Exam/Quiz/Guide/Journey/Handbook/Regulation），
结果存 butler-data/lm_pages.json。只读操作。"""

import json
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright

MAX_PAGES = 70
TEXT_LIMIT = 15000
LM_CORE_URL = "https://core.xjtlu.edu.cn"
LM_SAML_LOGIN_URL = (LM_CORE_URL + "/auth/saml2/login.php"
                     "?wants=https%3A%2F%2Fcore.xjtlu.edu.cn%2F"
                     "&idp=59e86c8687092bdfa106649b2d5519e5&passive=off")


def log(msg):
    print(msg, flush=True)


def main():
    cookie_str = os.environ.get("EBRIDGE_COOKIES", "")
    if not cookie_str:
        log("无 EBRIDGE_COOKIES，退出")
        sys.exit(1)
    results = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        try:
            context.add_cookies(json.loads(cookie_str))
        except Exception as e:
            log(f"cookie 加载失败: {e}")
            sys.exit(1)

        page = context.new_page()
        # 验证 SSO 有效性（与 monitor 相同）
        page.goto("https://uim.xjtlu.edu.cn", wait_until="networkidle", timeout=30000)
        time.sleep(3)
        if "login" in page.url.lower():
            log("SSO cookie 已过期，退出")
            sys.exit(1)
        log("✅ SSO 会话有效")

        # eBridge 预热：monitor 的成功路径是先登 eBridge 让 IdP 建立 session，
        # 直接走 SAML 会卡在 IdP 的 SSO 端点
        page.goto("https://ebridge.xjtlu.edu.cn", wait_until="domcontentloaded", timeout=45000)
        for i in range(20):
            time.sleep(3)
            u = page.url
            log(f"  eBridge预热[{i+1}]: {u[:70]}")
            if "ebridge" in u and "siw_lgn" not in u and "login" not in u.lower():
                log("✅ eBridge 登录完成（IdP session 已建立）")
                break

        # SAML 登录（此时 IdP 已有 session，应自动放行）
        page.goto(LM_SAML_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        logged = False
        for i in range(20):
            time.sleep(2)
            u = page.url
            log(f"  跳转[{i+1}]: {u[:70]}")
            if u.startswith(LM_CORE_URL) and "/auth/saml2" not in u:
                logged = True
                break
        if not logged:
            page.goto(LM_CORE_URL + "/my/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(5)
            logged = "login" not in page.url.lower()
        if not logged:
            log("LM 登录失败")
            sys.exit(1)
        log("✅ LM 登录成功")

        def grab(url):
            url = url.split("#")[0]
            m = re.search(r"(https://core\.xjtlu\.edu\.cn/[a-z_]+\.php\?id=\d+)", url)
            if m:
                url = m.group(1)
            if url in results or len(results) >= MAX_PAGES:
                return
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2.5)
                text = page.evaluate(
                    "() => document.body ? document.body.innerText.substring(0, %d) : ''" % TEXT_LIMIT)
                links = page.evaluate("""() => {
                    const seen = new Set(); const out = [];
                    document.querySelectorAll('a[href]').forEach(a => {
                        const t = (a.innerText || '').trim();
                        const h = a.href || '';
                        if (t.length > 2 && t.length < 120 && !seen.has(h)) {
                            seen.add(h); out.push({name: t, url: h});
                        }
                    });
                    return out.slice(0, 100);
                }""")
                results[url] = {"title": page.title(), "text": text, "links": links}
                log(f"  抓取: {page.title()[:50]} | {url[:70]}")
            except Exception as e:
                log(f"  失败: {url[:60]} — {e}")

        grab(LM_CORE_URL + "/my/")
        dash_links = results.get(LM_CORE_URL + "/my/", {}).get("links", [])
        course_urls = [l["url"] for l in dash_links
                       if re.search(r"/course/view\.php\?id=\d+", l["url"])
                       and l["url"] not in [c for c in []]]
        seen_c = []
        for cu in course_urls:
            m = re.search(r"\?id=(\d+)$", cu)
            if m and m.group(1) not in seen_c:
                seen_c.append(m.group(1))
                grab(f"{LM_CORE_URL}/course/view.php?id={m.group(1)}")
        log(f"dashboard 课程/模块数: {len(seen_c)}")

        kw = re.compile(r"(exam|quiz|test|guide|journey|handbook|regulat|assessment|inspera|"
                        r"policy|integrity|attendance|welcome|outline|new student)", re.I)
        for cu in [f"{LM_CORE_URL}/course/view.php?id={cid}" for cid in seen_c]:
            pd = results.get(cu)
            if not pd:
                continue
            is_mth = "MTH026" in pd.get("title", "") or "Calculus" in pd.get("title", "")
            budget = 8 if is_mth else 4
            dug = 0
            for l in pd.get("links", []):
                if dug >= budget or len(results) >= MAX_PAGES:
                    break
                if not re.search(r"core\.xjtlu\.edu\.cn", l["url"]):
                    continue
                if re.search(r"/course/view\.php\?id=\d+$", l["url"]):
                    continue
                if kw.search(l["name"] + " " + l["url"]):
                    before = len(results)
                    grab(l["url"])
                    if len(results) > before:
                        dug += 1
        for l in dash_links:
            if len(results) >= MAX_PAGES:
                break
            if not re.search(r"core\.xjtlu\.edu\.cn", l["url"]):
                continue
            if re.search(r"/course/view\.php\?id=\d+", l["url"]):
                continue
            if kw.search(l["name"]):
                grab(l["url"])

        browser.close()

    out = {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pages": results}
    state_file = os.environ.get("XJTLU_STATE_FILE", "data/xjtlu_state.json")
    out_file = os.path.join(os.path.dirname(state_file), "lm_pages.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f"✅ 共抓取 {len(results)} 页 -> {out_file} ({os.path.getsize(out_file)} bytes)")


if __name__ == "__main__":
    main()
