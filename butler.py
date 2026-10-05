#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 邮件管家 v2.0 — GitHub Actions 版
=====================================
升级内容：
  1. 读取邮件全文，提取时间/地点/主讲人/报名等核心信息
  2. 生成中文摘要，通过飞书机器人推送到手机
  3. 用户在飞书 App 中回复「批准」「跳过」即可管理日历
  4. 全程手机操作，无需打开电脑
"""
import json
import os
import re
import sys
import html as html_mod
import hashlib
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
# 状态数据存放私有数据仓库（GitHub Actions 注入 DATA_DIR），本地调试回退 .secrets/
SECRETS_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, ".secrets"))
STATE_FILE = os.path.join(SECRETS_DIR, "state.json")
PENDING_FILE = os.path.join(SECRETS_DIR, "pending.json")
CONFIRMED_FILE = os.path.join(SECRETS_DIR, "confirmed.json")
# v40c: 与云函数 webhook 共享的消息级去重表（云函数处理完用户消息后写入 ids，
# butler 兜底拉群消息前先过滤，杜绝同一条消息被两条路径各执行一次）
PROCESSED_MSGS_FILE = os.path.join(SECRETS_DIR, "feishu_processed_msgs.json")
# ICS 文件名带随机token（防陌生人猜测订阅链接）
ICS_FILENAME = os.environ.get("ICS_FILENAME", "calendar.ics")
ICS_FILE = os.path.join(BASE, "docs", ICS_FILENAME)

# 环境变量（GitHub Actions Secrets）
REFRESH_TOKEN = os.environ.get("OUTLOOK_REFRESH_TOKEN", "")
CLIENT_ID = os.environ.get("OUTLOOK_CLIENT_ID", "9199bf20-a13f-4107-85dc-02114787ef48")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BARK_KEY = os.environ.get("BARK_KEY", "")
BARK_URL = os.environ.get("BARK_URL", "https://api.day.app")

ORIGIN = "https://outlook.office.com"
TZ = "China Standard Time"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0 Safari/537.36")

FEISHU_BASE = "https://open.feishu.cn"

CONFIG = {
    "poll_interval_minutes": 5,
    "activity_keywords": [
        "讲座","分享会","workshop","lecture","招聘会","宣讲会","career fair","论坛",
        "比赛","竞赛","competition","考试","exam","deadline","截止","报名","注册",
        "register","sign up","活动","event","联赛","演出","音乐会","concert",
        "培训","training","课程","峰会","summit","大会","conference","嘉年华",
        "carnival","游泳","archery","射箭","调研","问卷","survey","答疑",
        "swimming","art","demand","fresher","导航","出海","供应链"
    ],
}


def log(msg, level="INFO"):
    safe = re.sub(r'1\.[A-Za-z0-9_\-]{20,}', '[REDACTED]', str(msg))
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {safe}"
    print(line, flush=True)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def strip_html(html_text):
    """HTML 转纯文本"""
    t = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL)
    t = re.sub(r'<style[^>]*>.*?</style>', '', t, flags=re.DOTALL)
    t = re.sub(r'<br\s*/?>', '\n', t)
    t = re.sub(r'</p>', '\n', t)
    t = re.sub(r'</div>', '\n', t)
    t = re.sub(r'<tr[^>]*>', '\n', t)
    t = re.sub(r'<td[^>]*>', ' | ', t)
    t = re.sub(r'<th[^>]*>', ' | ', t)
    t = re.sub(r'<li[^>]*>', '\n• ', t)
    t = re.sub(r'<[^>]+>', '', t)
    t = html_mod.unescape(t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    t = re.sub(r'[ \t]+', ' ', t)
    return t.strip()


# ================ Outlook 认证 ================
def get_access_token():
    body = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
        "scope": "https://outlook.office.com/.default offline_access",
        "client_info": "1",
    }).encode()
    req = urllib.request.Request(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Origin": ORIGIN, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read().decode())
    access = tok["access_token"]
    # v23: 把 access token 存到 state.json，供腾讯云函数直接读取（云函数 IP 可能被
    # 微软条件访问策略拒绝刷新，读现成的绕开限制；token 1 小时有效，本流程每 5 分钟刷新）
    try:
        import time as _t
        st = load_json(STATE_FILE, {})
        st["outlook_access"] = {
            "token": access,
            "expires_at": _t.time() + tok.get("expires_in", 3600) - 300,
        }
        save_json(STATE_FILE, st)
    except Exception as e:
        log(f"⚠️ outlook access token 存档失败: {e}")
    return access


# ================ Outlook REST API ================
class Outlook:
    def __init__(self, token):
        self.token = token

    def _call(self, method, path, payload=None):
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "application/json", "Origin": ORIGIN, "User-Agent": UA}
        data = json.dumps(payload).encode() if payload else None
        if data:
            headers["Content-Type"] = "application/json"
        url = "https://outlook.office.com" + urllib.parse.quote(path, safe="/?&%=$")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw.decode()) if raw else None

    def new_messages(self, since_utc, top=30):
        filt = f"ReceivedDateTime gt {since_utc}"
        q = (f"/api/v2.0/me/messages?$filter={urllib.parse.quote(filt)}"
             f"&$top={top}&$select=Id,Subject,From,ReceivedDateTime,BodyPreview")
        return self._call("GET", q).get("value", [])

    def get_body(self, msg_id):
        """获取邮件完整正文（HTML转纯文本）"""
        safe_id = urllib.parse.quote(msg_id, safe="")
        q = f"/api/v2.0/me/messages/{safe_id}?$select=Body"
        resp = self._call("GET", q)
        html_content = resp.get("Body", {}).get("Content", "")
        return strip_html(html_content)[:3000] if html_content else ""

    def create_event(self, subject, start_dt, end_dt, body="", all_day=False):
        if all_day:
            ev = {"Subject": subject,
                  "Start": {"DateTime": start_dt.strftime("%Y-%m-%dT00:00:00"), "TimeZone": TZ},
                  "End": {"DateTime": (end_dt + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00"), "TimeZone": TZ},
                  "Body": {"ContentType": "Text", "Content": body}, "IsAllDay": True}
        else:
            ev = {"Subject": subject,
                  "Start": {"DateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "TimeZone": TZ},
                  "End": {"DateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "TimeZone": TZ},
                  "Body": {"ContentType": "Text", "Content": body}}
        return self._call("POST", "/api/v2.0/me/events", ev)

    def upcoming_events(self, days=14, top=50):
        """查询未来N天的日历事件（按开始时间排序，含非管家添加的）"""
        q = f"/api/v2.0/me/events?$top={top}&$select=Id,Subject,Start"
        events = self._call("GET", q).get("value", [])
        today = datetime.now().strftime("%Y-%m-%d")
        limit = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        upcoming = [e for e in events
                    if today <= (e.get("Start", {}).get("DateTime", "") or "9999")[:10] <= limit]
        upcoming.sort(key=lambda e: e.get("Start", {}).get("DateTime", ""))
        return upcoming

    def delete_event(self, event_id):
        """删除日历事件（不可恢复）"""
        safe_id = urllib.parse.quote(event_id, safe="")
        return self._call("DELETE", f"/api/v2.0/me/events/{safe_id}")

    def create_subscription(self, notification_url):
        """创建 Outlook 推送订阅，新邮件实时通知"""
        expiration = (datetime.now(timezone.utc) + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {
            "@odata.type": "#Microsoft.OutlookServices.PushSubscription",
            "Resource": "https://outlook.office.com/api/v2.0/me/messages",
            "ChangeType": "Created",
            "NotificationURL": notification_url,
            "ClientState": "mailbutler",
            "ExpirationDateTime": expiration,
        }
        return self._call("POST", "/api/v2.0/me/subscriptions", body)


# ================ 邮件重要性分类 ================
IMPORTANCE_KEYWORDS = {
    "high": ["考试", "exam", "deadline", "截止", "ddl", "紧急", "urgent",
             "重要", "important", "final", "期末", "补考", "退课", "选课",
             "取消", "停课", "调课", "必读", "must read", "action required"],
    "medium": ["讲座", "lecture", "workshop", "培训", "活动", "event",
               "报名", "register", "比赛", "竞赛", "招聘", "career"],
    "low": ["问卷", "survey", "newsletter", "通知", "reminder", "校报",
            "social", "社团", "招新"],
}
IMPORTANCE_EMOJI = {"high": "🚨", "medium": "📬", "low": "📋"}


def classify_importance(subject, body_text):
    text = (subject + " " + body_text).lower()
    for level in ["high", "medium", "low"]:
        for kw in IMPORTANCE_KEYWORDS[level]:
            if kw.lower() in text:
                return level
    return "medium"


# ================ 活动识别 ================
def is_activity_email(subject, body_preview):
    text = ((subject or "") + " " + (body_preview or "")).lower()
    return any(kw.lower() in text for kw in CONFIG["activity_keywords"])


DATE_PATTERNS = [
    (re.compile(r"(20\d{2})[年.\-/](\d{1,2})[月.\-/](\d{1,2})"), "ymd"),
    (re.compile(r"(\d{1,2})月(\d{1,2})日"), "cn"),
    (re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})", re.I), "en"),
    (re.compile(r"(\d{1,2})/(\d{1,2})"), "md"),
]


def extract_dates(text):
    now = datetime.now()
    found = []
    for pat, kind in DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                if kind == "ymd":
                    d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                elif kind == "cn":
                    d = datetime(now.year, int(m.group(1)), int(m.group(2)))
                elif kind == "en":
                    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
                    d = datetime(now.year, months[m.group(1)[:3].lower()], int(m.group(2)))
                elif kind == "md":
                    d = datetime(now.year, int(m.group(1)), int(m.group(2)))
                if d < now - timedelta(days=30):
                    d = d.replace(year=now.year + 1)
                if d not in found and now - timedelta(days=3) <= d <= now + timedelta(days=366):
                    found.append(d)
            except (ValueError, AttributeError, IndexError, KeyError):
                continue
    return sorted(found)


# ================ 时间提取 ================
def extract_time_range(text):
    """从文本中提取起止时间，返回 (start_hour, start_min, end_hour, end_min) 或 None"""
    # 14:00-16:00, 14:00~16:00, 14:00至16:00
    m = re.search(r'(\d{1,2}):(\d{2})\s*[-–~至到]\s*(\d{1,2}):(\d{2})', text)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    # 下午2:00-4:00, 上午9:00-11:30, 下午2点-4点
    m = re.search(r'(上午|下午)(\d{1,2})[:：点](\d{2})?\s*[-–~至到]\s*(上午|下午)?(\d{1,2})[:：点](\d{2})?', text)
    if m:
        sh = int(m.group(2))
        if m.group(1) == '下午' and sh < 12:
            sh += 12
        sm = int(m.group(3)) if m.group(3) else 0
        eh = int(m.group(5))
        ampm_end = m.group(4) or m.group(1)
        if ampm_end == '下午' and eh < 12:
            eh += 12
        em = int(m.group(6)) if m.group(6) else 0
        return (sh, sm, eh, em)
    # 2pm-4pm, 2:00pm-4:00pm, 2 PM - 4 PM
    m = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*[-–~至到]\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)', text, re.I)
    if m:
        sh = int(m.group(1))
        if m.group(3).lower() == 'pm' and sh < 12:
            sh += 12
        sm = int(m.group(2)) if m.group(2) else 0
        eh = int(m.group(4))
        if m.group(7).lower() == 'pm' and eh < 12:
            eh += 12
        em = int(m.group(5)) if m.group(5) else 0
        return (sh, sm, eh, em)
    # 单个时间：14:00开始, 9:00
    m = re.search(r'(\d{1,2}):(\d{2})\s*(?:开始|start|open)', text, re.I)
    if m:
        sh, sm = int(m.group(1)), int(m.group(2))
        return (sh, sm, sh + 1, sm)  # 默认1小时
    return None


# ================ 邮件摘要生成 ================
FIELD_PATTERNS = {
    "time": [
        r'(?:时间|日期|Time|Date|When)[：:]\s*(.+?)(?:\n|$)',
        r'(\d{1,2}月\d{1,2}日[（(].*?[)）]\s*\d{1,2}:\d{2}[-–~至到]\d{1,2}:\d{2})',
    ],
    "location": [
        r'(?:地点|位置|Location|Venue|Where|地址)[：:]\s*(.+?)(?:\n|$)',
    ],
    "speaker": [
        r'(?:主讲人|主讲|嘉宾|演讲者|Speaker|Presenter|主讲嘉宾)[：:]\s*(.+?)(?:\n|$)',
    ],
    "registration": [
        r'(?:报名方式|报名|注册|Register|Sign up|RSVP|参与方式|扫描|扫码)[：:]*\s*(.+?)(?:\n|$)',
    ],
    "deadline": [
        r'(?:截止时间|截止|Deadline|DDL|报名截止|截止日期)[：:]\s*(.+?)(?:\n|$)',
    ],
}


def extract_field(field_name, text):
    """从邮件正文中提取指定字段（时间/地点/主讲人/报名/截止）"""
    for pattern in FIELD_PATTERNS.get(field_name, []):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            value = m.group(1).strip()
            # 清理尾部噪音
            value = re.sub(r'\s{2,}', ' ', value)
            value = value.rstrip('。，；,;')
            if len(value) > 1 and len(value) < 200:
                return value
    return ""


def generate_summary(subject, body_text, sender, date_txt):
    """从邮件全文生成中文摘要"""
    parts = [f"📌 {subject}"]

    # 时间：优先从正文提取，其次用 date_txt
    time_info = extract_field("time", body_text)
    if time_info:
        parts.append(f"📅 时间：{time_info}")
    elif date_txt and date_txt != "时间待定":
        parts.append(f"📅 时间：{date_txt}")

    location = extract_field("location", body_text)
    if location:
        parts.append(f"📍 地点：{location}")

    speaker = extract_field("speaker", body_text)
    if speaker:
        parts.append(f"🎤 主讲人：{speaker}")

    reg = extract_field("registration", body_text)
    if reg:
        parts.append(f"📝 报名：{reg}")

    deadline = extract_field("deadline", body_text)
    if deadline:
        parts.append(f"⏰ 截止：{deadline}")

    parts.append(f"👤 来源：{sender}")

    return "\n".join(parts)


# ================ 飞书机器人 ================
def get_feishu_token():
    """获取飞书 tenant_access_token"""
    url = f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    return data.get("tenant_access_token", "")


def feishu_get_chat_id(state):
    """获取与用户的聊天 chat_id（首次自动发现，后续从 state 读取）"""
    chat_id = state.get("feishu_chat_id")
    if chat_id:
        return chat_id

    token = get_feishu_token()
    url = f"{FEISHU_BASE}/open-apis/im/v1/chats?page_size=20"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())

    items = data.get("data", {}).get("items", [])
    # 优先选 p2p（单聊）
    for chat in items:
        if chat.get("chat_mode") == "p2p":
            state["feishu_chat_id"] = chat["chat_id"]
            return chat["chat_id"]
    # 其次选第一个群
    if items:
        state["feishu_chat_id"] = items[0]["chat_id"]
        return items[0]["chat_id"]
    return None


def feishu_send(text):
    """通过飞书机器人发送消息"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        log("[飞书未配置] 跳过推送")
        return False
    try:
        token = get_feishu_token()
        state = load_json(STATE_FILE, {"last_check": None, "processed": [], "feishu_processed": []})
        chat_id = feishu_get_chat_id(state)
        if not chat_id:
            log("[飞书] 未找到聊天，请先在飞书中给机器人发一条消息", "WARN")
            return False
        save_json(STATE_FILE, state)

        url = f"{FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = json.dumps({
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        if resp.get("code") == 0:
            log(f"✅ 飞书推送成功")
            return True
        else:
            log(f"飞书推送失败: {resp.get('msg', '')}", "WARN")
            return False
    except Exception as e:
        log(f"飞书推送异常: {e}", "WARN")
        return False


