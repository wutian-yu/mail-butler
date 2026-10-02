# -*- coding: utf-8 -*-
"""
西浦网站监控 · XJTLU Monitor
============================
使用 Playwright 无头浏览器登录 SSO，自动监控 eBridge 和 LearningMall。
每 30 分钟运行一次，发现新内容则飞书通知。

环境变量：
  XJTLU_USERNAME — SSO 用户名（学号或邮箱前缀）
  XJTLU_PASSWORD — SSO 密码
  FEISHU_APP_ID / FEISHU_APP_SECRET — 飞书凭据
"""
import json
import os
import re
import time
import hashlib
import urllib.request
import urllib.parse
import html as html_mod
from datetime import datetime, timedelta

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright not installed")
    exit(1)

# ============ 配置 ============
XJTLU_USERNAME = os.environ.get("XJTLU_USERNAME", "")
XJTLU_PASSWORD = os.environ.get("XJTLU_PASSWORD", "")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
CHAT_ID = "oc_0f1f851c3fce82feff595f7a44bcc88a"
STATE_FILE = os.environ.get("XJTLU_STATE_FILE", ".secrets/xjtlu_state.json")
TIMETABLE_FILE = "timetable.json"
CALENDAR_FILE = "academic_calendar.json"
SSO_URL = "https://uim.xjtlu.edu.cn"
EBRIDGE_URL = "https://ebridge.xjtlu.edu.cn"
LM_URL = "https://www.learningmall.cn"

# LearningMall Core (Moodle) —— 真正的教学平台，非 www.learningmall.cn（那是宣传站）
LM_CORE_URL = "https://core.xjtlu.edu.cn"
LM_SAML_LOGIN_URL = (LM_CORE_URL + "/auth/saml2/login.php"
                     "?wants=https%3A%2F%2Fcore.xjtlu.edu.cn%2F"
                     "&idp=59e86c8687092bdfa106649b2d5519e5&passive=off")
# Moodle 日历订阅 URL（含全部课程作业/考试截止时间，authtoken 自足认证无需登录）
LM_CAL_DEFAULT_URL = (LM_CORE_URL + "/calendar/export_execute.php"
                      "?userid=7860&authtoken=e0f4ad6318700f9841cb02a96315cb30d6451624"
                      "&preset_what=all&preset_time=recentupcoming")

IMPORTANT_KEYWORDS = [
    "考试", "exam", "deadline", "截止", "ddl", "作业", "assignment",
    "成绩", "grade", "通知", "announcement", "重要", "important",
    "选课", "补考", "quiz", "test", "提交", "submit",
    "讲座", "lecture", "活动", "event", "报名", "register",
]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ============ 飞书通知 ============
def get_feishu_token():
    data = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["tenant_access_token"]


def feishu_send(text):
    if not FEISHU_APP_ID:
        return
    try:
        token = get_feishu_token()
        card = {
            "config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text", "content": "🏫 西浦校园通"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text", "content": "⚡ 西浦网站自动监控"}
                ]}
            ]
        }
        data = json.dumps({"receive_id": CHAT_ID, "msg_type": "interactive",
                           "content": json.dumps(card)}).encode()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=data, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        log(f"✅ 飞书通知已发送")
    except Exception as e:
        log(f"飞书发送失败: {e}")


