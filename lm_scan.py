# -*- coding: utf-8 -*-
"""LM 页面扫描器（一次性知识采集）：登录 LearningMall，抓取
dashboard 全部课程/模块 + 每门课的活动链接 + 关键子页（Exam/Quiz/Guide/Journey/
Handbook/Regulation），结果存 butler-data/lm_pages.json 供管家知识库建设。
只读操作：不点击任何按钮、不提交任何表单。"""

import json
import os
import re
import sys
import time
from playwright.sync_api import sync_playwright

MAX_PAGES = 70
TEXT_LIMIT = 15000


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
            return

        page = context.new_page()
        # 验证 SSO 有效性
        page.goto("https://uim.xjtlu.edu.cn", wait_until="networkidle", timeout=30000)
        time.sleep(3)
        if "login" in page.url.lower():
            log("SSO cookie 已过期，退出（monitor secrets 需刷新）")
            sys.exit(1)
        log("✅ SSO 会话有效")

        # 登录 LM：必须先走 SAML SSO 入口（直接访问 /my/ 只会到本地登录页）
        sso_entry = ("https://core.xjtlu.edu.cn/auth/saml2/login.php"
                     "?wants=&idp=59e86c8687092bdfa106649b2d5519e5&passive=off")
        page.goto(sso_entry, wait_until="networkidle", timeout=60000)
        # SAML 链：Moodle→IdP→(自动提交表单)→ACS→session 建立，轮询等待
        logged = False
        for i in range(12):
            time.sleep(5)
            url = page.url
            log(f"  跳转中[{i+1}]: {url[:70]}")
            if "core.xjtlu.edu.cn" in url and "login" not in url.lower():
                logged = True
                break
            # 停在 IdP 自动提交页时，JS 会自动执行；若停留过久，尝试找 Continue 按钮点一下
            try:
                btn = page.query_selector("button[type=submit], input[type=submit]")
                if btn and "uim" in url:
                    btn.click()
            except Exception:
                pass
        if not logged:
            log("LM 登录失败（SAML 链未完成）")
            sys.exit(1)
        # 最终确认 dashboard 可达
        page.goto("https://core.xjtlu.edu.cn/my/", wait_until="networkidle", timeout=60000)
        time.sleep(3)
        if "login" in page.url.lower():
            log("LM 登录失败")
            sys.exit(1)
        log("✅ LM 登录成功: " + page.url[:60])

        def grab(url, depth=0):
            url = url.split("?")[0].split("#")[0]
            # 归一化 moodle 链接保留 id 参数
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
                title = page.title()
                results[url] = {"title": title, "text": text, "links": links}
                log(f"  抓取: {title[:50]} | {url[:70]}")
            except Exception as e:
                log(f"  失败: {url[:60]} — {e}")

        # 1. dashboard 本身（含 Your XJTU Journey 等站点级卡片）
        grab("https://core.xjtlu.edu.cn/my/")

        # 2. dashboard 上所有课程/模块链接
        dash_links = results.get("https://core.xjtlu.edu.cn/my/", {}).get("links", [])
        course_urls = []
        for l in dash_links:
            if re.search(r"/course/view\.php\?id=\d+", l["url"]):
                if l["url"] not in course_urls:
                    course_urls.append(l["url"])
        log(f"dashboard 课程/模块数: {len(course_urls)}")
        for cu in course_urls:
            grab(cu)

        # 3. 每门课深挖关键子页（exam/quiz/guide/journey/handbook/regulation/inspera）
        kw = re.compile(r"(exam|quiz|test|guide|journey|handbook|regulat|assessment|inspera|"
                        r"policy|integrity|attendance|welcome|outline)", re.I)
        for cu in list(course_urls):
            page_data = results.get(cu)
            if not page_data:
                continue
            # 优先 MTH026（用户当前关注微积分 quiz）
            is_mth = "MTH026" in page_data.get("title", "") or "Calculus" in page_data.get("title", "")
            budget = 8 if is_mth else 4
            dug = 0
            for l in page_data.get("links", []):
                if dug >= budget or len(results) >= MAX_PAGES:
                    break
                if not re.search(r"core\.xjtlu\.edu\.cn", l["url"]):
                    continue
                if re.search(r"/course/view\.php\?id=\d+$", l["url"]):
                    continue  # 课程页自身已抓
                if kw.search(l["name"] + " " + l["url"]):
                    before = len(results)
                    grab(l["url"])
                    if len(results) > before:
                        dug += 1

        # 4. dashboard 层的关键站点模块（Your XJTU Journey / Guide for New Students 等）
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

    out = {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pages": results,
    }
    state_file = os.environ.get("XJTLU_STATE_FILE", "data/xjtlu_state.json")
    out_file = os.path.join(os.path.dirname(state_file), "lm_pages.json")
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f"✅ 共抓取 {len(results)} 页，已写 {out_file} ({os.path.getsize(out_file)} bytes)")


if __name__ == "__main__":
    main()