def feishu_read_commands(state):
    """读取飞书中用户发送的最近消息，返回命令列表"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return []

    import time as _time
    data = None
    for attempt in range(3):
        try:
            token = get_feishu_token()
            chat_id = feishu_get_chat_id(state)
            if not chat_id:
                return []

            url = (f"{FEISHU_BASE}/open-apis/im/v1/messages"
                   f"?container_id_type=chat&container_id={chat_id}"
                   f"&sort_type=ByCreateTimeDesc&page_size=20")
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            break
        except Exception as e:
            if attempt < 2:
                _time.sleep(2)
                continue
            log(f"飞书读取消息异常: {e}", "WARN")
            return []

    if data is None:
        return []

    commands = []
    processed = state.setdefault("feishu_processed", [])
    # v40c: 读共享已处理表（云函数 webhook 处理过的消息在这里）；
    # 文件不存在/为空属正常初始态（云函数还没处理过任何消息），按空表继续兜底
    shared = load_json(PROCESSED_MSGS_FILE, {"ids": []})
    shared_ids = set(shared.get("ids") or [])
    shared_dirty = False
    _now_ms = _time.time() * 1000
    for msg in data.get("data", {}).get("items", []):
        msg_id = msg.get("message_id", "")
        if msg_id in processed or msg_id in shared_ids:
            continue
        # 只处理文本消息
        if msg.get("msg_type") != "text":
            continue
        # 只处理用户发的消息（过滤机器人自己发的）
        sender_info = msg.get("sender", {})
        if sender_info.get("sender_type", "") != "user":
            processed.append(msg_id)
            continue
        # v40c: 发出不到 2 分钟的新消息留给 webhook 秒级路径处理，本轮先跳过
        # （不标记已处理，下轮再看；webhook 挂了 2 分钟后由这里兜底）
        try:
            ct = int(msg.get("create_time", "0"))
            if ct and _now_ms - ct < 120_000:
                continue
        except (ValueError, TypeError):
            pass
        # 解析消息内容
        content = json.loads(msg.get("body", {}).get("content", "{}"))
        text = content.get("text", "").strip()
        if text:
            commands.append({"text": text, "msg_id": msg_id})
            processed.append(msg_id)
            shared_ids.add(msg_id)
            shared_dirty = True

    # 只保留最近500条已处理记录
    state["feishu_processed"] = processed[-500:]
    if shared_dirty:
        shared["ids"] = list(shared_ids)[-500:]
        shared["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_json(PROCESSED_MSGS_FILE, shared)
    return commands


WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def norm_subject(s):
    """剥邮件回复/转发前缀（RE:/FW:/Fwd:/答复:/转发:，支持多层嵌套）。
    v40c: 学校导师常用"回复"形式重发同一封活动邀请（subject 带 RE: 前缀），
    精确匹配会把变体当成新活动重复推送——规范化后比对，变体不再入队。"""
    prev = None
    s = s or ""
    while prev != s:
        prev = s
        s = re.sub(r"^\s*(re\s*[:：]\s*|fw\s*[:：]\s*|fwd\s*[:：]\s*|答复\s*[:：]\s*|转发\s*[:：]\s*)",
                   "", s, flags=re.IGNORECASE)
    return s.strip()


def fmt_date(date_str):
    """'2026-10-14' → '10月14日·周三'"""
    if not date_str:
        return "时间待定"
    try:
        y, m, d = map(int, str(date_str).split("-")[:3])
        return f"{m}月{d:02d}日·{WEEKDAYS[datetime(y, m, d).weekday()]}"
    except Exception:
        return str(date_str)


def parse_month_day(text):
    """解析 '10月8日'/'10月8'/'10-08'/'10/8' → (10,8)；无法解析返回 None"""
    m = re.search(r'(\d{1,2})月(\d{1,2})日?', text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r'(\d{1,2})[-/](\d{1,2})', text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def item_month_day(item):
    """管家活动 item 的首日期 (月,日)，无日期返回 None"""
    dates = item.get("dates") or []
    if not dates:
        return None
    try:
        parts = dates[0].split("-")
        return (int(parts[1]), int(parts[2]))
    except Exception:
        return None


def do_cancel_item(item, confirmed_items):
    """从管家日历移除该活动并回复"""
    d = (item.get("dates") or [""])[0]
    confirmed_items.remove(item)
    feishu_send(
        f"🗑️ 已取消：{fmt_date(d)} {item['subject'][:40]}\n\n"
        f"iPhone日历同步后将自动消失。\n"
        f"Outlook的📮标记保留，彻底删除请用「日程」+「删除」。"
    )
    log(f"用户取消活动: {item['subject'][:40]}")


def do_delete_event(outlook, entry, confirmed_items, outlook_map, state):
    """从 Outlook 日历删除日程并同步管家日历"""
    event_id, event_subject = entry["id"], entry.get("subject", "")
    try:
        outlook.delete_event(event_id)
        for item in confirmed_items[:]:
            if item.get("subject", "")[:40] in event_subject:
                confirmed_items.remove(item)
        for k, v in list(outlook_map.items()):
            if v.get("id") == event_id:
                del outlook_map[k]
        state["outlook_map"] = outlook_map
        feishu_send(f"🗑️ 已从Outlook日历删除：\n\n📌 {event_subject[:45]}")
        log(f"用户删除Outlook日程: {event_subject[:40]}")
    except Exception as e:
        feishu_send(f"⚠️ 删除失败: {e}")
        log(f"删除日程失败: {e}", "WARN")


def process_commands(commands, pending, confirmed, state, outlook):
    """处理用户在飞书中发送的命令"""
    if not commands:
        return

    pending_items = pending.setdefault("items", [])
    confirmed_items = confirmed.setdefault("items", [])

    for cmd in commands:
        text = cmd["text"].lower().strip()

        if text in ("帮助", "help", "?", "？"):
            feishu_send(
                "🤖 AI邮件管家 · 帮助\n\n"
                "📋「列表」待确认活动\n"
                "✅「批准 [N]」加入日历\n"
                "⏭️「跳过 [N]」忽略活动\n"
                "📊「日历」管家已加入的活动\n"
                "🗑️「取消」查看可取消清单\n"
                "📅「日程」Outlook全部日程\n"
                "❌「删除」查看可删除清单\n\n"
                "💡 取消/删除支持三种指定：\n"
                "   按编号：取消 1\n"
                "   按日期：取消 10月14\n"
                "   按名称：取消 生涯\n\n"
                "💡 取消=仅iPhone订阅日历移除\n"
                "💡 删除=Outlook日历彻底移除"
            )

        elif text in ("列表", "list", "pending", "待确认"):
            active = [i for i in pending_items if i.get("status") == "pending"]
            if not active:
                feishu_send("📋 当前没有待确认的活动。")
            else:
                lines = ["📋 待确认活动列表：\n"]
                for idx, item in enumerate(active, 1):
                    lines.append(f"{idx}. {item['subject'][:50]}")
                    if item.get("date_txt") and item["date_txt"] != "时间待定":
                        lines.append(f"   📅 {item['date_txt']}")
                lines.append("\n回复「批准 N」加入日历 | 回复「跳过 N」忽略")
                feishu_send("\n".join(lines))

        elif text in ("日历", "calendar", "已批准"):
            if not confirmed_items:
                feishu_send("📊 管家日历中暂无活动。\n\n回复「日程」可查看Outlook全部日程。")
            else:
                lines = ["📊 管家日历中的活动：\n"]
                for idx, item in enumerate(confirmed_items, 1):
                    d = (item.get("dates") or [""])[0]
                    lines.append(f"{idx}. [{fmt_date(d)}] {item['subject'][:44]}")
                lines.append("\n🗑️ 回复「取消」查看可取消清单")
                lines.append("📅 回复「日程」查看Outlook全部日程")
                feishu_send("\n".join(lines))

        elif text.startswith("取消") or text.startswith("revoke"):
            if not confirmed_items:
                feishu_send("📊 管家日历中没有活动可取消。\n\nOutlook日程请用「日程」+「删除」处理。")
                continue

            rest = (text[len("取消"):].strip() if text.startswith("取消")
                    else text[len("revoke"):].strip())

            # 无参数 → 直接显示可取消清单
            if not rest:
                lines = ["📊 管家日历中的活动（可取消）：\n"]
                for idx, item in enumerate(confirmed_items, 1):
                    d = (item.get("dates") or [""])[0]
                    lines.append(f"{idx}. [{fmt_date(d)}] {item['subject'][:44]}")
                lines.append("\n🗑️ 指定方式（任选）：")
                lines.append("  取消 1        → 按编号")
                lines.append("  取消 10月14   → 按日期")
                lines.append("  取消 生涯      → 按名称")
                feishu_send("\n".join(lines))
                continue

            # 方式1：按日期
            md = parse_month_day(rest)
            if md:
                matches = [it for it in confirmed_items if item_month_day(it) == md]
                if not matches:
                    feishu_send(f"⚠️ 管家日历中没有 {md[0]}月{md[1]}日 的活动。\n回复「取消」查看清单。")
                elif len(matches) == 1:
                    do_cancel_item(matches[0], confirmed_items)
                else:
                    lines = [f"⚠️ {md[0]}月{md[1]}日 有 {len(matches)} 个活动：\n"]
                    for idx, it in enumerate(matches, 1):
                        lines.append(f"{idx}. {it['subject'][:44]}")
                    lines.append("\n回复「取消 N」或更具体的名称")
                    feishu_send("\n".join(lines))
                continue

            # 方式2：按编号（纯数字）
            if rest.isdigit():
                num = int(rest)
                if num < 1 or num > len(confirmed_items):
                    feishu_send(f"⚠️ 编号超出范围（1-{len(confirmed_items)}）。\n回复「取消」查看清单。")
                    continue
                do_cancel_item(confirmed_items[num - 1], confirmed_items)
                continue

            # 方式3：按名称关键词
            matches = [it for it in confirmed_items if rest in it.get("subject", "")]
            if not matches:
                feishu_send(f"⚠️ 管家日历中没有名称含「{rest}」的活动。\n回复「取消」查看清单。")
            elif len(matches) == 1:
                do_cancel_item(matches[0], confirmed_items)
            else:
                lines = [f"⚠️ 找到 {len(matches)} 个匹配「{rest}」的活动：\n"]
                for idx, it in enumerate(matches, 1):
                    d = (it.get("dates") or [""])[0]
                    lines.append(f"{idx}. [{fmt_date(d)}] {it['subject'][:44]}")
                lines.append("\n回复「取消 N」进一步指定")
                feishu_send("\n".join(lines))

        elif text in ("日程", "全部日程", "outlook", "events"):
            try:
                events = outlook.upcoming_events()
                if not events:
                    feishu_send("📅 未来两周Outlook日历上没有日程。")
                else:
                    lines = [f"📅 Outlook日历 · 未来14天日程（共{len(events)}个）：\n"]
                    outlook_map = {}
                    for idx, ev in enumerate(events[:40], 1):
                        subj = ev.get("Subject", "") or "(无标题)"
                        date_part = (ev.get("Start", {}).get("DateTime", "") or "?")[:10]
                        mark = "📮" if "📮" in subj else "　"
                        lines.append(f"{idx}. {mark} [{fmt_date(date_part)}] {subj[:40]}")
                        outlook_map[str(idx)] = {"id": ev.get("Id", ""), "subject": subj, "date": date_part}
                    state["outlook_map"] = outlook_map
                    lines.append("\n📮=管家标记 | 空格=其他来源")
                    lines.append("❌ 回复「删除」按清单删除")
                    lines.append("  也可直接：删除 10月14 / 删除 生涯")
                    feishu_send("\n".join(lines))
            except Exception as e:
                feishu_send(f"⚠️ 查询日程失败: {e}")
                log(f"查询日程失败: {e}", "WARN")

        elif text.startswith("删除") or text.startswith("delete"):
            outlook_map = state.get("outlook_map", {})
            rest = (text[len("删除"):].strip() if text.startswith("删除")
                    else text[len("delete"):].strip())

            # 无参数 → 重新查询并显示可删除清单
            if not rest:
                try:
                    events = outlook.upcoming_events()
                    if not events:
                        feishu_send("📅 未来两周Outlook日历上没有日程。")
                        continue
                    lines = [f"📅 Outlook日历 · 未来14天日程（共{len(events)}个）：\n"]
                    outlook_map = {}
                    for idx, ev in enumerate(events[:40], 1):
                        subj = ev.get("Subject", "") or "(无标题)"
                        date_part = (ev.get("Start", {}).get("DateTime", "") or "?")[:10]
                        mark = "📮" if "📮" in subj else "　"
                        lines.append(f"{idx}. {mark} [{fmt_date(date_part)}] {subj[:40]}")
                        outlook_map[str(idx)] = {"id": ev.get("Id", ""), "subject": subj, "date": date_part}
                    state["outlook_map"] = outlook_map
                    lines.append("\n❌ 指定删除（任选）：")
                    lines.append("  删除 3 → 按编号")
                    lines.append("  删除 10月14 → 按日期")
                    lines.append("  删除 生涯 → 按名称")
                    feishu_send("\n".join(lines))
                except Exception as e:
                    feishu_send(f"⚠️ 查询日程失败: {e}")
                    log(f"查询日程失败: {e}", "WARN")
                continue

            # 方式1：按日期
            md = parse_month_day(rest)
            if md:
                matches = [e for e in outlook_map.values()
                           if (e.get("date") or "")[5:10] == f"{md[0]:02d}-{md[1]:02d}"]
                if not matches:
                    feishu_send(f"⚠️ 未来14天没有 {md[0]}月{md[1]}日 的日程。\n回复「删除」查看清单。")
                elif len(matches) == 1:
                    do_delete_event(outlook, matches[0], confirmed_items, outlook_map, state)
                else:
                    lines = [f"⚠️ {md[0]}月{md[1]}日 有 {len(matches)} 个日程：\n"]
                    for idx, e in enumerate(matches, 1):
                        lines.append(f"{idx}. {e['subject'][:44]}")
                    lines.append("\n回复「删除 N」进一步指定")
                    feishu_send("\n".join(lines))
                continue

            # 方式2：按编号
            if rest.isdigit():
                num = int(rest)
                if str(num) not in outlook_map:
                    feishu_send(f"⚠️ 编号无效（1-{len(outlook_map)}）。\n回复「删除」获取最新编号。")
                    continue
                do_delete_event(outlook, outlook_map[str(num)], confirmed_items, outlook_map, state)
                continue

            # 方式3：按名称关键词
            matches = [e for e in outlook_map.values() if rest in e.get("subject", "")]
            if not matches:
                feishu_send(f"⚠️ 未来14天日程中没有名称含「{rest}」的。\n回复「删除」查看清单。")
            elif len(matches) == 1:
                do_delete_event(outlook, matches[0], confirmed_items, outlook_map, state)
            else:
                lines = [f"⚠️ 找到 {len(matches)} 个匹配「{rest}」的日程：\n"]
                for idx, e in enumerate(matches, 1):
                    lines.append(f"{idx}. [{fmt_date(e.get('date',''))}] {e['subject'][:44]}")
                lines.append("\n回复「删除 N」进一步指定")
                feishu_send("\n".join(lines))

        elif text.startswith("批准") or text.startswith("approve"):
            # 解析编号
            num = 0
            m = re.search(r'(\d+)', text)
            if m:
                num = int(m.group(1))

            active = [i for i in pending_items if i.get("status") == "pending"]
            if not active:
                feishu_send("📋 当前没有待确认的活动。")
                continue

            if num == 0:
                num = len(active)  # 默认批准最后一条（最新的）

            if num < 1 or num > len(active):
                feishu_send(f"⚠️ 编号超出范围（1-{len(active)}），回复「列表」查看。")
                continue

            item = active[num - 1]
            item["status"] = "confirmed"
            confirmed_items.append(item)
            # v39c: 不再物理删除 item，保留在 pending.json 做去重
            # pending_items.remove(item)  # 旧逻辑：删除后下次扫描会重复推送

            feishu_send(
                f"✅ 已加入日历！\n\n"
                f"📌 {item['subject'][:50]}\n"
                f"📅 {item.get('date_txt', '时间待定')}\n"
                f"👤 {item.get('sender', '')}\n\n"
                f"将自动同步到你的 iPhone 日历。"
            )
            log(f"✅ 用户批准活动: {item['subject'][:40]}")

        elif text.startswith("跳过") or text.startswith("skip"):
            num = 0
            m = re.search(r'(\d+)', text)
            if m:
                num = int(m.group(1))

            active = [i for i in pending_items if i.get("status") == "pending"]
            if not active:
                feishu_send("📋 当前没有待确认的活动。")
                continue

            if num == 0:
                num = len(active)

            if num < 1 or num > len(active):
                feishu_send(f"⚠️ 编号超出范围（1-{len(active)}），回复「列表」查看。")
                continue

            item = active[num - 1]
            item["status"] = "skipped"
            # v39c: 不再物理删除 item，保留在 pending.json 做去重
            # pending_items.remove(item)  # 旧逻辑：删除后下次扫描会重复推送

            feishu_send(f"⏭️ 已跳过：{item['subject'][:50]}")
            log(f"用户跳过活动: {item['subject'][:40]}")

        else:
            # 未识别的命令
            feishu_send(
                f"收到消息：「{cmd['text'][:20]}」\n\n"
                f"我可以帮你：\n"
                f"• 回复「列表」查看待确认活动\n"
                f"• 回复「日历」管理日程\n"
                f"• 回复「帮助」查看完整指令"
            )


# ================ Bark 推送（备用）================
def bark_notify(title, body):
    if not BARK_KEY:
        return
    try:
        url = (f"{BARK_URL}/{BARK_KEY}/"
               f"{urllib.parse.quote(title)}?body={urllib.parse.quote(body)}"
               f"&group={urllib.parse.quote('AI邮件管家')}&sound=bell")
        urllib.request.urlopen(url, timeout=10)
        log(f"Bark 推送成功: {title}")
    except Exception as e:
        log(f"Bark 推送失败: {e}", "WARN")


# ================ ICS 生成 ================
def generate_ics():
    confirmed = load_json(CONFIRMED_FILE, {"items": []})
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AI Mail Butler//CN",
        "CALSCALE:GREGORIAN", "X-WR-CALNAME:AI邮件管家", "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    for item in confirmed.get("items", []):
        uid = hashlib.md5(item["id"].encode()).hexdigest()
        dtstamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
        dates = item.get("dates", [])
        if not dates:
            continue
        d_start = dates[0].replace("-", "")
        d_parts = list(map(int, dates[0].split("-")))
        d_end_dt = datetime(d_parts[0], d_parts[1], d_parts[2]) + timedelta(days=1)
        d_end = d_end_dt.strftime("%Y%m%d")
        summary = item.get("subject", "活动")[:80]
        sender = item.get("sender", "")
        start_time = item.get("start_time")
        end_time = item.get("end_time")
        if start_time and end_time:
            # 有具体时间的事件
            dt_start = f"{d_start}T{start_time.replace(':','')}00"
            dt_end = f"{dates[0].replace('-','')}T{end_time.replace(':','')}00"
            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{uid}@mail-butler",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART:{dt_start}",
                f"DTEND:{dt_end}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:来自 {sender}",
                "BEGIN:VALARM", "TRIGGER:-PT1H", "ACTION:DISPLAY",
                f"DESCRIPTION:{summary[:40]} 1小时后开始", "END:VALARM",
                "END:VEVENT",
            ])
        else:
            # 全天事件
            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{uid}@mail-butler",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART;VALUE=DATE:{d_start}",
                f"DTEND;VALUE=DATE:{d_end}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:来自 {sender}",
                "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
                f"DESCRIPTION:{summary[:40]} 明天开始", "END:VALARM",
                "END:VEVENT",
            ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ================ 主流程 ================
def run():
    state = load_json(STATE_FILE, {"last_check": None, "processed": [], "feishu_processed": []})
    pending = load_json(PENDING_FILE, {"items": []})
    confirmed = load_json(CONFIRMED_FILE, {"items": []})

    log("🚀 AI 邮件管家 v2.0 开始运行")

    # 先获取 Outlook 令牌（命令处理与邮件检查都需要）
    token = get_access_token()
    outlook = Outlook(token)
    # v23: access token 存入 state（run() 末尾 save 时带上，供腾讯云函数直接读取；
    # 云函数 IP 刷新 Outlook token 被微软条件访问拒绝，读现成的绕开限制）
    try:
        import time as _t
        state["outlook_access"] = {"token": token, "expires_at": _t.time() + 3500}
    except Exception as e:
        log(f"⚠️ outlook token 存档失败: {e}")

    # ---- 1. 飞书命令处理已移至腾讯云函数（即时响应，~1秒）----
    log("📱 飞书命令由云函数即时处理（跳过）")

    # ---- 1b. 每日早安日程提醒（北京时间7点后首次运行时触发）----
    beijing_now = datetime.now(timezone.utc) + timedelta(hours=8)
    beijing_hour = beijing_now.hour
    today_str = beijing_now.strftime("%Y-%m-%d")
    if beijing_hour >= 7 and state.get("last_morning_briefing") != today_str:
        log("🌅 发送早安日程提醒")
        try:
            events = outlook.upcoming_events(days=1)
            today_events = [e for e in events
                           if (e.get("Start", {}).get("DateTime", "") or "")[:10] == today_str]
            if not today_events:
                feishu_send("🌅 早上好！今天日历上没有安排，好好享受一天～")
            else:
                lines = [f"🌅 早上好！今天有 {len(today_events)} 个日程：\n"]
                for idx, ev in enumerate(today_events, 1):
                    subj = ev.get("Subject", "") or "(无标题)"
                    time_part = (ev.get("Start", {}).get("DateTime", "") or "")[11:16]
                    mark = "📮" if "📮" in subj else "📅"
                    if time_part:
                        lines.append(f"{idx}. {mark} ⏰ {time_part} {subj[:40]}")
                    else:
                        lines.append(f"{idx}. {mark} {subj[:40]}")
                lines.append("\n祝今天顺利！💪")
                feishu_send("\n".join(lines))
            state["last_morning_briefing"] = today_str
            log("✅ 早安提醒已发送")
        except Exception as e:
            log(f"⚠️ 早安提醒失败: {e}", "WARN")

    # ---- 2. 检查新邮件 ----
    now = datetime.now(timezone.utc)
    since = state.get("last_check") or (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"检查新邮件 since={since}")

    mails = outlook.new_messages(since)
    log(f"新邮件 {len(mails)} 封")

    new_activities = 0
    for mail in mails:
        mid = mail.get("Id")
        if mid in state.get("processed", []):
            continue
        state.setdefault("processed", []).append(mid)

        subject = (mail.get("Subject") or "").strip() or "(无主题)"
        sender = (mail.get("From") or {}).get("EmailAddress", {}).get("Name", "?")
        preview = (mail.get("BodyPreview") or "")[:200]

        if not is_activity_email(subject, preview):
            continue

        # 获取邮件全文
        log(f"📧 读取邮件全文: {subject[:40]}")
        body_text = outlook.get_body(mid)

        # 提取日期
        dates = extract_dates(subject + " " + body_text)
        date_txt = "、".join(d.strftime("%m月%d日") for d in dates) if dates else "时间待定"

        # 提取起止时间
        time_range = extract_time_range(body_text)
        if time_range:
            time_txt = f"{time_range[0]:02d}:{time_range[1]:02d}-{time_range[2]:02d}:{time_range[3]:02d}"
            log(f"⏰ 提取时间: {time_txt}")
        else:
            time_txt = ""

        # 生成中文摘要
        summary = generate_summary(subject, body_text, sender, date_txt)
        if time_txt:
            summary = summary.replace(f"📅 时间：{date_txt}", f"📅 时间：{date_txt} {time_txt}") if date_txt != "时间待定" else f"📅 时间：{time_txt}\n" + summary

        # 分类重要性
        importance = classify_importance(subject, body_text)
        importance_emoji = IMPORTANCE_EMOJI[importance]
        log(f"🎯 活动邮件: {subject[:40]}（{date_txt} {time_txt}）[{importance}]")

        # 写入 Outlook 日历
        prefix = "📮"
        title = f"{prefix} {subject[:40]}"
        note = summary[:500]
        try:
            if dates:
                d = dates[0]
                if time_range:
                    start = d.replace(hour=time_range[0], minute=time_range[1])
                    end = d.replace(hour=time_range[2], minute=time_range[3])
                else:
                    start = d.replace(hour=9)
                    end = d.replace(hour=10)
                outlook.create_event(title, start, end, body=note)
            else:
                outlook.create_event(title + "（时间待定）",
                                     now.replace(tzinfo=None) + timedelta(days=1),
                                     now.replace(tzinfo=None) + timedelta(days=2),
                                     body=note, all_day=True)
            log(f"✅ Outlook 日历已写入: {title[:50]}")
        except Exception as e:
            log(f"⚠️ Outlook 日历写入失败: {e}", "WARN")

        # 加入待确认队列
        item = {
            "id": mid, "subject": subject, "sender": sender,
            "preview": preview, "summary": summary,
            "date_txt": date_txt,
            "dates": [d.strftime("%Y-%m-%d") for d in dates],
            "start_time": f"{time_range[0]:02d}:{time_range[1]:02d}" if time_range else None,
            "end_time": f"{time_range[2]:02d}:{time_range[3]:02d}" if time_range else None,
            "importance": importance,
            "status": "pending",
            "found_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        # 去重：v40c 按"规范化 subject"比对——同一活动的 RE:/FW: 转发变体不重复入队
        existing = [i for i in pending.setdefault("items", [])
                    if norm_subject(i.get("subject") or "") == norm_subject(subject)]
        if not existing:
            pending["items"].append(item)
            new_activities += 1
        save_json(PENDING_FILE, pending)

        # 推送通知
        push_text = (
            f"{importance_emoji} 发现{'' if importance == 'medium' else '重要' if importance == 'high' else '新'}活动\n\n{summary}\n\n"
            f"━━━━━━━━━━━━━━\n"
            f"💬 回复「批准」加入日历\n"
            f"💬 回复「跳过」忽略此活动\n"
            f"💬 回复「列表」查看所有待确认"
        )
        feishu_sent = feishu_send(push_text)
        if not feishu_sent:
            # 飞书失败则用 Bark 备用
            bark_notify("📬 发现新活动", f"{subject[:60]}\n📅 {date_txt}\n👤 {sender}")

    state["last_check"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state["processed"] = state["processed"][-500:]
    save_json(STATE_FILE, state)

    # ---- 3. 生成 ICS ----
    os.makedirs(os.path.dirname(ICS_FILE), exist_ok=True)
    with open(ICS_FILE, "w", encoding="utf-8") as f:
        f.write(generate_ics())
    log(f"✅ ICS 已生成（已确认 {len(confirmed.get('items', []))} 条活动）")

    # ---- 4. 创建/续期 Outlook 推送订阅（实时邮件提醒）----
    SCF_URL = os.environ.get("SCF_URL", "")
    try:
        outlook.create_subscription(SCF_URL)
        log("✅ Outlook推送订阅已续期")
    except Exception as e:
        log(f"⚠️ 推送订阅续期失败（非致命）: {e}", "WARN")

    log(f"✅ 运行完成（本次新增 {new_activities} 条活动）")


if __name__ == "__main__":
    run()
