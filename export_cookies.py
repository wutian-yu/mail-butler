# -*- coding: utf-8 -*-
"""
西浦 Cookie 导出工具
====================
在你的 Mac 上运行此脚本：
  python3 export_cookies.py

流程：
1. 自动打开浏览器窗口（不是无头模式）
2. 你手动登录 uim.xjtlu.edu.cn（如手机收到MFA推送，点一下确认）
3. 脚本检测到登录成功后自动抓取 Cookie
4. 把输出的 JSON 复制到 GitHub Secrets 的 EBRIDGE_COOKIES

依赖安装（只需一次）：
  pip3 install playwright && python3 -m playwright install chromium
"""
import json
import time
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("❌ 请先安装 Playwright：")
    print("   pip3 install playwright && python3 -m playwright install chromium")
    sys.exit(1)


def main():
    print("=" * 50)
    print("🍪 西浦 Cookie 导出工具")
    print("=" * 50)
    print()
    print("即将打开浏览器，请你：")
    print("1. 输入账号密码登录（就是你平时怎么登就怎么登）")
    print("2. 如果手机收到 MFA 推送，点一下确认")
    print("3. 登录成功进入 eBridge 后，回到这个窗口")
    print()

    with sync_playwright() as p:
        # 有头模式 —— 真实浏览器窗口
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        # 用 iPhone 模式（跟用户平时登录一致）
        iphone = p.devices["iPhone 13"]
        context = browser.new_context(**iphone, locale="zh-CN")
        page = context.new_page()

        print("🌐 正在打开 uim.xjtlu.edu.cn ...")
        page.goto("https://uim.xjtlu.edu.cn", wait_until="networkidle", timeout=30000)

        print("⏳ 等待你完成登录（最多等3分钟）...")
        print("   登录成功后脚本会自动检测，无需任何操作")
        print()

        # 等待登录成功：URL 变成 ebridge 或不再包含 login
        logged_in = False
        for i in range(180):  # 3分钟
            time.sleep(1)
            try:
                url = page.url
                if "ebridge" in url and "login" not in url.lower():
                    logged_in = True
                    break
                # 每30秒提示一次
                if i % 30 == 0 and i > 0:
                    print(f"   ...还在等待 ({i}秒)，当前: {url[:50]}")
            except Exception:
                continue

        if not logged_in:
            # 最后再检查一次页面内容
            try:
                content = page.content()
                if "Guancheng" in content or "e-Bridge" in content or "Welcome" in content:
                    logged_in = True
            except Exception:
                pass

        if not logged_in:
            print("❌ 3分钟内未检测到登录成功，请重试")
            browser.close()
            sys.exit(1)

        print("✅ 检测到登录成功！正在抓取 Cookie...")
        time.sleep(3)  # 等页面完全加载

        # 抓取所有域名的 Cookie
        cookies = context.cookies()

        # 只要 uim/ebridge/sso 相关的
        xjtlu_cookies = [c for c in cookies
                         if any(d in c.get("domain", "") for d in
                                ["xjtlu.edu.cn", "learningmall.cn"])]

        print(f"🍪 抓到 {len(xjtlu_cookies)} 个 Cookie")

        # 序列化成单行 JSON
        cookie_json = json.dumps(xjtlu_cookies, ensure_ascii=False, separators=(",", ":"))

        # 保存到文件备份
        with open("xjtlu_cookies.json", "w", encoding="utf-8") as f:
            f.write(cookie_json)
        print(f"💾 已备份到 xjtlu_cookies.json")

        print()
        print("=" * 50)
        print("📋 复制下面整行到 GitHub Secrets：")
        print("=" * 50)
        print()
        print(f"EBRIDGE_COOKIES 的值（整行复制）：")
        print()
        print(cookie_json)
        print()
        print("=" * 50)
        print("操作步骤：")
        print("1. 打开 https://github.com/wutian-yu/mail-butler/settings/secrets/actions")
        print("2. New repository secret")
        print("3. Name: EBRIDGE_COOKIES")
        print("4. Secret: 粘贴上面那一整行 JSON")
        print("5. Add secret 保存")
        print()
        print("✅ 完成后告诉星辰，服务器就能用这个 Cookie 登录了")
        print("⏰ Cookie 一般能用 1-7 天，过期后重新跑一次本脚本即可")

        browser.close()


if __name__ == "__main__":
    main()