# ============ SSO 登录 ============
def sso_login(page):
    """完全模仿用户真实登录：访问uim.xjtlu.edu.cn统一认证中心，模拟手指点击"""
    log("🔐 开始登录（uim.xjtlu.edu.cn 统一认证中心）...")
    try:
        # 访问统一认证中心（手机上实际跳转的登录页）
        page.goto("https://uim.xjtlu.edu.cn", wait_until="networkidle", timeout=30000)
        time.sleep(3)
        log(f"📄 当前URL: {page.url[:80]}")
        log(f"📄 页面标题: {page.title()[:50]}")

        # 保存调试信息
        os.makedirs(".secrets", exist_ok=True)

        # 查找账号和密码输入框
        username_input = None
        password_input = None
        u_sel = p_sel = ""
        selectors = [
            ("input[name='username']", "input[name='password']"),
            ("input#username", "input#password"),
            ("input[type='text']", "input[type='password']"),
            ("input[placeholder*='帐号']", "input[placeholder*='密码']"),
            ("input[placeholder*='账号']", "input[placeholder*='密码']"),
            ("input[placeholder*='学号']", "input[placeholder*='密码']"),
            ("input[placeholder*='User']", "input[placeholder*='Pass']"),
            ("input[placeholder*='user']", "input[placeholder*='pass']"),
        ]
        for u_sel, p_sel in selectors:
            try:
                username_input = page.query_selector(u_sel)
                password_input = page.query_selector(p_sel)
            except Exception:
                continue
            if username_input and password_input:
                log(f"✅ 找到登录表单: {u_sel}")
                break
            username_input = None
            password_input = None

        if not username_input or not password_input:
            log("❌ 未找到登录表单")
            html = page.content()
            return False

        # 填入凭据（模仿真实输入）
        username_input.fill(XJTLU_USERNAME)
        time.sleep(0.5)
        password_input.fill(XJTLU_PASSWORD)
        time.sleep(0.5)
        log("✅ 账号密码已输入")

        # === 关键：模拟真实手指/鼠标点击登录按钮 ===
        # 找到登录按钮的位置
        login_btn = page.query_selector("button:has-text('登录')") or \
                    page.query_selector("button:has-text('Login')") or \
                    page.query_selector("button[type='submit']") or \
                    page.query_selector("input[type='submit']") or \
                    page.query_selector("a:has-text('登录')") or \
                    page.query_selector(".login-btn") or \
                    page.query_selector(".btn-login")

        if not login_btn:
            # 用文字搜索按钮
            login_btn = page.evaluate("""() => {
                const els = [...document.querySelectorAll('button, a, input[type=submit], div[role=button], span[class*=btn]')];
                for (const el of els) {
                    const text = el.innerText || el.value || '';
                    if (text.includes('登录') || text.includes('Log in') || text.includes('Login')) {
                        return el;
                    }
                }
                return null;
            }""")
            if login_btn:
                log("⚠️ 用文字搜索找到登录按钮")

        if login_btn:
            # 获取按钮屏幕坐标（模拟真实点击）
            box = login_btn.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                log(f"🖱️ 登录按钮位置: ({x:.0f}, {y:.0f})")
                # 模拟真实鼠标：移动 → 按下 → 释放
                page.mouse.move(x, y)
                time.sleep(0.2)
                page.mouse.down()
                time.sleep(0.1)
                page.mouse.up()
                log("✅ 已模拟真实点击登录按钮")
            else:
                # bounding_box为空时退回JS点击
                login_btn.click(force=True)
                log("✅ 已force点击登录按钮")
        else:
            # 找不到按钮就用回车提交
            password_input.press("Enter")
            log("✅ 已按回车提交")

        # 等待登录处理完成（页面跳转过程中context会销毁，要用try/except保护）
        log("⏳ 等待登录跳转...")
        time.sleep(8)  # 用户视频显示Processing要5-8秒
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        time.sleep(2)

        # 检查登录结果
        try:
            current_url = page.url
            log(f"📄 登录后URL: {current_url[:70]}")
        except Exception as e:
            log(f"⚠️ 获取URL失败: {e}")
            current_url = ""

        # 判断登录成功：URL跳转到eBridge或不再是登录页
        if current_url and "login" not in current_url.lower() and "uim.xjtlu" not in current_url:
            log(f"✅ 登录成功！已进入: {current_url[:50]}")
            return True

        # 页面可能还在Processing，再等等
        for i in range(10):
            time.sleep(2)
            try:
                current_url = page.url
                processing = page.query_selector("text=Processing")
                if not processing and current_url and "login" not in current_url.lower() and "uim.xjtlu" not in current_url:
                    log(f"✅ 登录成功（等待{20+i*2}秒后）！URL: {current_url[:50]}")
                    return True
                if i % 3 == 0:
                    log(f"⏳ 等待中... ({i*2+12}秒) URL: {current_url[:40]}")
            except Exception:
                continue

        # 最终检查
        try:
            current_url = page.url
            if "login" not in current_url.lower() and "uim.xjtlu" not in current_url:
                log(f"✅ 登录成功！URL: {current_url[:50]}")
                return True
            error = page.query_selector("[class*=error]") or page.query_selector("[class*=Error]")
            if error:
                log(f"❌ 登录失败: {error.inner_text()[:80]}")
            else:
                log("❌ 登录后仍在登录页")
        except Exception as e:
            log(f"⚠️ 最终检查失败: {e}")
        return False
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        return False

        # 填入凭据 — 多种方式依次尝试
        filled = False
        # 方式1：fill with force
        try:
            username_input.fill(XJTLU_USERNAME, force=True, timeout=8000)
            password_input.fill(XJTLU_PASSWORD, force=True, timeout=8000)
            filled = True
            log("✅ 凭据已填入（fill force）")
        except Exception:
            log("⚠️ fill force失败，尝试键盘模拟...")

        # 方式2：JS注入值+触发事件
        if not filled:
            try:
                page.evaluate("""([u_sel, p_sel, u_val, p_val]) => {
                    const u = document.querySelector(u_sel);
                    const p = document.querySelector(p_sel);
                    if (u) {
                        u.focus();
                        u.value = u_val;
                        u.dispatchEvent(new Event('input', {bubbles: true}));
                        u.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    if (p) {
                        p.focus();
                        p.value = p_val;
                        p.dispatchEvent(new Event('input', {bubbles: true}));
                        p.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                }""", [u_sel, p_sel, XJTLU_USERNAME, XJTLU_PASSWORD])
                filled = True
                log("✅ 凭据已填入（JS注入）")
            except Exception as e:
                log(f"❌ JS注入也失败: {e}")
                return False

        # 等一下让表单处理完输入
        time.sleep(1)

        # 打印表单结构供调试
        try:
            form_info = page.evaluate("""() => {
                const form = document.querySelector('form');
                if (!form) return 'no form';
                const inputs = [...form.querySelectorAll('input, button')].map(el => ({
                    tag: el.tagName,
                    type: el.type,
                    name: el.name || '',
                    id: el.id || '',
                    value: el.type === 'password' ? '***' : (el.value || '').substring(0, 20)
                }));
                return JSON.stringify(inputs, null, 2);
            }""")
            log(f"📋 表单结构: {form_info[:800]}")
        except Exception:
            pass

        # 提交登录 — 多种方式依次尝试
        submit_btn = page.query_selector("button[type='submit']") or \
                    page.query_selector("input[type='submit']") or \
                    page.query_selector("button:has-text('登录')") or \
                    page.query_selector("button:has-text('Login')") or \
                    page.query_selector("input[name*='logon']") or \
                    page.query_selector("input[name*='submit']")

        submitted = False

        # 方式1：force=True 强制点击（绕过遮挡检查）
        if submit_btn:
            try:
                submit_btn.click(force=True, timeout=5000)
                submitted = True
                log("✅ 已强制点击提交按钮")
            except Exception:
                log("⚠️ force click失败，尝试其他方式...")

        # 方式2：JS直接触发click事件（在input上触发click，模拟真实点击）
        if not submitted:
            try:
                # SITS系统常用：点击登录按钮或用回车提交
                result = page.evaluate("""() => {
                    // 找所有可能的提交元素
                    const candidates = [
                        "input[type='submit']",
                        "button[type='submit']",
                        "input[name*='logon']",
                        "input[name*='LOGON']",
                        "button[name*='logon']",
                        "input[value*='Log']",
                        "input[value*='log']",
                        "input[value*='登录']",
                        "button:has-text('Log in')",
                        "a:has-text('Log in')",
                        "input[type='button']"
                    ];
                    for (const sel of candidates) {
                        const el = document.querySelector(sel);
                        if (el) { el.click(); return sel; }
                    }
                    return 'not_found';
                }""")
                if result != 'not_found':
                    submitted = True
                    log(f"✅ JS点击了: {result}")
                else:
                    log("⚠️ JS未找到提交元素")
            except Exception as e:
                log(f"⚠️ JS click失败: {e}")

        # 方式3：JS直接提交表单
        if not submitted:
            try:
                page.evaluate("""() => {
                    const form = document.querySelector('form');
                    if (form) { form.submit(); return 'submitted'; }
                    return 'no_form';
                }""")
                submitted = True
                log("✅ JS表单提交已执行")
            except Exception as e:
                log(f"⚠️ JS表单提交失败: {e}")

        # 方式4：键盘Tab到按钮再Enter
        if not submitted:
            try:
                page.keyboard.press("Tab")
                page.keyboard.press("Enter")
                submitted = True
                log("✅ 键盘Tab+Enter已执行")
            except Exception:
                log("❌ 所有提交方式均失败")
                return False

        page.wait_for_load_state("networkidle", timeout=20000)
        time.sleep(3)

        # 检查是否登录成功（URL 不再是登录页 siw_lgn）
        current_url = page.url
        log(f"📄 登录后URL: {current_url[:60]}")
        if "siw_lgn" not in current_url and "login" not in current_url.lower():
            log(f"✅ 登录成功！当前页面: {current_url[:50]}")
            return True
        else:
            # 检查是否有错误信息
            error = page.query_selector(".error") or page.query_selector(".alert-danger") or \
                    page.query_selector(".errorMessage") or page.query_selector(".msg-error")
            if error:
                log(f"❌ 登录失败: {error.inner_text()[:50]}")
            else:
                log("❌ 登录后仍在登录页")
            return False
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        return False


