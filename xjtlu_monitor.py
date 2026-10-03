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

# 图书馆房间预定系统（Information Commons，走 trust → sso → uim esc-sso OAuth 链）
ROOM_URL = "https://roombookings.xjtlu.edu.cn"
IC_API = ROOM_URL + "/ic-web"

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


def saml_login_core(page):
    """SAML 登录 LM Core（完全复用 regenerate_lm_calendar_url 的等待逻辑）"""
    try:
        page.goto(LM_SAML_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        for i in range(20):
            time.sleep(2)
            u = page.url
            if u.startswith("https://core.xjtlu.edu.cn") and "/auth/saml2" not in u:
                return True
        # 备用：直接访问 /my/ 看是否登录
        page.goto(LM_CORE_URL + "/my/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        return "/auth" not in page.url and "login" not in page.url.lower()
    except Exception as e:
        log(f"❌ core SAML 登录异常: {e}")
        return False


def fetch_eap_progress(context, page):
    """抓取 EAP043 课程页面的作业活动列表（含未解锁），返回进度数据"""
    log("📖 抓取 EAP043 作业进度...")
    try:
        # 1. SAML 登录 LM Core（不依赖 URL 判断，直接走 SAML 链）
        if not saml_login_core(page):
            log("❌ 无法登录 LM Core，跳过 EAP 进度抓取")
            return None
        log("✅ LM Core 登录成功")

        # 2. 从 My Courses 页面找 EAP043 课程链接
        page.goto(LM_CORE_URL + "/my/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        course_link = page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="course/view.php"]');
            for (const a of links) {
                const t = (a.innerText || '').trim();
                if (t.toUpperCase().includes('EAP043') || t.toUpperCase().includes('EAP 043')) {
                    return a.href;
                }
            }
            return '';
        }""")
        if not course_link:
            log("❌ My Courses 页面未找到 EAP043 课程链接")
            return None
        log(f"✅ 找到 EAP043 课程: {course_link[:70]}")

        # 3. 进课程页面解析作业活动
        page.goto(course_link, wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        # 展开所有 section（Moodle 课程可能分节折叠）
        try:
            for btn in page.query_selector_all(".course-section-header, [data-toggle*='collapse']"):
                btn.click()
                time.sleep(0.3)
        except Exception:
            pass
        time.sleep(2)

        # Moodle 4.x 活动列表解析
        activities = page.evaluate("""() => {
            const acts = document.querySelectorAll('li.activity, .activity-item, [class*="activity-wrapper"]');
            const results = [];
            for (const a of acts) {
                const nameEl = a.querySelector('.instancename, .activity-name, .aalink .instancename, [class*="instancename"]');
                const name = nameEl ? nameEl.innerText.trim() : '';
                if (!name) continue;
                const isLocked = !!(a.querySelector('.availabilityinfo, [class*="restricted"], [class*="locked"]') ||
                                    a.className.includes('restricted'));
                const isCompleted = !!(a.querySelector('.completion_done, [class*="completion-passed"], [data-completionstate="1"]'));
                const isAssign = !!(a.querySelector('a[href*="mod/assign"], a[href*="mod/quiz"], a[href*="mod/forum"]') ||
                                    a.className.includes('assign') || a.className.includes('quiz'));
                results.push({name: name.slice(0, 60), locked: isLocked, completed: isCompleted, is_hw: isAssign});
            }
            return JSON.stringify(results);
        }""")
        items = json.loads(activities) if activities else []

        # 4. 备用：不管 items 多少，都抓 page_text 存档（下次完善解析用）
        try:
            page_text = page.evaluate("() => document.body.innerText.slice(0, 5000)")
        except Exception:
            page_text = ""

        # 5. 构建进度数据
        hw_items = [it for it in items if it.get("is_hw")] or items  # 优先作业类，否则全部
        total = len(hw_items)
        completed = len([it for it in hw_items if it.get("completed")])
        locked = len([it for it in hw_items if it.get("locked")])
        progress = {
            "course_url": course_link,
            "total": total,
            "completed": completed,
            "locked": locked,
            "items": [{"name": it["name"], "locked": it.get("locked", False),
                       "completed": it.get("completed", False)} for it in hw_items[:20]],
            "page_text": page_text,
            "last_check": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        log(f"✅ EAP进度: 共{total}项作业，已完成{completed}，未解锁{locked}")
        return progress
    except Exception as e:
        log(f"❌ EAP 进度抓取失败: {e}")
        return None


# ============ AMS 考勤系统 ============
# 机制（2026-10-02 实测）：
#   1. 带 uim SSO Cookie 访问 /studentpc/checkIn → 自动 SSO → localStorage 存 x-token
#   2. x-token 自足认证（header），纯 curl 可用 → 云函数可远程签到
#   3. 签到: GET /xjtlu/sign/qRCodeSign?code={签到码}&type=2 （老师公布的密码码，72小时内可补签）
#   4. 课表: GET /xjtlu/xjtlu-base-student-timetables/getStudentTimetable（未来3天+签到状态）
#   5. 出勤: GET /xjtlu/stuapi/my-module/list?type=1&academicYear=...&semester=...
AMS_URL = "https://ams.xjtlu.edu.cn"
AMS_CHECKIN_URL = AMS_URL + "/studentpc/checkIn"


def ams_semester_params():
    """按当前日期算学年学期参数（AMS 用短学年格式：2026/27；西浦：8月起 SEM1）"""
    now = datetime.now()
    if now.month >= 8:
        ay = f"{now.year}/{(now.year + 1) % 100:02d}"
        sem = "SEM1"
    elif now.month <= 1:
        ay = f"{now.year - 1}/{now.year % 100:02d}"
        sem = "SEM1"
    else:
        ay = f"{now.year - 1}/{now.year % 100:02d}"
        sem = "SEM2"
    # 注意 safe=""：必须把 / 转义成 %2F，否则 academicYear 参数会被路径截断
    return urllib.parse.quote(ay, safe=""), sem


def ams_course_label(code):
    """EAP043 → 'EAP043 学术英语'"""
    return short_course(code) if code in COURSE_NAME_MAP else (code or "未知课程")


def ams_login_get_token(page):
    """访问 AMS 走 SSO 自动登录，从 localStorage 抓 x-token
    注意：Actions 到西浦网络慢（LM SAML 实测 53 秒），等待上限 3 分钟"""
    log("🏫 登录 AMS 考勤系统...")
    try:
        page.goto(AMS_CHECKIN_URL, wait_until="domcontentloaded", timeout=90000)
        for i in range(90):
            time.sleep(2)
            try:
                u = page.url
                if "uim.xjtlu" in u and "login" in u.lower():
                    log("❌ AMS 登录被弹回 uim 登录页（SSO Cookie 可能过期）")
                    return None
                token = page.evaluate("() => localStorage.getItem('Token')")
                if token and len(str(token)) >= 16:
                    log(f"✅ AMS 登录成功，x-token 已获取 ({i * 2 + 2}秒)")
                    return token
                if i % 15 == 14:
                    log(f"⏳ AMS SSO 链等待中... ({i * 2 + 2}秒) URL: {u[:55]}")
            except Exception:
                continue
        log("❌ AMS 登录超时，未拿到 x-token")
        return None
    except Exception as e:
        log(f"❌ AMS 登录异常: {e}")
        return None


def ams_api_get(path, token):
    """AMS API GET（x-token 自足认证），成功返回 data"""
    try:
        req = urllib.request.Request(AMS_URL + path, headers={"x-token": token})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        if data.get("code") == 0:
            return data.get("data")
        log(f"⚠️ AMS API 非0返回: {str(data.get('message', ''))[:60]}")
    except Exception as e:
        log(f"⚠️ AMS API 请求失败: {e}")
    return None


def fetch_ams_data(page):
    """登录 AMS，返回 {'token', 'sessions', 'attendance'} 或 None"""
    token = ams_login_get_token(page)
    if not token:
        return None
    tt = ams_api_get("/xjtlu/xjtlu-base-student-timetables/getStudentTimetable", token)
    ay, sem = ams_semester_params()
    att = ams_api_get(
        f"/xjtlu/stuapi/my-module/list?type=1&academicYear={ay}&semester={sem}", token)
    if tt is None and att is None:
        return None
    # 课表 → {signInRecordId: session dict}
    sessions = {}
    for day in (tt or []):
        for s in day.get("timetableVoList", []):
            rid = s.get("signInRecordId")
            if rid:
                sessions[str(rid)] = {
                    "code": s.get("moduleCode", ""),
                    "title": s.get("moduleTitle", ""),
                    "group": s.get("moduleTypeGroup", ""),
                    "start": s.get("startTime", ""),
                    "time_text": s.get("moduleTime", ""),
                    "date": day.get("date", ""),
                    "room": s.get("room") or "",
                    "status": s.get("status"),
                }
    # 出勤统计（学年参数错误时后端会返回空 vos + 0.0%，做防护）
    attendance = None
    if att and att.get("vos"):
        modules = {}
        for m in att.get("vos", []):
            modules[m.get("moduleCode", "?")] = {
                "absences": m.get("absences", 0),
                "sign_hours": m.get("signClassHour", 0),
                "total_hours": m.get("classHour", 0),
                "att": m.get("courseAttendance", 100.0),
                "threshold": m.get("courseThresholdFlag", False),
            }
        attendance = {
            "overall": att.get("comprehensiveAttendance", ""),
            "threshold": att.get("thresholdFlag", False),
            "modules": modules,
        }
    log(f"✅ AMS 数据: {len(sessions)} 个课节，总出勤率 {attendance['overall'] if attendance else '?'}%")
    return {"token": token, "sessions": sessions, "attendance": attendance}


def ams_parse_start(start_str):
    """"2026-10-05 09:00:00" → datetime 或 None"""
    try:
        return datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def check_ams_changes(ams_new, state, is_first_run):
    """对比上次快照产生提醒。返回 (notifications 列表, updated_state_ams)"""
    notes = []
    old = state.get("ams") or {}
    reminded = set(old.get("reminded", []))
    new_sessions = ams_new.get("sessions", {})
    new_att = ams_new.get("attendance") or {}
    old_att = old.get("attendance") or {}
    old_modules = old_att.get("modules", {}) if old_att else {}
    old_overall = old_att.get("overall") if old_att else None

    if not is_first_run:
        # 1. 缺勤增长（高置信信号：absences 是老师课后确认的数字）
        for code, m in (new_att.get("modules") or {}).items():
            old_abs = old_modules.get(code, {}).get("absences", 0)
            if m["absences"] > old_abs:
                delta = m["absences"] - old_abs
                lines = [f"❗ AMS 新增缺勤记录\n\n"
                         f"{ams_course_label(code)}缺勤 +{delta}"
                         f"（累计 {m['absences']} 次，已签 {m['sign_hours']}/{m['total_hours']} 课时）"]
                old_pct = old_modules.get(code, {}).get("att")
                if old_pct is not None and old_pct != m["att"]:
                    lines.append(f"模块出勤率：{old_pct}% → {m['att']}%")
                lines.append("\n若实际有出勤，请课后尽快联系老师修改记录（老师可在 AMS 直接修正）。")
                notes.append("\n".join(lines))
        # 2. 总出勤率下降
        new_overall = new_att.get("overall")
        if (old_overall and new_overall
                and isinstance(new_overall, (int, float, str))
                and float(str(new_overall)) < float(str(old_overall))):
            has_absence_note = any("缺勤" in n for n in notes)
            if not has_absence_note:
                notes.append(f"📉 总出勤率变化：{old_overall}% → {new_overall}%")
        # 3. thresholdFlag 触发（低于学校阈值，升学籍警告级）
        if new_att.get("threshold") and not old_att.get("threshold"):
            bad = [f"{ams_course_label(c)}（{m['att']}%）"
                   for c, m in (new_att.get("modules") or {}).items() if m.get("threshold")]
            notes.append("🚨 出勤率触发学校阈值警告！\n\n"
                         + ("；".join(bad) if bad else "总出勤率已低于阈值")
                         + "\n\n请尽快联系 DA（Development Advisor）说明情况，必要时申请 Authorized Absence。")
        # 4. 签到提醒：今天将开课/正在上课的未签课节（每课节只提醒一次）
        now = datetime.now()
        for rid, s in new_sessions.items():
            if s.get("status") != 0 or rid in reminded:
                continue
            st = ams_parse_start(s.get("start", ""))
            if not st:
                continue
            # 开课前30分钟 ~ 开课后2小时内提醒一次
            if st - timedelta(minutes=30) <= now <= st + timedelta(hours=2):
                lines = [f"⏰ 别忘了签到\n\n{ams_course_label(s['code'])} · {s['time_text']}"
                         + (f" · {s['room']}" if s.get("room") else "")]
                lines.append("\n老师公布签到码后，直接回复「签到 码」即可远程签到（3秒完成）。\n"
                             "密码签到 72 小时内可补签，二维码则需现场扫。")
                notes.append("\n".join(lines))
                reminded.add(rid)

    # 更新 state 快照（token 供云函数远程签到使用）
    # reminded 只保留当前课表内已提醒过的课节，出窗口自动清理
    updated = {
        "token": ams_new.get("token", ""),
        "attendance": new_att,
        "sessions": new_sessions,
        "reminded": [rid for rid in reminded if rid in new_sessions],
        "last_check": datetime.now().isoformat(timespec="minutes"),
    }
    return notes, updated


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
            lines.append(f"  • ...等 {len(normal) - 5} 条")

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


# ============ 图书馆房间预定系统（Information Commons） ============

def _room_uim_fill_login(page):
    """roombookings 跳到 uim esc-sso 登录页时，填账密登录（选择器与 sso_login 同源）"""
    selectors = [
        ("input[name='username']", "input[name='password']"),
        ("input#username", "input#password"),
        ("input[type='text']", "input[type='password']"),
        ("input[placeholder*='帐号']", "input[placeholder*='密码']"),
        ("input[placeholder*='账号']", "input[placeholder*='密码']"),
    ]
    u = p = None
    for u_sel, p_sel in selectors:
        try:
            u = page.query_selector(u_sel)
            p = page.query_selector(p_sel)
        except Exception:
            continue
        if u and p:
            break
        u = p = None
    if not u or not p:
        return False
    try:
        u.fill(XJTLU_USERNAME)
        time.sleep(0.4)
        p.fill(XJTLU_PASSWORD)
        time.sleep(0.4)
        btn = (page.query_selector("button:has-text('登录')")
               or page.query_selector("button:has-text('Login')")
               or page.query_selector("button[type='submit']")
               or page.query_selector("input[type='submit']"))
        if btn:
            try:
                box = btn.bounding_box()
                if box:
                    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    time.sleep(0.2)
                    page.mouse.down()
                    time.sleep(0.1)
                    page.mouse.up()
                else:
                    btn.click(force=True)
            except Exception:
                p.press("Enter")
        else:
            p.press("Enter")
        log("🔑 房间系统 uim 登录表单已提交")
        return True
    except Exception as e:
        log(f"⚠️ uim 表单填写失败: {e}")
        return False


def _find_accid(obj):
    """在任意 JSON 结构里递归找 accId 字段"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("accid", "accno", "useraccid") and v:
                return str(v)
            r = _find_accid(v)
            if r:
                return r
    elif isinstance(obj, list):
        for it in obj:
            r = _find_accid(it)
            if r:
                return r
    return None


def fetch_roombookings_session(browser, main_context):
    """登录房间预定系统，抓 API session cookies + accId，存入 state['room']

    跳转链：roombookings → trust → sso.xjtlu → uim esc-sso（OAuth2）
    复用主 context 的 uim TGC，通常自动跳回；停在登录页则填账密。
    """
    captured = {}

    def _capture(resp):
        try:
            if "/ic-web/" in resp.url:
                path = resp.url.split("/ic-web/", 1)[1].split("?")[0]
                captured[path] = resp.json()
        except Exception:
            pass

    ctx = None
    try:
        ctx = browser.new_context(locale="zh-CN")  # 桌面 UA：IC 是 PC 系统
        ctx.add_cookies(main_context.cookies())
        page = ctx.new_page()
        page.on("response", _capture)
        log("🏛️ 登录图书馆房间预定系统...")
        page.goto(ROOM_URL, wait_until="domcontentloaded", timeout=45000)

        stable = 0
        filled_once = False
        for i in range(40):
            time.sleep(3)
            try:
                u = page.url
            except Exception:
                continue
            if "uim.xjtlu.edu.cn" in u and "login" in u.lower():
                if not filled_once and XJTLU_USERNAME and XJTLU_PASSWORD:
                    log(f"🔑 停在 uim 登录页，自动填入账密...")
                    filled_once = _room_uim_fill_login(page)
                continue
            if "roombookings" in u:
                stable += 1
                if stable >= 2:
                    log(f"✅ 房间系统就绪: {u[:60]}")
                    break
            else:
                stable = 0
                if i % 4 == 0:
                    log(f"⏳ 跳转链中... URL: {u[:70]}")
                # 卡在 trust/sso 超过 45 秒 → 重新触发一次跳转链
                if i in (15, 30):
                    log(f"🔄 跳转链卡住，重新访问 {ROOM_URL}")
                    try:
                        page.goto(ROOM_URL, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass

        # 验证 API 登录态（roomMenu 无需登录也可能返回？以 reserve/count 为准）
        try:
            resp = page.request.get(f"{IC_API}/roomMenu", timeout=20000)
            data = resp.json()
        except Exception as e:
            log(f"⚠️ roomMenu 请求失败: {e}")
            data = {}
        if data.get("code") != 0:
            log(f"❌ 房间系统 API 未登录: {str(data.get('message'))[:60]}")
            return None

        # 抓 roombookings/trust/sso 三个域的 session cookies
        all_cookies = ctx.cookies()
        keep_domains = ("roombookings", "trust.xjtlu", "sso.xjtlu")
        room_cookies = [c for c in all_cookies
                        if any(d in (c.get("domain") or "") for d in keep_domains)
                        and c.get("name") not in ("lang", "language")]  # 语言 cookie 无用，省体积
        # accId：先翻捕获的响应，再主动调 getOwerUser
        acc_id = None
        for body in captured.values():
            acc_id = _find_accid(body)
            if acc_id:
                break
        user_name = None
        for path, body in captured.items():
            if isinstance(body, dict):
                d = body.get("data")
                if isinstance(d, dict) and d.get("name"):
                    user_name = str(d["name"])
                    break
        if not acc_id:
            for ep in ("/authUser/getOwerUser", "/user/getUserInfo", "/authUser/getUser"):
                try:
                    r2 = page.request.get(IC_API + ep, timeout=15000)
                    j2 = r2.json()
                    acc_id = _find_accid(j2)
                    if acc_id:
                        log(f"🆔 accId 来自 {ep}")
                        if not user_name and isinstance(j2.get("data"), dict):
                            user_name = j2["data"].get("name")
                        break
                except Exception:
                    continue

        log(f"✅ 房间系统登录成功：{len(room_cookies)} 个 cookie，accId={'有' if acc_id else '未获取'}")
        return {
            "cookies": room_cookies,
            "accId": acc_id or "",
            "name": user_name or "",
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as e:
        log(f"⚠️ 房间系统登录异常: {e}")
        return None
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass


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
                if XJTLU_USERNAME and XJTLU_PASSWORD:
                    msgs.append("⚠️ 西浦网站登录失败\n\nCookie 已过期，系统已自动尝试账密登录但未成功。\n\n可能原因：SSO 密码已更改，或网络波动。\n\n请检查 GitHub Secrets 中的 XJTLU_USERNAME / XJTLU_PASSWORD 是否正确。\n\n为避免打扰，今日不再提醒。")
                else:
                    msgs.append("⚠️ 西浦网站登录失败\n\nCookie 已过期，且未配置账密自动登录。\n\n请在 GitHub Secrets 设置 XJTLU_USERNAME 和 XJTLU_PASSWORD 实现自动登录，或手动更新 EBRIDGE_COOKIES。\n\n为避免打扰，今日不再提醒。")
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

        # 4. EAP043 作业进度（浏览器在，顺手抓课程页面）
        if logged_in:
            eap = fetch_eap_progress(context, page)
            if eap:
                # EAP 新作业解锁检测（对比上次）
                old_eap = state.get("eap_progress") or {}
                if old_eap and old_eap.get("total") and eap.get("total") and \
                   eap.get("locked", 0) < old_eap.get("locked", 0):
                    notifications.append("✍️ **EAP 新作业已解锁！** 请查看 EAP043 课程页面尽快完成。")
                state["eap_progress"] = eap

        # 5. AMS 考勤系统（缺勤检测/出勤率/签到提醒；token 存 state 供云函数远程签到）
        #    用桌面 UA 独立 context：AMS 是 /studentpc/ 页面，iPhone UA 可能被跳转/渲染异常
        ams = None
        if logged_in:
            try:
                ams_ctx = browser.new_context(locale="zh-CN")
                ams_ctx.add_cookies(context.cookies())
                ams_page = ams_ctx.new_page()
                ams = fetch_ams_data(ams_page)
                ams_ctx.close()
            except Exception as e:
                log(f"⚠️ AMS 桌面 context 异常: {e}")
                ams = None
        if ams:
            ams_notes, ams_state = check_ams_changes(ams, state, is_first_run)
            notifications.extend(ams_notes)
            state["ams"] = ams_state
            att = ams_state.get('attendance') or {}
            log(f"🏫 AMS: 出勤率 {att.get('overall', '?')}%"
                f"，课节 {len(ams_state['sessions'])} 个，提醒 {len(ams_notes)} 条")
        else:
            log("📭 AMS 数据未获取（登录或接口异常），跳过")

        # 6. 图书馆房间预定系统（抓 session cookie 存 state，供云函数查房/订房）
        if logged_in:
            room = fetch_roombookings_session(browser, context)
            if room:
                state["room"] = room
                log(f"🏛️ 房间 session 已更新（cookie {len(room['cookies'])} 个）")
            else:
                # 登录失败保留旧 session（cookie 可能仍有效）
                old = state.get("room") or {}
                state["room"] = old
                log(f"📭 房间系统本次未登录成功，保留旧 session（{len(old.get('cookies') or [])} cookie）")

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