# ============ eBridge 结构化提取 ============
def extract_ebridge_bulletins(page):
    """提取eBridge首页公告栏的每条公告（标题+日期）"""
    log("📖 提取eBridge公告...")
    try:
        page.goto(EBRIDGE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)  # SPA需要时间渲染

        # 提取公告列表：eBridge首页有Bulletin区域
        bulletins = page.evaluate("""() => {
            const results = [];
            // 找所有包含日期的文本块（公告通常有标题+日期）
            const allElements = document.querySelectorAll('div, span, p, a, td, li');
            for (const el of allElements) {
                const text = el.innerText || '';
                // 公告特征：包含New标记或有日期格式
                if (text.includes('New') || text.match(/20\\d{2}[-/.]\\d{1,2}[-/.]\\d{1,2}/)) {
                    // 向上找父容器获取完整公告
                    let parent = el.closest('[class*=bulletin]') || el.closest('[class*=notice]') ||
                                 el.closest('[class*=announcement]') || el.parentElement;
                    if (parent) {
                        const ptext = parent.innerText.trim().substring(0, 200);
                        if (ptext.length > 10 && !results.includes(ptext)) {
                            results.push(ptext);
                        }
                    }
                }
            }
            // 如果上面没找到，直接抓所有可见文本块
            if (results.length === 0) {
                const blocks = document.querySelectorAll('div');
                for (const b of blocks) {
                    const t = b.innerText.trim();
                    if (t.length > 20 && t.length < 300 && t.includes('20')) {
                        if (!results.includes(t)) results.push(t);
                    }
                }
            }
            return results.slice(0, 20).join('\\n---\\n');
        }""")
        log(f"✅ eBridge公告: {len(bulletins)} 字符")
        return bulletins
    except Exception as e:
        log(f"⚠️ eBridge公告提取失败: {e}")
        return ""


def extract_ebridge_timetable(page):
    """提取eBridge课表：课程名+老师+时间+地点"""
    log("📖 提取eBridge课表...")
    try:
        # eBridge课表页面
        page.goto("https://ebridge.xjtlu.edu.cn/urd/sits.urd/run/siw_lgs",
                  wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)

        # 提取课表信息
        timetable = page.evaluate("""() => {
            const results = [];
            // 课表通常在table或grid中
            const tables = document.querySelectorAll('table');
            for (const table of tables) {
                const rows = table.querySelectorAll('tr');
                for (const row of rows) {
                    const cells = row.querySelectorAll('td, th');
                    const rowData = [];
                    for (const cell of cells) {
                        rowData.push(cell.innerText.trim());
                    }
                    if (rowData.length > 1) {
                        results.push(rowData.join(' | '));
                    }
                }
            }
            // 如果没table，抓所有文本块
            if (results.length === 0) {
                const blocks = document.querySelectorAll('div, span');
                for (const b of blocks) {
                    const t = b.innerText.trim();
                    if (t.length > 10 && t.length < 200 &&
                        (t.includes('Teacher') || t.includes('老师') || t.includes('Room') || t.includes('Week'))) {
                        if (!results.includes(t)) results.push(t);
                    }
                }
            }
            return results.slice(0, 50).join('\\n');
        }""")
        log(f"✅ eBridge课表: {len(timetable)} 字符")
        return timetable
    except Exception as e:
        log(f"⚠️ eBridge课表提取失败: {e}")
        return ""


# ============ LearningMall Core 日历事件（作业/考试截止） ============
def parse_ics_events(ics_text):
    """解析 Moodle 日历 ICS 文本为事件列表"""
    events = []
    current = None
    for line in ics_text.splitlines():
        line = line.rstrip("\r")
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current and current.get("uid") and current.get("summary"):
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, _, val = line.partition(":")
            if key == "UID":
                current["uid"] = val.strip()
            elif key == "SUMMARY":
                current["summary"] = val.strip()
            elif key == "DESCRIPTION":
                # 只取第一段说明（后续折行拼接太长）
                if not current.get("description"):
                    current["description"] = val.strip()[:200]
            elif key == "DTSTART":
                current["dtstart"] = val.strip()
            elif key == "DTEND":
                current["dtend"] = val.strip()
            elif key == "CATEGORIES":
                current["course"] = val.strip()
    return events


def ics_time_to_bj(dtstr):
    """ICS UTC 时间（20261009T140000Z）→ 北京时间 datetime"""
    try:
        dt = datetime.strptime(dtstr, "%Y%m%dT%H%M%SZ")
        return dt + timedelta(hours=8)
    except Exception:
        return None


# 课程代码 → 中文友好名（从 LearningMall 实际课程名简化）
COURSE_NAME_MAP = {
    "EAP043": "学术英语", "MTH026": "微积分", "MTH028": "线性代数",
    "CCT001": "马原", "CCT007": "自我管理", "CCT011": "毛概", "CCT012": "形势与政策",
    "SCI004": "科学基础", "PSP004": "新兴技术", "PHE001": "体育",
    "LIF": "生命科学", "LIFSCI004": "生命科学",
}


def short_course(cat):
    """CATEGORIES 'MTH026-2627-S1' → 'MTH026 微积分'"""
    code = (cat or "未知").split("-")[0]
    if code in COURSE_NAME_MAP:
        return f"{code} {COURSE_NAME_MAP[code]}"
    return code


def fmt_dt_cn(dt):
    """datetime → '10月9日(周五) 22:00'"""
    if not dt:
        return "时间待定"
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
    return f"{dt.month}月{dt.day}日({wd}) {dt.strftime('%H:%M')}"


def event_is_assignment(ev):
    """是否为作业/考试类事件（截止、开放、关闭）"""
    s = ev.get("summary", "").lower()
    return any(kw in s for kw in ["due", "opens", "closes", "quiz", "exam", "assignment"])


def fetch_ics_url(url):
    """GET 日历 URL，返回 ICS 文本或 None"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" in text:
            return text
    except Exception as e:
        log(f"⚠️ 日历URL请求失败: {e}")
    return None


def regenerate_lm_calendar_url(context, page):
    """日历 authtoken 失效时：SAML 登录 core → 填导出表单 → 返回新 URL"""
    try:
        log("🔄 重新生成 LearningMall 日历URL...")
        # uim SSO Cookie 已在 cookie_login 时加载，直接走 SAML 链
        page.goto(LM_SAML_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        logged = False
        for i in range(20):
            time.sleep(2)
            u = page.url
            if u.startswith("https://core.xjtlu.edu.cn") and "/auth/saml2" not in u:
                logged = True
                break
        if not logged:
            page.goto(LM_CORE_URL + "/my/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(5)
            logged = "login" not in page.url
        if not logged:
            log("❌ core SAML 登录失败，无法重新生成")
            return None
        log("✅ core 登录成功，填日历导出表单")
        page.goto(LM_CORE_URL + "/calendar/export.php", wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        page.check("input[name='events[exportevents]'][value='all']")
        page.check("input[name='period[timeperiod]'][value='recentupcoming']")
        time.sleep(1)
        page.click("input[name='generateurl']")
        time.sleep(6)
        raw = page.evaluate("() => document.documentElement.innerHTML")
        m = re.search(r"https[^\"'\s]*export_execute[^\"'\s]*", raw)
        if m:
            url = html_mod.unescape(m.group(0))
            log(f"✅ 新日历URL已生成")
            return url
        log("❌ 未抓到新生成的日历URL")
        return None
    except Exception as e:
        log(f"⚠️ 重新生成日历URL异常: {e}")
        return None


def get_lm_calendar_events(state, context=None, page=None):
    """获取 LearningMall Core 全部日历事件（作业/考试截止）"""
    url = state.get("lm_cal_url") or LM_CAL_DEFAULT_URL
    ics = fetch_ics_url(url)
    if ics is None and context is not None:
        # authtoken 失效 → 重新生成
        new_url = regenerate_lm_calendar_url(context, page)
        if new_url:
            state["lm_cal_url"] = new_url
            ics = fetch_ics_url(new_url)
    if ics is None:
        log("❌ 无法获取 LearningMall 日历")
        return None
    events = parse_ics_events(ics)
    log(f"✅ LearningMall 日历: {len(events)} 个事件")
    return events


def format_new_lm_events(events, known_uids):
    """格式化新增事件推送：按课程分组 + EAP 解锁专属提醒"""
    new_events = [ev for ev in events if ev["uid"] not in known_uids]
    if not new_events:
        return None
    # 按课程分组
    by_course, order = {}, []
    for ev in new_events:
        course = short_course(ev.get("course", "其他"))
        if course not in by_course:
            by_course[course] = []
            order.append(course)
        by_course[course].append(ev)
    lines = ["📢 **LearningMall 新动态**", ""]
    for course in order:
        lines.append(f"**📚 {course}**")
        for ev in by_course[course]:
            s = ev.get("summary", "")
            dt = ics_time_to_bj(ev.get("dtstart", ""))
            tstr = fmt_dt_cn(dt) if dt else "时间待定"
            sl = s.lower()
            if "due" in sl:
                action, emoji = "截止", "🚨"
            elif "closes" in sl:
                action, emoji = "关闭", "⏰"
            elif "opens" in sl:
                action, emoji = "开放", "📅"
            else:
                action, emoji = "安排", "📌"
            # 去掉尾部 " is due/open/closed" 让标题更干净
            title = re.sub(r"\s+is\s+(due|open|closed)\s*$", "", s, flags=re.I)
            lines.append(f"  {emoji} {title} · {action} {tstr}")
        lines.append("")
    # EAP 解锁专属提醒（任何 EAP 新事件 = 新任务解锁）
    if any((ev.get("course") or "").startswith("EAP") for ev in new_events):
        lines.append("✍️ **EAP 已解锁，尽快完成。**")
        lines.append("")
    lines.append(f"共 {len(new_events)} 项新动态")
    return "\n".join(lines)


def format_upcoming_assignments(events, days=7):
    """未来 N 天内截止/关闭的作业清单，按课程分组（用于每日推送）"""
    now_bj = datetime.utcnow() + timedelta(hours=8)
    today = now_bj.date()
    upcoming = []
    for ev in events or []:
        s = ev.get("summary", "").lower()
        if not ("due" in s or "closes" in s or "deadline" in s):
            continue
        dt = ics_time_to_bj(ev.get("dtstart", ""))
        if dt and today <= dt.date() <= today + timedelta(days=days):
            upcoming.append((dt, ev))
    if not upcoming:
        return ""
    upcoming.sort(key=lambda x: x[0])
    by_course, order = {}, []
    for dt, ev in upcoming:
        course = short_course(ev.get("course", "其他"))
        if course not in by_course:
            by_course[course] = []
            order.append(course)
        by_course[course].append((dt, ev))
    lines = ["", "📋 **未来 %d 天作业截止**" % days, ""]
    for course in order:
        lines.append(f"**📚 {course}**")
        for dt, ev in by_course[course]:
            s = ev.get("summary", "")
            title = re.sub(r"\s+is\s+(due|closed)\s*$", "", s, flags=re.I)
            days_left = (dt.date() - today).days
            if days_left == 0:
                when = "⚠️今天截止"
            elif days_left == 1:
                when = "明天截止"
            elif days_left <= 3:
                when = f"{days_left}天后截止"
            else:
                when = f"{days_left}天后"
            lines.append(f"  🚨 {title} · {fmt_dt_cn(dt)} · {when}")
        lines.append("")
    return "\n".join(lines)


# ============ LearningMall 结构化提取 ============
def extract_learningmall_courses(page):
    """提取LearningMall的选课列表和作业"""
    log("📖 提取LearningMall课程...")
    try:
        # LearningMall的My Course页面
        page.goto("https://www.learningmall.cn/zh/my/course", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        # 提取课程列表
        courses = page.evaluate("""() => {
            const results = [];
            // 课程通常在卡片或列表中
            const cards = document.querySelectorAll('[class*=course], [class*=Course], .card, .list-item, a[href*=course]');
            for (const card of cards) {
                const title = card.querySelector('h1, h2, h3, h4, .title, [class*=title]');
                const link = card.href || card.querySelector('a')?.href || '';
                if (title) {
                    results.push({
                        name: title.innerText.trim(),
                        url: link
                    });
                }
            }
            // 备选：抓所有链接中文课程名
            if (results.length === 0) {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    const t = a.innerText.trim();
                    if (t.length > 3 && t.length < 100 && a.href.includes('course')) {
                        results.push({name: t, url: a.href});
                    }
                }
            }
            return JSON.stringify(results.slice(0, 20));
        }""")

        course_list = json.loads(courses) if courses else []
        log(f"✅ LearningMall课程: {len(course_list)} 门")

        # 进入每门课提取作业
        assignments = []
        for course in course_list[:10]:  # 最多10门课
            course_url = course.get("url", "")
            course_name = course.get("name", "")
            if not course_url:
                continue
            try:
                log(f"  📂 检查课程: {course_name[:30]}")
                page.goto(course_url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)

                # 提取作业/截止日期
                assigns = page.evaluate("""() => {
                    const results = [];
                    // 找作业/assignment相关元素
                    const items = document.querySelectorAll('[class*=assignment], [class*=Assignment], [class*=deadline], [class*=homework], [class*=activity]');
                    for (const item of items) {
                        const text = item.innerText.trim().substring(0, 200);
                        if (text.length > 5) results.push(text);
                    }
                    // 备选：找包含日期的文本
                    if (results.length === 0) {
                        const blocks = document.querySelectorAll('div, span, td, li');
                        for (const b of blocks) {
                            const t = b.innerText.trim();
                            if (t.length > 10 && t.length < 200 &&
                                (t.match(/20\\d{2}[-/.]\\d{1,2}[-/.]\\d{1,2}/) || t.includes('Due') || t.includes('deadline') || t.includes('截止'))) {
                                if (!results.includes(t)) results.push(t);
                            }
                        }
                    }
                    return results.slice(0, 10).join('\\n');
                }""")

                if assigns:
                    assignments.append({
                        "course": course_name,
                        "assignments": assigns
                    })
                    log(f"    ✅ 找到作业信息")
            except Exception as e:
                log(f"    ⚠️ 课程页面访问失败: {e}")

        return course_list, assignments
    except Exception as e:
        log(f"⚠️ LearningMall提取失败: {e}")
        return [], []


# ============ 课表 + 校历（静态数据，用户提供） ============
def load_static_data():
    """加载课表与校历 JSON，返回 (timetable, calendar)"""
    try:
        with open(TIMETABLE_FILE, "r", encoding="utf-8") as f:
            timetable = json.load(f)
    except Exception as e:
        log(f"⚠️ 加载课表失败: {e}")
        timetable = {"courses": []}
    try:
        with open(CALENDAR_FILE, "r", encoding="utf-8") as f:
            calendar = json.load(f)
    except Exception as e:
        log(f"⚠️ 加载校历失败: {e}")
        calendar = {"semester1": []}
    return timetable, calendar


def get_current_week_entry(calendar, dt=None):
    """返回今天属于哪个校历周条目：{'semester','week','label','type','name','closed_dates'}
    找不到则返回 None"""
    today = (dt.date() if dt else datetime.now().date()).isoformat()
    for sem in ("semester1", "semester2"):
        for entry in calendar.get(sem, []):
            if entry["start"] <= today <= entry["end"]:
                return {**entry, "semester": sem}
    return None


def get_today_courses(timetable, week_entry, dt=None):
    """返回今天应上的课列表。
    - 闭校日返回 []
    - 课表按 weekday(1=周一..7=周日) + weeks 匹配
    返回 (is_closed, courses)"""
    now = dt or datetime.now()
    today_str = now.date().isoformat()
    # 闭校日判断
    closed = week_entry.get("closed_dates", []) if week_entry else []
    if today_str in closed or (week_entry and week_entry.get("type") == "holiday"):
        return True, []
    weekday = now.isoweekday()  # 1=周一
    week_num = week_entry.get("week") if week_entry else None
    courses = []
    for c in timetable.get("courses", []):
        if c["day"] != weekday:
            continue
        if week_num is not None and week_num not in c.get("weeks", []):
            continue
        courses.append(c)
    courses.sort(key=lambda x: x["start"])
    return False, courses


def format_today_courses(week_entry, closed, courses, dt=None):
    """生成今日课程推送文本"""
    now = dt or datetime.now()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.isoweekday() - 1]
    date_cn = f"{now.month}月{now.day}日 {weekday_cn}"
    if not week_entry:
        return f"📅 **{date_cn}**\n\n不在校历范围内。"
    label = week_entry.get("label", f"W{week_entry.get('week')}")
    name = week_entry.get("name", "")
    head = f"📅 **{date_cn}** · {label} {name}"
    if closed:
        return f"{head}\n\n🔕 今天是闭校日/假期，无课程安排。"
    if not courses:
        return f"{head}\n\n☕ 今天没有课，休息一下吧。"
    lines = [head, ""]
    for c in courses:
        emoji = {"lecture": "📚", "seminar": "💬", "tutorial": "✏️", "sport": "🏹"}.get(c.get("type", ""), "📖")
        lines.append(f"{emoji} **{c['start']}-{c['end']}** {c['name']}")
        lines.append(f"   🏫 {c.get('location', '待定')} · 👨‍🏫 {c.get('teacher', '待定')}")
    lines.append("")
    lines.append(f"共 {len(courses)} 节课")
    return "\n".join(lines)


# ============ 状态管理 ============
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ebridge": "", "learningmall": "", "first_run": True, "last_check": ""}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============ 内容对比 ============
def find_new_items(old_content, new_content):
    """对比新旧内容，找出新增的文本条目"""
    if not old_content or not new_content:
        return []
    old_lines = set(line.strip() for line in old_content.split("\n") if line.strip())
    new_lines = [line.strip() for line in new_content.split("\n") if line.strip()]
    new_items = []
    for line in new_lines:
        if line not in old_lines and len(line) > 8:
            # 标记重要性
            is_important = any(kw.lower() in line.lower() for kw in IMPORTANT_KEYWORDS)
            new_items.append({"text": line, "important": is_important})
    return new_items


def format_notification(source, items):
    """格式化飞书通知"""
    if not items:
        return None
    important = [i for i in items if i["important"]]
    normal = [i for i in items if not i["important"]]

    lines = [f"📡 **{source}** 发现 {len(items)} 条新内容\n"]

    if important:
        lines.append(f"🚨 **重要内容（{len(important)}条）：**")
        for item in important[:5]:
            lines.append(f"  • {item['text'][:60]}")
        lines.append("")

    if normal:
        lines.append(f"📋 **其他更新（{len(normal)}条）：**")
        for item in normal[:5]:
            lines.append(f"  • {item['text'][:60]}")
        if len(normal) > 5:
            lines.append(f"  • ...等 {len(normal)} 条")

    return "\n".join(lines)


# ============ 主流程 ============
def cookie_login(context, page):
    """用 Cookie 登录：先加载Cookie，通过SSO跳转eBridge创建SITS会话"""
    cookie_str = os.environ.get("EBRIDGE_COOKIES", "")
    if not cookie_str:
        return False
    try:
        cookies = json.loads(cookie_str)
        context.add_cookies(cookies)
        log(f"🍪 加载了 {len(cookies)} 个 Cookie")

        # 先访问uim确认SSO令牌有效
        page.goto("https://uim.xjtlu.edu.cn", wait_until="networkidle", timeout=30000)
        time.sleep(3)
        uim_url = page.url
        log(f"📄 uim页面: {uim_url[:60]}")

        # 如果uim还在登录页，Cookie无效
        if "login" in uim_url.lower():
            log("⚠️ SSO Cookie已过期")
            return False

        # SSO令牌有效，直接访问eBridge让它走SSO链（eBridge→uim→eBridge）
        log(f"🔗 访问eBridge触发SSO跳转链...")
        page.goto(EBRIDGE_URL, wait_until="domcontentloaded", timeout=30000)

        # SSO链有多重跳转+Processing，耐心等待
        for i in range(20):
            time.sleep(3)
            try:
                current_url = page.url
                # 成功标志：进入eBridge且不是登录页
                if ("ebridge" in current_url and "siw_lgn" not in current_url
                        and "login" not in current_url.lower()):
                    log(f"✅ Cookie登录成功！URL: {current_url[:60]}")
                    return True
                # 还在uim登录页说明Cookie失效
                if "uim.xjtlu" in current_url and "login" in current_url.lower():
                    log("⚠️ 被重定向回uim登录页，Cookie可能过期")
                    return False
                if i % 4 == 0:
                    log(f"⏳ SSO跳转链中... ({i*3+3}秒) URL: {current_url[:55]}")
            except Exception:
                continue

        log(f"⚠️ SSO链未完成，停在: {current_url[:50]}")
        return False
    except Exception as e:
        log(f"⚠️ Cookie登录失败: {e}")
        return False


def run():
    log("🚀 西浦网站监控启动")

    state = load_state()
    is_first_run = state.get("first_run", True)
    timetable, calendar = load_static_data()

    # LearningMall 日历事件（authtoken 自足认证，不依赖浏览器登录）
    lm_events = get_lm_calendar_events(state)

    # 今日课程（不依赖登录，即使浏览器失败也要推送；附带未来7天作业截止）
    week_entry = get_current_week_entry(calendar)
    closed, today_courses = get_today_courses(timetable, week_entry)
    today_push = format_today_courses(week_entry, closed, today_courses)
    today_push += format_upcoming_assignments(lm_events, days=7)
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_pushed = state.get("last_today_push", "")


    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        iphone = p.devices["iPhone 13"]
        context = browser.new_context(
            **iphone,
            locale="zh-CN"
        )
        page = context.new_page()

        # 1. 优先用 Cookie 登录（跳过 MFA）
        logged_in = cookie_login(context, page)

        # 2. Cookie失败则退回账密登录（可能触发 MFA 失败）
        if not logged_in and XJTLU_USERNAME and XJTLU_PASSWORD:
            # 先检查是否已在selfcare页面（Cookie实际已生效）
            try:
                if "selfcare" in page.url:
                    log("✅ 检测到已在selfcare页，Cookie有效！直接走eBridge SSO链")
                    page.goto(EBRIDGE_URL, wait_until="domcontentloaded", timeout=30000)
                    for i in range(15):
                        time.sleep(3)
                        u = page.url
                        if "ebridge" in u and "siw_lgn" not in u and "login" not in u.lower():
                            logged_in = True
                            log(f"✅ eBridge登录成功！URL: {u[:50]}")
                            break
                        if i % 4 == 0:
                            log(f"⏳ SSO链等待... URL: {u[:55]}")
            except Exception:
                pass

        if not logged_in and XJTLU_USERNAME and XJTLU_PASSWORD:
            log("🔄 尝试账密登录...")
            logged_in = sso_login(page)

        if not logged_in:
            # 彻底防打扰：每天最多1条 + 连续失败3次后完全静默
            fails = state.get("consecutive_fails", 0) + 1
            state["consecutive_fails"] = fails
            today = datetime.now().strftime("%Y-%m-%d")
            last_fail_day = state.get("last_fail_notify", "")
            msgs = []
            # 登录失败不阻塞今日课程（不依赖登录）
            if today_pushed != today_str:
                msgs.append(today_push)
                state["last_today_push"] = today_str
            # 已知事件入库，防止下次登录成功后重复推送全量
            if lm_events:
                state["lm_events"] = {ev["uid"]: 1 for ev in lm_events}
            if fails <= 3 and last_fail_day != today:
                msgs.append("⚠️ 西浦网站登录失败\n\n可能原因：Cookie过期或MFA验证。\n\n请在Mac上运行 export_cookies.py 重新导出Cookie并更新GitHub Secrets。\n\n为避免打扰，今日不再提醒。")
                state["last_fail_notify"] = today
            if msgs:
                feishu_send("\n\n".join(msgs))
            else:
                log(f"⏭️ 登录失败{fails}次，静默处理")
            # 保存状态再退出
            state["last_check"] = datetime.now().isoformat()
            save_state(state)
            browser.close()
            return

        notifications = []

        # 2. 提取eBridge公告
        ebridge_bulletins = extract_ebridge_bulletins(page)
        if ebridge_bulletins and not is_first_run:
            old_bulletins = state.get("ebridge_bulletins", "")
            new_items = find_new_items(old_bulletins, ebridge_bulletins)
            if new_items:
                msg = format_notification("eBridge公告", new_items)
                if msg:
                    notifications.append(msg)
        state["ebridge_bulletins"] = ebridge_bulletins

        # 3. LearningMall Core 日历事件：开头 GET 失败时这里用浏览器重新生成再试
        if lm_events is None:
            lm_events = get_lm_calendar_events(state, context, page)
        if lm_events:
            known_uids = set(state.get("lm_events", {}).keys())
            # 新事件推送（登录成功才会走到这里，首次运行不推）
            if not is_first_run:
                new_msg = format_new_lm_events(lm_events, known_uids)
                if new_msg:
                    notifications.append(new_msg)
            state["lm_events"] = {ev["uid"]: 1 for ev in lm_events}
            log(f"📚 事件总数 {len(lm_events)}，已知 {len(known_uids)}，新增 {len(lm_events) - len([u for u in known_uids if u in state['lm_events']])}")
        else:
            log("📭 无法获取日历事件")

        browser.close()

    # 3. 今日课程推送（每天一次；含未来7天作业截止，已在开头拼好）
    if today_pushed != today_str:
        notifications.append(today_push)
        state["last_today_push"] = today_str

    # 4. 发送飞书通知
    if notifications:
        combined = "\n\n".join(notifications)
        feishu_send(combined)
        log(f"📡 共发现 {len(notifications)} 条通知已发送")
    else:
        log("📭 无新内容")

    # 5. 保存状态
    state["first_run"] = False
    state["last_check"] = datetime.now().isoformat()
    save_state(state)
    log("✅ 监控完成")


if __name__ == "__main__":
    run()
