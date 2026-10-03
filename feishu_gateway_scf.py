# -*- coding: utf-8 -*-
"""
AI 邮件管家 · 腾讯云SCF 即时命令处理器 v2
==========================================
飞书消息 → 云函数直接处理 → 飞书回复（~3秒）
跳过 GitHub Actions，实现秒级响应。

环境变量（在腾讯云函数控制台配置）：
  GH_TOKEN              — GitHub Personal Access Token
  FEISHU_APP_ID         — 飞书应用 App ID
  FEISHU_APP_SECRET     — 飞书应用 App Secret
  OUTLOOK_REFRESH_TOKEN — Outlook 刷新令牌
  OUTLOOK_CLIENT_ID     — Outlook 客户端 ID
"""
import json
import os
import base64
import re
import time
import collections
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta

# ============ 环境变量 ============
GH_TOKEN = os.environ.get("GH_TOKEN", "")
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
OUTLOOK_REFRESH_TOKEN = os.environ.get("OUTLOOK_REFRESH_TOKEN", "")
OUTLOOK_CLIENT_ID = os.environ.get("OUTLOOK_CLIENT_ID", "d3590ed6-52b3-4102-aeff-aad2292ab01c")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

REPO = "wutian-yu/mail-butler"              # 公开仓库：代码+workflow（仅用于触发Actions）
DATA_REPO = "wutian-yu/butler-data"          # 私有仓库：所有状态数据（邮件/聊天/活动）
FEISHU_VERIFY_TOKEN = os.environ.get("FEISHU_VERIFY_TOKEN", "")
LM_CAL_URL = os.environ.get("LM_CAL_URL", "https://core.xjtlu.edu.cn/calendar/export_execute.php?userid=7860&authtoken=e0f4ad6318700f9841cb02a96315cb30d6451624&preset_what=all&preset_time=recentupcoming")
FEISHU_BASE = "https://open.feishu.cn"
CHAT_ID_FALLBACK = "oc_0f1f851c3fce82feff595f7a44bcc88a"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_FEISHU_TOKEN_CACHE = {"token": "", "expires": 0}
# 去重：已处理的消息ID（保持插入序，超限时渐进淘汰最旧的，避免全清导致重复）
_PROCESSED_MSG_IDS = collections.OrderedDict()


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


CHAT_HISTORY_FILE = "chat_history.json"


# ============ HTTP 工具 ============
def _http(url, method="GET", headers=None, data=None, timeout=30):
    body = None
    if data is not None:
        body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data
    req = urllib.request.Request(url, data=body, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if body is not None and isinstance(data, (dict, list)):
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw.decode()) if raw else None


# ============ 飞书 API ============
def get_feishu_token():
    if _FEISHU_TOKEN_CACHE["token"] and time.time() < _FEISHU_TOKEN_CACHE["expires"]:
        return _FEISHU_TOKEN_CACHE["token"]
    data = _http(f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                 method="POST", data={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET})
    token = data.get("tenant_access_token", "")
    _FEISHU_TOKEN_CACHE["token"] = token
    _FEISHU_TOKEN_CACHE["expires"] = time.time() + 6600
    return token


def feishu_send(chat_id, text):
    """发送普通文本消息（用于聊天对话）"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return
    text = _strip_md(text)
    try:
        token = get_feishu_token()
        _http(f"{FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type=chat_id",
              method="POST",
              headers={"Authorization": f"Bearer {token}"},
              data={"receive_id": chat_id, "msg_type": "text",
                    "content": json.dumps({"text": text})})
        log(f"✅ 飞书回复: {text[:40]}")
    except Exception as e:
        log(f"飞书发送失败: {e}")


def _strip_md(text):
    """去掉聊天/信件文本里的 Markdown 符号（纯文本消息会原样显示 **、*、# 等）"""
    if not text:
        return text
    t = text
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)          # **加粗**
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", t)  # *斜体*（不误伤**）
    t = re.sub(r"__(.+?)__", r"\1", t)              # __下划线__
    t = re.sub(r"`([^`\n]+)`", r"\1", t)            # `行内代码`
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)    # ## 标题
    t = re.sub(r"^>\s*", "", t, flags=re.M)         # > 引用
    t = re.sub(r"^[\s]*[-*+]\s+(?=[^\d])", "", t, flags=re.M)  # 行首列表符（不碰日期数字）
    return t


def feishu_send_action(chat_id, title, content, color="green"):
    """发送操作结果卡片消息（带彩色标题栏，醒目区分于聊天）"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return
    try:
        token = get_feishu_token()
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": color,
                "title": {"tag": "plain_text", "content": title}
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": "⚡ 此为系统操作结果，非AI对话"}
                ]}
            ]
        }
        _http(f"{FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type=chat_id",
              method="POST",
              headers={"Authorization": f"Bearer {token}"},
              data={"receive_id": chat_id, "msg_type": "interactive",
                    "content": json.dumps(card)})
        log(f"✅ 飞书操作卡片: {title}")
    except Exception as e:
        log(f"飞书操作卡片失败: {e}")
        # 回退到普通文本
        feishu_send(chat_id, f"⚡ {title}\n\n{content}")


# ============ GitHub API (读写状态文件) ============
def gh_read_json(path):
    """读取私有数据仓库中的 JSON 文件，返回 (data, sha)；失败重试1次；
    404（文件不存在）不重试——那是确定状态，重试只会白白多等几秒"""
    url = f"https://api.github.com/repos/{DATA_REPO}/contents/{urllib.parse.quote(path, safe='/')}"
    last_err = None
    for attempt in range(2):
        try:
            resp = _http(url, headers={"Authorization": f"Bearer {GH_TOKEN}",
                                       "Accept": "application/vnd.github+json"})
            content = base64.b64decode(resp["content"]).decode("utf-8")
            return json.loads(content), resp.get("sha", "")
        except Exception as e:
            if getattr(e, "code", None) == 404:
                raise
            last_err = e
            log(f"GitHub读取失败(第{attempt+1}次): {e}")
            if attempt == 0:
                time.sleep(1)
    raise last_err


def gh_write_json(path, data, sha, message="update", retries=2):
    """写入 JSON 文件到私有数据仓库（失败自动重试：处理网络抖动和sha冲突）"""
    content = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode()).decode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            body = {"message": message, "content": content}
            if sha:
                body["sha"] = sha
            url = f"https://api.github.com/repos/{DATA_REPO}/contents/{urllib.parse.quote(path, safe='/')}"
            _http(url, method="PUT",
                  headers={"Authorization": f"Bearer {GH_TOKEN}",
                           "Accept": "application/vnd.github+json"},
                  data=body)
            return
        except Exception as e:
            last_err = e
            log(f"GitHub写入失败(第{attempt+1}次尝试): {e}")
            if attempt < retries:
                time.sleep(2)
                # 重读最新sha，避免并发写入导致的409冲突
                try:
                    _, sha = gh_read_json(path)
                except Exception:
                    sha = ""
    raise last_err


def trigger_github():
    """触发 GitHub Actions 重新生成 ICS"""
    if not GH_TOKEN:
        log("⚠️ trigger_github: 无 GH_TOKEN")
        return
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/workflows/butler.yml/dispatches",
            data=json.dumps({"ref": "main"}).encode(), method="POST",
            headers={"Authorization": f"Bearer {GH_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"⚠️ trigger_github 失败: {e}")  # M7 修复：不再静默吞掉


# ============ Outlook API ============
_OUTLOOK_TOKEN_CACHE = {"token": "", "expires": 0}

def get_outlook_token():
    # M3 修复：加缓存，避免同一请求重复获取 token
    if _OUTLOOK_TOKEN_CACHE["token"] and time.time() < _OUTLOOK_TOKEN_CACHE["expires"]:
        return _OUTLOOK_TOKEN_CACHE["token"]
    body = urllib.parse.urlencode({
        "client_id": OUTLOOK_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": OUTLOOK_REFRESH_TOKEN,
        "scope": "https://outlook.office.com/.default offline_access",
    }).encode()
    req = urllib.request.Request(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read().decode())
    _OUTLOOK_TOKEN_CACHE["token"] = tok["access_token"]
    _OUTLOOK_TOKEN_CACHE["expires"] = time.time() + tok.get("expires_in", 3600) - 60
    return tok["access_token"]


def outlook_events():
    token = get_outlook_token()
    data = _http(
        "https://outlook.office.com/api/v2.0/me/events?$top=50&$select=Id,Subject,Start",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    events = data.get("value", [])
    today = datetime.now().strftime("%Y-%m-%d")
    limit = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    upcoming = [e for e in events
                if today <= (e.get("Start", {}).get("DateTime", "") or "9999")[:10] <= limit]
    upcoming.sort(key=lambda e: e.get("Start", {}).get("DateTime", ""))
    return upcoming


def outlook_delete(event_id):
    token = get_outlook_token()
    safe_id = urllib.parse.quote(event_id, safe="")
    req = urllib.request.Request(
        f"https://outlook.office.com/api/v2.0/me/events/{safe_id}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"})
    urllib.request.urlopen(req, timeout=30)


def outlook_create_event(subject, start_dt, end_dt, body=""):
    """在 Outlook 日历创建事件 → 同步到 iPhone 自带日历
    完全对齐 butler.py 的 _call 实现，避免 _http 的 header 冲突"""
    token = get_outlook_token()
    ev = {
        "Subject": subject[:120],
        "Start": {"DateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"), "TimeZone": "China Standard Time"},
        "End": {"DateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"), "TimeZone": "China Standard Time"},
        "Body": {"ContentType": "Text", "Content": body or ""},
        "ShowAs": "Free",
    }
    # 不用 _http（它会重复设 Content-Type），直接手写，完全对齐 butler.py 的 _call
    data = json.dumps(ev).encode()
    req = urllib.request.Request("https://outlook.office.com/api/v2.0/me/events",
        data=data, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/json",
                 "Content-Type": "application/json",
                 "Origin": "https://outlook.office.com",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    resp = json.loads(raw.decode()) if raw else None
    return resp.get("Id") if resp else None


# ============ 日期工具 ============
def fmt_date(s):
    if not s:
        return "时间待定"
    try:
        y, m, d = map(int, str(s).split("-")[:3])
        return f"{m}月{d:02d}日·{WEEKDAYS[datetime(y, m, d).weekday()]}"
    except Exception:
        return str(s)


def parse_md(text):
    m = re.search(r'(\d{1,2})月(\d{1,2})日?', text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r'(\d{1,2})[-/](\d{1,2})', text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def item_md(item):
    dates = item.get("dates") or []
    if not dates:
        return None
    try:
        p = dates[0].split("-")
        return (int(p[1]), int(p[2]))
    except Exception:
        return None


# ============ 命令处理 ============
HELP_TEXT = (
    "🤖 AI邮件管家 · 帮助\n\n"
    "📋 邮件活动\n"
    "「列表」待确认活动\n"
    "「批准 [N]」加入日历　「批准全部」批量\n"
    "「跳过 [N]」忽略活动　「跳过全部」批量\n\n"
    "📅 日历\n"
    "「日历」管家已加入的活动\n"
    "「日程」Outlook全部日程\n"
    "「删日历」查看可删清单\n"
    "「删日历 N」删第N个\n"
    "「删日历 10月9号的数学」按条件删\n"
    "「删日历全部 📚」删所有作业日程\n\n"
    "📚 作业\n"
    "「作业」查看 LearningMall 截止\n"
    "「把XX作业加到日历」写进 Outlook 日历\n\n"
    "🏫 AMS 考勤\n"
    "「签到 码」远程签到（72小时内可补）\n"
    "「出勤」出勤率统计\n"
    "「请假 日期 [结束日期] 原因」申请准假（支持范围：「请假 10-05 10-14 崴脚」）\n"
    "「改假条 新原因」修改假条正文\n"
    "「取消请假」放弃进行中假条\n"
    "「撤回请假 [日期]」撤回待审批申请（「撤回请假」全撤；「撤回请假 10-11」只撤那天）\n\n"
    "🏛️ 图书馆研讨室\n"
    "「查房 [日期] [楼层]」查空闲房间\n"
    "「订房 房间号 日期 时段」预定（自动带同伴）\n"
    "「设同伴 张伟」设置常用同学（中文名即可）\n"
    "「我的预约」查我的预定\n"
    "「取消预定 N」取消第N个\n"
    "（研讨室要求至少2人；也可自然语言：「明天下午和张伟订个研讨室」）\n\n"
    "💡 也可以直接跟我聊天～"
)


def _check_conflict(item):
    """检查待批准活动与现有Outlook日历是否时间冲突"""
    dates = item.get("dates") or []
    if not dates:
        return None
    start_time = item.get("start_time")
    if not start_time:
        return None  # 无具体时间不检测
    try:
        events = outlook_events()
    except Exception:
        return None
    target_date = dates[0]
    target_start = start_time
    end_time = item.get("end_time", start_time)
    conflicts = []
    for ev in events:
        ev_date = (ev.get("Start", {}).get("DateTime", "") or "")[:10]
        ev_time = (ev.get("Start", {}).get("DateTime", "") or "")[11:16]
        if ev_date == target_date and ev_time:
            ev_subj = ev.get("Subject", "")[:30]
            conflicts.append(f"⏰ {ev_time} {ev_subj}")
    if conflicts:
        return f"同一天已有 {len(conflicts)} 个日程：\n" + "\n".join(conflicts[:3])
    return None


def _load_chat_history():
    """加载对话历史"""
    try:
        data, sha = gh_read_json(CHAT_HISTORY_FILE)
        return data.get("messages", []), sha
    except Exception:
        return [], None


def _save_chat_history(messages, sha):
    """保存对话历史（只保留最近20条）"""
    messages = messages[-20:]
    try:
        gh_write_json(CHAT_HISTORY_FILE, {"messages": messages}, sha, "chat history")
    except Exception as e:
        log(f"⚠️ 对话历史保存失败: {e}")


def cmd_help(chat_id):
    feishu_send(chat_id, HELP_TEXT)


def cmd_list(chat_id):
    try:
        pending, _ = gh_read_json("pending.json")
    except Exception:
        pending = {"items": []}
    active = [i for i in pending.get("items", []) if i.get("status") == "pending"]
    if not active:
        feishu_send(chat_id, "📋 当前没有待确认的活动。")
        return

    # 去重 + 清理标题
    seen = set()
    cleaned = []
    for item in active:
        subj = item['subject'].strip()
        key = subj[:20]
        if key in seen:
            continue
        seen.add(key)
        # 清理标题尾部嵌入的日期时间（标题常自带日期）
        subj = re.sub(r'\s*[—\-–]+\s*\d{4}年.*$', '', subj).strip() or subj
        subj = re.sub(r'\s*[—\-–]+\s*\d{4}-\d{2}-\d{2}.*$', '', subj).strip() or subj
        cleaned.append({"subject": subj[:38], "date": item.get("date_txt", "") or ""})

    # 分组：有确定日期 vs 时间待定
    dated = [c for c in cleaned if c["date"] and c["date"] != "时间待定"]
    undated = [c for c in cleaned if c not in dated]

    def date_key(s):
        m = re.search(r'(\d{1,2})月(\d{1,2})日', s or '')
        return (int(m.group(1)), int(m.group(2))) if m else (99, 99)
    dated.sort(key=lambda c: date_key(c["date"]))

    lines = [f"📋 待确认活动（{len(cleaned)} 个）："]
    idx = 1
    if dated:
        lines.append("\n📅 有确定时间：")
        for c in dated:
            lines.append(f"{idx}. {c['subject']}")
            lines.append(f"　　🕒 {c['date']}")
            idx += 1
    if undated:
        if dated:
            lines.append("")
        lines.append("⏰ 时间待定：")
        for c in undated:
            lines.append(f"{idx}. {c['subject']}")
            idx += 1
    lines.append("\n✅ 「批准 N」加入日历　⏭️ 「跳过 N」忽略")
    feishu_send(chat_id, "\n".join(lines))


def cmd_calendar(chat_id):
    try:
        confirmed, _ = gh_read_json("confirmed.json")
    except Exception:
        confirmed = {"items": []}
    items = confirmed.get("items", [])
    if not items:
        feishu_send(chat_id, "📊 管家日历中暂无活动。\n\n回复「日程」可查看Outlook全部日程。")
        return
    lines = ["📊 管家日历中的活动：\n"]
    for idx, item in enumerate(items, 1):
        d = (item.get("dates") or [""])[0]
        lines.append(f"{idx}. [{fmt_date(d)}] {item['subject'][:44]}")
    lines.append("\n🗑️ 回复「删除」查看可删除清单")
    lines.append("📅 回复「日程」查看Outlook全部日程")
    feishu_send(chat_id, "\n".join(lines))


def _create_outlook_event_from_item(item):
    """从 pending item 创建 Outlook 日历事件，返回 event_id 或 None"""
    try:
        subject = (item.get("subject") or "(活动)")[:100]
        prefix = "📮"
        title = f"{prefix} {subject}"
        body = (item.get("summary") or item.get("preview") or "")[:500]
        dates = item.get("dates") or []
        time_range = item.get("time_range")  # [h0, m0, h1, m1] or None
        now = datetime.now()
        if dates:
            d = dates[0]
            # H1 修复：JSON 反序列化后 dates 可能是字符串，转为 datetime
            if isinstance(d, str):
                try:
                    d = datetime.strptime(d[:10], "%Y-%m-%d")
                except ValueError:
                    d = None
            if d and time_range and len(time_range) == 4:
                start = d.replace(hour=time_range[0], minute=time_range[1])
                end = d.replace(hour=time_range[2], minute=time_range[3])
            elif d:
                start = d.replace(hour=9, minute=0)
                end = d.replace(hour=10, minute=0)
            else:
                start = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
                end = start.replace(hour=10)
                title += "（时间待定）"
        else:
            # 时间待定：占位明天 9-10 点
            start = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            end = start.replace(hour=10)
            title += "（时间待定）"
        eid = outlook_create_event(title, start, end, body=body)
        log(f"📅 Outlook 日历事件已创建: {title[:40]} → {eid}")
        return eid
    except Exception as e:
        log(f"⚠️ 创建 Outlook 日历事件失败: {e}")
        return None


def cmd_approve(chat_id, text):
    try:
        pending, p_sha = gh_read_json("pending.json")
        confirmed, c_sha = gh_read_json("confirmed.json")
    except Exception:
        feishu_send(chat_id, "⚠️ 读取状态失败，请稍后重试。")
        return
    active = [i for i in pending.get("items", []) if i.get("status") == "pending"]
    if not active:
        feishu_send(chat_id, "📋 当前没有待确认的活动。")
        return

    # 批量：批准全部
    if "全部" in text or "all" in text:
        count = len(active)
        created = 0
        for item in active:
            item["status"] = "confirmed"
            # 真实写入 Outlook 日历
            eid = _create_outlook_event_from_item(item)
            if eid:
                item["outlook_event_id"] = eid
                created += 1
            confirmed.setdefault("items", []).append(item)
        pending["items"] = [i for i in pending.get("items", []) if i.get("status") != "pending"]
        gh_write_json("pending.json", pending, p_sha, "approve all")
        try:
            confirmed2, c_sha2 = gh_read_json("confirmed.json")
            confirmed2["items"] = confirmed["items"]
            gh_write_json("confirmed.json", confirmed2, c_sha2, "approve all")
        except Exception:
            gh_write_json("confirmed.json", confirmed, c_sha, "approve all")
        sync_note = f"已批准 **{count}** 个活动，{created} 个已写入 Outlook 日历。\n\n打开 iPhone 日历 app 即可看到（如果看不到，在日历 app 里点底部「日历」勾选 Outlook 分组）。" if created else f"已批准 **{count}** 个活动，但写入日历失败，稍后 Actions 会补写。"
        feishu_send_action(chat_id, "✅ 批量批准完成", sync_note, color="green")
        trigger_github()
        return

    # 单条批准
    num = 0
    m = re.search(r'(\d+)', text)
    if m:
        num = int(m.group(1))
    if num == 0:
        num = len(active)
    if num < 1 or num > len(active):
        feishu_send(chat_id, f"⚠️ 编号超出范围（1-{len(active)}），回复「列表」查看。")
        return
    item = active[num - 1]

    # 冲突检测
    conflict_msg = _check_conflict(item)

    item["status"] = "confirmed"
    # 真实写入 Outlook 日历
    eid = _create_outlook_event_from_item(item)
    if eid:
        item["outlook_event_id"] = eid
    confirmed.setdefault("items", []).append(item)
    pending["items"].remove(item)
    gh_write_json("pending.json", pending, p_sha, "approve")
    try:
        confirmed2, c_sha2 = gh_read_json("confirmed.json")
        confirmed2["items"] = confirmed["items"]
        gh_write_json("confirmed.json", confirmed2, c_sha2, "approve")
    except Exception:
        gh_write_json("confirmed.json", confirmed, c_sha, "approve")
    result_msg = f"📌 {item['subject'][:50]}\n📅 {item.get('date_txt', '时间待定')}\n👤 {item.get('sender', '')}"
    if eid:
        result_msg += "\n\n✅ 已写入 Outlook 日历，iPhone 日历会自动同步显示。"
    else:
        result_msg += "\n\n⚠️ 写入日历失败，稍后 Actions 会补写。"
    if conflict_msg:
        result_msg += f"\n\n⚠️ **冲突提醒**\n{conflict_msg}"
        feishu_send_action(chat_id, "✅ 已加入日历（有冲突）", result_msg, color="orange")
    else:
        feishu_send_action(chat_id, "✅ 已加入日历", result_msg, color="green")
    trigger_github()


def cmd_skip(chat_id, text):
    try:
        pending, p_sha = gh_read_json("pending.json")
    except Exception:
        feishu_send(chat_id, "⚠️ 读取状态失败，请稍后重试。")
        return
    active = [i for i in pending.get("items", []) if i.get("status") == "pending"]
    if not active:
        feishu_send(chat_id, "📋 当前没有待确认的活动。")
        return

    # 批量：跳过全部
    if "全部" in text or "all" in text:
        count = len(active)
        for item in active:
            item["status"] = "skipped"
        pending["items"] = [i for i in pending["items"] if i.get("status") != "pending"]
        gh_write_json("pending.json", pending, p_sha, "skip all")
        feishu_send_action(chat_id, "⏭️ 批量跳过完成", f"已跳过 **{count}** 个活动。", color="blue")
        return

    # 单条跳过
    num = 0
    m = re.search(r'(\d+)', text)
    if m:
        num = int(m.group(1))
    if num == 0:
        num = len(active)
    if num < 1 or num > len(active):
        feishu_send(chat_id, f"⚠️ 编号超出范围（1-{len(active)}），回复「列表」查看。")
        return
    item = active[num - 1]
    item["status"] = "skipped"
    pending["items"].remove(item)
    gh_write_json("pending.json", pending, p_sha, "skip")
    feishu_send_action(chat_id, "⏭️ 已跳过", f"{item['subject'][:50]}", color="blue")


def cmd_schedule(chat_id):
    try:
        events = outlook_events()
        if not events:
            feishu_send(chat_id, "📅 未来两周Outlook日历上没有日程。")
            return
        lines = [f"📅 Outlook日历 · 未来14天日程（共{len(events)}个）：\n"]
        for idx, ev in enumerate(events[:40], 1):
            subj = ev.get("Subject", "") or "(无标题)"
            date_part = (ev.get("Start", {}).get("DateTime", "") or "?")[:10]
            mark = "📮" if "📮" in subj else "　"
            lines.append(f"{idx}. {mark} [{fmt_date(date_part)}] {subj[:40]}")
        lines.append("\n📮=管家标记 | 空格=其他来源")
        lines.append("❌ 回复「删除」+ 编号/日期/名称 删除")
        feishu_send(chat_id, "\n".join(lines))
    except Exception as e:
        feishu_send(chat_id, f"⚠️ 查询日程失败: {e}")


def cmd_smart_delete(chat_id, rest):
    """统一删除：合并管家日历 + Outlook日历，一个列表让用户选"""
    # 收集管家日历活动
    confirmed_items = []
    try:
        confirmed, c_sha = gh_read_json("confirmed.json")
        confirmed_items = confirmed.get("items", [])
    except Exception:
        c_sha = None
        confirmed = {"items": []}

    # 收集Outlook日历活动
    try:
        events = outlook_events()
    except Exception as e:
        feishu_send(chat_id, f"⚠️ 查询日程失败: {e}")
        return

    # 合并成一个列表
    all_items = []
    for item in confirmed_items:
        d = (item.get("dates") or [""])[0]
        all_items.append({
            "type": "管家", "subject": item["subject"], "date": d,
            "fmt": fmt_date(d), "ref": item
        })
    for ev in events:
        subj = ev.get("Subject", "") or "(无标题)"
        date_part = (ev.get("Start", {}).get("DateTime", "") or "?")[:10]
        is_butler = "📮" in subj
        all_items.append({
            "type": "Outlook" + ("📮" if is_butler else ""),
            "subject": subj, "date": date_part,
            "fmt": fmt_date(date_part), "ref": ev
        })

    if not all_items:
        feishu_send(chat_id, "📅 日历上没有任何日程可删除。")
        return

    # 无参数 → 显示统一列表
    if not rest:
        lines = [f"📅 所有日程（共{len(all_items)}个）：\n"]
        for idx, it in enumerate(all_items, 1):
            mark = "📮" if "📮" in it["type"] else "　"
            lines.append(f"{idx}. {mark} [{it['fmt']}] {it['subject'][:40]}")
        lines.append("\n❌ 指定删除（任选）：")
        lines.append("  删除 3 → 按编号")
        lines.append("  删除 10月14 → 按日期")
        lines.append("  删除 生涯 → 按名称")
        lines.append("  删除全部 → 一键清空")
        feishu_send(chat_id, "\n".join(lines))
        return

    # 全部删除
    if rest in ("全部", "all", "所有"):
        deleted = 0
        # 删除管家日历
        if confirmed_items:
            confirmed["items"] = []
            try:
                gh_write_json("confirmed.json", confirmed, c_sha, "delete all")
            except Exception:
                pass
        # 删除Outlook日历
        for ev in events:
            try:
                outlook_delete(ev.get("Id", ""))
                deleted += 1
            except Exception:
                pass
        feishu_send_action(chat_id, "🗑️ 清空完成", f"已清空所有日程\n管家日历 {len(confirmed_items)} 个\nOutlook日历 {deleted} 个", color="red")
        trigger_github()
        return

    # 按日期
    md = parse_md(rest)
    if md:
        date_str = f"{md[0]:02d}-{md[1]:02d}"
        matches = [it for it in all_items if (it.get("date") or "")[5:10] == date_str]
        if not matches:
            feishu_send(chat_id, f"⚠️ 没有 {md[0]}月{md[1]}日 的日程。\n回复「删除」查看清单。")
        elif len(matches) == 1:
            _do_smart_delete_one(chat_id, matches[0], confirmed, c_sha)
        else:
            lines = [f"⚠️ {md[0]}月{md[1]}日 有 {len(matches)} 个日程：\n"]
            for idx, it in enumerate(matches, 1):
                lines.append(f"{idx}. {it['subject'][:44]}")
            lines.append("\n回复「删除 N」进一步指定")
            feishu_send(chat_id, "\n".join(lines))
        return

    # 按编号
    if rest.isdigit():
        num = int(rest)
        if num < 1 or num > len(all_items):
            feishu_send(chat_id, f"⚠️ 编号超出范围（1-{len(all_items)}）。\n回复「删除」获取最新编号。")
            return
        _do_smart_delete_one(chat_id, all_items[num - 1], confirmed, c_sha)
        return

    # 按名称
    matches = [it for it in all_items if rest in it.get("subject", "")]
    if not matches:
        feishu_send(chat_id, f"⚠️ 没有名称含「{rest}」的日程。\n回复「删除」查看清单。")
    elif len(matches) == 1:
        _do_smart_delete_one(chat_id, matches[0], confirmed, c_sha)
    else:
        lines = [f"⚠️ 找到 {len(matches)} 个匹配「{rest}」的日程：\n"]
        for idx, it in enumerate(matches, 1):
            lines.append(f"{idx}. [{it['fmt']}] {it['subject'][:44]}")
        lines.append("\n回复「删除 N」进一步指定")
        feishu_send(chat_id, "\n".join(lines))


def _do_smart_delete_one(chat_id, item, confirmed, c_sha):
    """删除单个日程，自动判断是管家日历还是Outlook日历"""
    subj = item["subject"][:45]
    ref = item["ref"]
    # 管家日历的 item 是 dict（有 id/dates），Outlook 的是 events 列表元素（有 Id）
    if isinstance(ref, dict) and "Id" not in ref and "id" in ref:
        # 管家日历
        try:
            confirmed.get("items", []).remove(ref)
            gh_write_json("confirmed.json", confirmed, c_sha, "delete")
            feishu_send_action(chat_id, "🗑️ 已删除", f"{subj}\n\niPhone日历同步后将自动消失。", color="red")
            trigger_github()
        except Exception as e:
            feishu_send(chat_id, f"⚠️ 删除失败: {e}")
    else:
        # Outlook日历
        try:
            outlook_delete(ref.get("Id", ""))
            feishu_send_action(chat_id, "🗑️ 已从Outlook删除", f"📌 {subj}", color="red")
            # 如果是管家标记的，同步删 confirmed
            if "📮" in item.get("type", ""):
                try:
                    confirmed2, c_sha2 = gh_read_json("confirmed.json")
                    new_items = [i for i in confirmed2.get("items", []) if subj[:20] not in i.get("subject", "")]
                    if len(new_items) != len(confirmed2.get("items", [])):
                        confirmed2["items"] = new_items
                        gh_write_json("confirmed.json", confirmed2, c_sha2, "delete")
                except Exception:
                    pass
            trigger_github()
        except Exception as e:
            feishu_send(chat_id, f"⚠️ 删除失败: {e}")


def cmd_homework(chat_id):
    """查看 LearningMall 作业截止时间（卡片格式）"""
    events = fetch_lm_assignments()
    send_homework_card(chat_id, events)


# ============ AMS 远程签到 ============
AMS_URL = "https://ams.xjtlu.edu.cn"


def _ams_state():
    """读 butler-data 的 xjtlu_state.json，返回 (ams 快照, 完整 state, sha)"""
    try:
        data, sha = gh_read_json("xjtlu_state.json")
        return data.get("ams") or {}, data, sha
    except Exception as e:
        log(f"AMS state 读取失败: {e}")
        return {}, None, ""


def cmd_checkin(chat_id, code):
    """远程签到：AMS 密码签到码（72小时内可补签，无需在教室）"""
    ams, _, _ = _ams_state()
    token = ams.get("token", "")
    if not token:
        triggered = trigger_monitor_refresh()
        feishu_send(chat_id, "❌ 暂无 AMS 签到凭证（x-token）。\n\n"
                             + ("⏳ 已触发监控立刻刷新（约 2~4 分钟），稍后重试即可。" if triggered
                                else "监控每 30 分钟自动刷新一次，稍后再试；若持续失败请检查监控 Actions。"))
        return
    try:
        resp = _http(f"{AMS_URL}/xjtlu/sign/qRCodeSign?code={urllib.parse.quote(code)}&type=2",
                     headers={"x-token": token}, timeout=15)
    except Exception as e:
        feishu_send(chat_id, f"❌ 签到请求失败（AMS 或网络异常）: {e}")
        return
    rc = (resp or {}).get("code")
    msg = (resp or {}).get("message", "")
    if rc == 0:
        feishu_send_action(chat_id, "✅ 签到成功！",
                           f"签到码 {code} 已提交到 AMS。\n"
                           "出勤率将在 24 小时后更新。\n"
                           "若码不对应当前课节，AMS 会拒绝并提示。", color="green")
    elif rc == 1001:
        feishu_send(chat_id, f"❌ 签到失败：{msg or '签到码无效或已过期'}\n\n"
                             "请确认码是否正确。密码签到 72 小时内有效，可让同学转告后重试。")
    else:
        hint = ""
        if _ams_token_dead(resp):
            hint = ("\n\n⏳ 已触发监控立刻刷新凭证（约 2~4 分钟），稍等后重试即可。"
                    if trigger_monitor_refresh() else "\n\n💡 x-token 已过期且刷新失败，请检查监控 Actions。")
        else:
            hint = "\n\n💡 x-token 可能已过期，等监控下次运行自动刷新后再试。" \
               if (rc in (401, 403) or "token" in str(msg).lower()) else ""
        feishu_send(chat_id, f"❌ 签到失败（code={rc}）：{msg or '未知错误'}{hint}")


def cmd_attendance(chat_id):
    """查询 AMS 出勤率统计（数据来自监控最近一次同步）"""
    ams, _, _ = _ams_state()
    att = ams.get("attendance") or {}
    if not att:
        feishu_send(chat_id, "📭 出勤数据尚未同步。\n\n监控每 30 分钟拉取一次 AMS，稍后再试。")
        return
    overall = att.get("overall", "?")
    warn = "\n🚨 已触发学校出勤率阈值警告，请尽快联系 DA！" if att.get("threshold") else ""
    lines = [f"📊 AMS 考勤统计 · 总出勤率 {overall}%{warn}"]
    modules = att.get("modules") or {}
    for code, m in sorted(modules.items()):
        flag = " ⚠️阈值" if m.get("threshold") else ""
        lines.append(f"· {code} {m.get('att', '?')}%"
                     f"（已签 {m.get('sign_hours', 0)}/{m.get('total_hours', 0)} 课时，缺勤 {m.get('absences', 0)} 次）{flag}")
    last = ams.get("last_check", "")
    if last:
        lines.append(f"\n⏱️ 数据同步于 {last}")
    feishu_send(chat_id, "\n".join(lines))


# ============ AMS 实时轮询（1分钟 Timer 触发） ============
# 实测 AMS status 语义：0=未签 1=Present已签到 2=Authorized Absence已准假 3=缺勤 4=Excluded免签
AMS_STATUS_NAME = {1: "已签到", 2: "已准假", 3: "缺勤", 4: "免签不计入"}


def ams_timer_poll():
    """1分钟 Timer：课节状态实时监控（纯 API，x-token 自足，无需浏览器）
    信号1: status 0→1 签到成功自动确认
    信号2: status 0→2/3 缺勤/准假标记 → 即时告警（72小时补签窗口内快速反应）
    信号3: 上课/将上课且未签 → 签到提醒（每课节最多1次）
    """
    try:
        ams, state_data, sha = _ams_state()
        token = ams.get("token", "")
        if not token or not state_data or not sha:
            log("AMS 轮询跳过：无凭证或 state 不可读")
            return
        resp = _http(f"{AMS_URL}/xjtlu/xjtlu-base-student-timetables/getStudentTimetable",
                     headers={"x-token": token}, timeout=12)
        if not isinstance(resp, dict) or resp.get("code") != 0 or not resp.get("data"):
            log(f"AMS 轮询跳过：API code={resp and resp.get('code')}")
            return
        now_sessions = {}
        for day in resp["data"]:
            for s in day.get("timetableVoList", []):
                rid = str(s.get("signInRecordId") or "")
                if rid:
                    now_sessions[rid] = {
                        "code": s.get("moduleCode", ""),
                        "time_text": s.get("moduleTime", ""),
                        "start": s.get("startTime", ""),
                        "status": s.get("status"),
                        "signTime": s.get("signTime"),
                    }
        if not now_sessions:
            return
        old_sessions = ams.get("sessions") or {}
        reminded = set(ams.get("reminded", []))
        now = datetime.now()
        notes = []
        changed = False

        # 1. status 变化检测（只在 monitor 已建基线的课节上，避免新课节误报）
        for rid, s in now_sessions.items():
            old = old_sessions.get(rid)
            if not old or s["status"] is None or s["status"] == old.get("status"):
                continue
            name = AMS_STATUS_NAME.get(s["status"], f"状态{s['status']}")
            if s["status"] == 1:
                stime = f"（{s['signTime']}）" if s.get("signTime") else ""
                notes.append(f"✅ AMS：{s['code']} {s['time_text']} 签到成功已记录{stime}")
            elif s["status"] in (2, 3):
                notes.append(f"❗ AMS：{s['code']} {s['time_text']} 被标记为「{name}」\n\n"
                             "若与实际不符：缺勤可让同学转告签到码，回复「签到 码」72小时内补签；"
                             "或课后联系老师在 AMS 直接修正。")
            elif s["status"] == 0:
                notes.append(f"↩️ AMS：{s['code']} {s['time_text']} 状态被重置为「未签到」，请留意老师是否开启了签到")
            else:
                notes.append(f"AMS：{s['code']} {s['time_text']} 状态变化 → {name}")
            changed = True

        # 2. 上课签到提醒（开课前10分钟~上课中，status=0，每课节只提醒一次）
        for rid, s in now_sessions.items():
            if s["status"] != 0 or rid in reminded:
                continue
            try:
                st = datetime.strptime(s["start"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if st - timedelta(minutes=10) <= now <= st + timedelta(hours=2):
                notes.append(f"⏰ {s['code']} {s['time_text']} 即将开始/正在上课\n\n"
                             "老师开始签到后：回复「签到 码」或直接发纯数字码，我帮你签（密码72小时有效）。")
                reminded.add(rid)
                changed = True

        if notes:
            feishu_send(CHAT_ID_FALLBACK, "\n\n".join(notes))
            log(f"AMS 轮询发现 {len(notes)} 条信号，已推送")

        # 3. 变化写回（只合并 status/signTime，完整快照结构由 monitor 维护；sha 冲突自动重试）
        if changed:
            merged = dict(old_sessions)
            for rid, s in now_sessions.items():
                if rid in merged:
                    merged[rid]["status"] = s["status"]
                    merged[rid]["signTime"] = s.get("signTime")
            ams["sessions"] = merged
            ams["reminded"] = [rid for rid in reminded if rid in merged]
            try:
                state_data["ams"] = ams
                gh_write_json("xjtlu_state.json", state_data, sha, "ams timer update")
                log("AMS 轮询状态已写回 butler-data")
            except Exception as e:
                log(f"AMS 状态写回失败（下轮重试）: {e}")
    except Exception as e:
        log(f"AMS 轮询异常: {e}")


def ams_try_auto_sign(t, chat_id):
    """智能签到入口：上课时间发纯数字码或扫码URL → 自动签到。返回 True 已处理"""
    # 二维码扫码结果 / 含 code 参数的 URL（同学转发或相册识别复制）
    if "ams.xjtlu.edu.cn" in t or "code=" in t:
        m = re.search(r"code[=:/]\s*([A-Za-z0-9\-]{3,24})", t)
        if m:
            cmd_checkin(chat_id, m.group(1))
            return True
    # 纯数字 3-8 位：仅当前有课（开课前10分钟~下课）时视为签到码，其余情况当聊天不处理
    if re.fullmatch(r"\d{3,8}", t):
        ams, _, _ = _ams_state()
        sessions = ams.get("sessions") or {}
        now = datetime.now()
        for s in sessions.values():
            if s.get("status") != 0:
                continue
            try:
                st = datetime.strptime(s.get("start", ""), "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if st - timedelta(minutes=10) <= now <= st + timedelta(hours=3):
                cmd_checkin(chat_id, t)
                return True
    return False


# ============ AMS 请假（Authorized Absence） ============
# 全链路实测（2026-10-02）：
#   查课节: GET /xjtlu/stuapi/xjtlu-leave/getLeaveModuleListByDate?startDate=..&endDate=.. → 当天课节+准假额度
#   传证明: POST /xjtlu/file/uploadToObject（multipart，字段 file）→ 返回 originalFileName + fileUrl
#   提交:   POST /xjtlu/stuapi/xjtlu-leave/addLeave（JSON）
#           {startDate, endDate, leaveType, leaveReason, fileName, fileUrl, leaveModuleList:[课节对象]}
#   规则:   日期窗口 ±7 天；每门课准假 ≤30% 课时；附件必传（rar/zip/pdf/png/jpg，≤10M）
AMS_LEAVE_PENDING_FILE = "ams_leave_pending.json"
LEAVE_TYPES_OFFICIAL = [
    "Illness or injury", "Bereavement",
    "Serious illness of next of kin", "Unforeseen/unpreventable event",
]


def _http_bin(url, headers=None, timeout=30):
    """下载二进制（飞书图片资源）"""
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_upload_ams(url, token, filename, data):
    """AMS multipart 上传（纯标准库手写 multipart）"""
    boundary = "PythonBoundary" + str(int(time.time() * 1000))
    parts = []
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    parts.append(data)
    parts.append(f'\r\n--{boundary}--\r\n'.encode())
    body = b"".join(parts)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "x-token": token, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _load_leave_pending():
    try:
        d, sha = gh_read_json(AMS_LEAVE_PENDING_FILE)
        if isinstance(d, dict) and d.get("status"):
            return d, sha
    except Exception:
        pass
    return {}, ""


def _save_leave_pending(data):
    """保存/清空待提交请假（data={} 即清空）"""
    try:
        old, sha = gh_read_json(AMS_LEAVE_PENDING_FILE)
    except Exception:
        old, sha = None, ""
    gh_write_json(AMS_LEAVE_PENDING_FILE, data, sha, "ams leave pending")


def parse_leave_date(text, now=None):
    """解析请假日期：10-05 / 10月5日 / 2026-10-05 / 明天 / 后天 / 今天 / 周一~周日"""
    now = now or datetime.now()
    # 规范化：去掉日期数字与分隔符之间的空格（「10 月 11」→「10月11」）
    t = re.sub(r"(\d)\s*([-月.])\s*(\d)", r"\1\2\3", str(text or ""))
    t = t.replace("年", "-").replace("月", "-").replace("日", "")
    # 明确日期：MM-DD 或 YYYY-MM-DD
    m = re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    m = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", t)
    if m:
        try:
            d = datetime(now.year, int(m.group(1)), int(m.group(2)))
            # H4 修复：日期已过则跨年（原代码比较 1月1日几乎永不为 True）
            if d < now.replace(hour=0, minute=0, second=0, microsecond=0):
                d = datetime(now.year + 1, int(m.group(1)), int(m.group(2)))
            return d.strftime("%Y-%m-%d")
        except ValueError:
            return None
    if "后天" in text:
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    if "明天" in text or "tmr" in t or "tomorrow" in t:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if "今天" in text or "today" in t:
        return now.strftime("%Y-%m-%d")
    wd_map = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6,
              "星期一": 0, "星期二": 1, "星期三": 2, "星期四": 3, "星期五": 4, "星期六": 5, "星期日": 6}
    for name, wd in wd_map.items():
        if name in text:
            delta = (wd - now.weekday()) % 7 or 7
            return (now + timedelta(days=delta)).strftime("%Y-%m-%d")
    return None


def _guess_leave_type(text):
    """从口语原因判断官方请假类型（家人重病优先于伤病，避免关键词误判）"""
    t = text.lower()
    if any(k in t for k in ["丧", "去世", "白事", "bereave", "funeral", "pass away"]):
        return "Bereavement"
    if any(k in t for k in ["家人", "父母", "妈妈", "爸爸", "母亲", "父亲", "爷爷", "奶奶", "外公", "外婆",
                            "亲属", "kin", "family"]) \
            and any(k in t for k in ["病", "住院", "重", "手术", "ill", "hospital"]):
        return "Serious illness of next of kin"
    if any(k in t for k in ["伤", "崴", "瘸", "骨折", "扭", "病", "发烧", "感冒", "咳嗽", "肚子",
                            "疼", "痛", "医院", "医生", "不舒服", "头晕", "呕吐", "ill", "sick", "injur",
                            "fever", "cold", "hospital", "doctor", "sprain", "fracture", "腿", "脚"]):
        return "Illness or injury"
    return "Unforeseen/unpreventable event"


def _translate_reason_en(text):
    """把中文请假原因翻译成一句正式英文（用于 AMS 假条正文）；失败返回 None"""
    if not any('\u4e00' <= ch <= '\u9fff' for ch in text):
        return text.strip()  # 已是英文
    if not DEEPSEEK_API_KEY:
        return None
    try:
        resp = _http("https://api.deepseek.com/v1/chat/completions",
                     method="POST",
                     headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                     data={"model": "deepseek-chat",
                           "messages": [
                               {"role": "system",
                                "content": "You are a translator. Translate the student's "
                                           "leave-of-absence reason into ONE formal English "
                                           "sentence in the first person (starting with 'I ...'). "
                                           "Output ONLY the English sentence - no Chinese, "
                                           "no quotes, no markdown, no explanations."},
                               {"role": "user", "content": text}],
                           "max_tokens": 160, "temperature": 0.3},
                     timeout=15)
        out = (((resp.get("choices") or [{}])[0].get("message") or {}).get("content", "") or "").strip()
        out = out.strip('"').strip("'").strip()
        if out and not any('\u4e00' <= ch <= '\u9fff' for ch in out):
            return out
        return None
    except Exception as e:
        log(f"原因翻译失败: {e}")
        return None


def _format_leave_reason(user_reason, leave_type):
    """把口语原因整理成正式、真诚的英文 Reason（学生口吻，给 DA 和老师看）"""
    detail = user_reason.strip().rstrip("。.").strip()
    # 原因含中文时先翻译成正式英文，保证假条正文纯英文（不再出现中英混合）
    if any('\u4e00' <= ch <= '\u9fff' for ch in detail):
        en = _translate_reason_en(detail)
        if en:
            detail = en.rstrip(".")
    if leave_type == "Illness or injury":
        return (f"I am unable to attend the selected session(s) due to a medical condition: {detail}. "
                f"I have sought medical advice where necessary, and the medical certificate is attached "
                f"as supporting evidence. While I am away, I will keep up with the course by studying the "
                f"session materials on LearningMall and referring to a classmate's notes. "
                f"I would be grateful if my absence could be recorded as an authorised absence, "
                f"and I expect to return to class as soon as I have recovered.")
    if leave_type == "Bereavement":
        return (f"I am unable to attend the selected session(s) due to a bereavement in my family: {detail}. "
                f"The relevant supporting document is attached. I will catch up on the session materials on "
                f"LearningMall and a classmate's notes during this difficult time. "
                f"I would be grateful for your understanding and kindly request an authorised absence.")
    if leave_type == "Serious illness of next of kin":
        return (f"I am unable to attend the selected session(s) because a close family member is seriously "
                f"ill and I need to be with them / care for them: {detail}. Supporting evidence is attached. "
                f"I will keep up with the course materials on LearningMall and catch up as soon as possible. "
                f"I kindly request an authorised absence for the selected session(s).")
    return (f"I am unable to attend the selected session(s) due to an unforeseen and unpreventable "
            f"circumstance: {detail}. Supporting evidence is attached where available. I will study the "
            f"session materials on LearningMall and catch up with a classmate's notes, so that my studies "
            f"are not affected. I kindly request an authorised absence for the selected session(s).")


def trigger_monitor_refresh():
    """触发 xjtlu-monitor workflow 立刻运行：刷新 AMS x-token（不等 30 分钟 cron）"""
    if not GH_TOKEN:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/workflows/xjtlu-monitor.yml/dispatches",
            data=json.dumps({"ref": "main"}).encode(), method="POST",
            headers={"Authorization": f"Bearer {GH_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        log("✅ 已触发 monitor 立刻刷新 AMS x-token")
        return True
    except Exception as e:
        log(f"触发 monitor 刷新失败: {e}")
        return False


def _ams_token_dead(resp):
    """AMS 响应是否为凭证失效（实测 401 响应：The account is invalid. Please log in again.）"""
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("message", "")).lower()
    return resp.get("code") == 401 or "invalid" in msg or "log in again" in msg


def cmd_leave(chat_id, text):
    """请假第1步：解析日期/课程/原因 → 查课节 → 生成假条 → 预览并等图片证明
    用法：请假 10-05 EAP043 崴脚了去不了 | 请假 明天 全天 病了
    """
    rest = re.sub(r"^(?:请假|请个假|申请请假|authorized absence|leave)", "", text.strip(), flags=re.I).strip()
    if not rest:
        feishu_send(chat_id, "🏫 AMS 请假用法：\n\n⬇️ 直接复制发送：\n【请假 10-05 崴脚了去不了学校】\n\n⬇️ 或日期范围：\n【请假 10-05 10-14 崴脚了】\n\n"
                             "流程：我先写好假条给你确认 → 你发病假条照片 → 自动提交到 AMS 等 DA 审批。\n"
                             "规则：缺课日前后 7 天内可申请；每门课最多准假 30% 课时；证明材料必传。")
        return
    ams, _, _ = _ams_state()
    token = ams.get("token", "")
    if not token:
        triggered = trigger_monitor_refresh()
        feishu_send(chat_id, "❌ 暂无 AMS 凭证。\n\n"
                             + ("⏳ 已触发监控立刻刷新（约 2~4 分钟），稍后重说一遍「请假 日期 原因」即可。" if triggered
                                else "监控每 30 分钟自动刷新一次，稍后再试；若持续失败请检查监控 Actions。"))
        return
    # 1. 解析日期（支持范围：「请假 10-05 10-14 原因」「请假 10月5日到10月14日 原因」）
    end_date = None
    m_range = re.search(r"((?:\d{1,2}[-月.]\d{1,2}日?|今天|明天|后天)(?:\s*(?:到|至|~|—)\s*|\s+)(\d{1,2}[-月.]\d{1,2}日?|今天|明天|后天))", rest)
    if m_range:
        date = parse_leave_date(m_range.group(1))
        end_date = parse_leave_date(m_range.group(2))
        if date and end_date and end_date < date:
            date, end_date = end_date, date
    else:
        date = parse_leave_date(rest)
    if not date:
        feishu_send(chat_id, "❓ 没看懂日期，试试「请假 10-05」或「请假 10-05 10-14」+ 原因。")
        return
    if not end_date:
        end_date = date
    d = datetime.strptime(date, "%Y-%m-%d")
    d2 = datetime.strptime(end_date, "%Y-%m-%d")
    if abs((d - datetime.now()).days) > 7 or (d2 - d).days > 14:
        feishu_send(chat_id, f"⚠️ {date}~{end_date} 超出申请窗口（缺课日前后 7 天内，单次最长 14 天），AMS 不接受。")
        return
    # 2. 解析课程过滤（可选）：EAP043 / 体育 / 微积分...
    course_filter = None
    m = re.search(r"\b([A-Z]{3}\d{3})\b", text.upper())
    if m:
        course_filter = m.group(1)
    else:
        for cn, code in [("体育", "PHE001"), ("微积分", "MTH026"), ("线代", "MTH028"), ("学术英语", "EAP043"),
                         ("英语", "EAP043"), ("科学", "SCI004"), ("新兴技术", "PSP004"), ("马原", "CCT001"),
                         ("毛概", "CCT011"), ("形势", "CCT012"), ("心理", "CCT007")]:
            if cn in text:
                course_filter = code
                break
    # 3. 原因 = 去掉日期和课程码后的文本
    reason_raw = re.sub(r"\b([A-Z]{3}\d{3})\b", "", rest, flags=re.I).strip()
    reason_raw = re.sub(r"\d{1,2}[-月.]\d{1,2}(日|号)?", "", reason_raw).strip()
    reason_raw = re.sub(r"(明天|后天|今天|周[一二三四五六日天]|星期[一二三四五六日天])", "", reason_raw).strip(" ，,.-到至~")
    if not reason_raw:
        feishu_send(chat_id, "❓ 请补充请假原因，例如「请假 10-05 崴脚了腿瘸了去不了学校」。")
        return
    # 4. 查范围内课节（AMS 支持起止日期，返回 dateList 多天）
    try:
        resp = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/getLeaveModuleListByDate"
                     f"?startDate={date}&endDate={end_date}", headers={"x-token": token}, timeout=15)
    except Exception as e:
        feishu_send(chat_id, f"❌ 查询课节失败：{e}")
        return
    if _ams_token_dead(resp):
        if trigger_monitor_refresh():
            feishu_send(chat_id, "⏳ AMS 凭证刚好过期，我已触发监控立刻刷新（约 2~4 分钟）\n\n"
                                 "稍等几分钟后重说一遍「请假 日期 原因」即可，假条不会丢。")
        else:
            feishu_send(chat_id, "❌ AMS 凭证过期且自动刷新失败，请检查监控 Actions 是否正常。")
        return
    if not isinstance(resp, dict) or resp.get("code") != 0 or not resp.get("data"):
        feishu_send(chat_id, "📭 该日期没有可请假的课节（可能没课或超出范围）。")
        return
    data = resp["data"]
    all_sessions = []
    sess_days = []   # 与 all_sessions 平行：每节课所属日期（用于按天分组预览）
    for day in (data.get("dateList") or []):
        day_date = None
        for _k in ("date", "leaveDate", "day", "moduleDate"):
            _m = re.search(r"\d{4}-\d{2}-\d{2}", str(day.get(_k) or ""))
            if _m:
                day_date = _m.group(0)
                break
        for s in (day.get("leaveModuleList") or []):
            if course_filter and s.get("moduleCode") != course_filter:
                continue
            if not day_date:
                _m = re.search(r"\d{4}-\d{2}-\d{2}", str(s.get("time") or ""))
                if _m:
                    day_date = _m.group(0)
            all_sessions.append(s)
            sess_days.append(day_date)
    if not all_sessions:
        feishu_send(chat_id, f"📭 {date}~{end_date} 没有{(' ' + course_filter) if course_filter else ''}的课节。")
        return
    leave_type = _guess_leave_type(text)
    leave_reason = _format_leave_reason(reason_raw, leave_type)
    # 5. 暂存待提交申请
    pending = {
        "status": "awaiting_attachment",
        "startDate": date, "endDate": end_date,
        "leaveType": leave_type, "leaveReason": leave_reason,
        "leaveModuleList": all_sessions,
        "course_filter": course_filter,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "created_ts": time.time(),
    }
    _save_leave_pending(pending)
    # 6. 预览（多天范围：按天分组紧凑显示，避免逐节罗列过长；单日保持详细格式）
    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d.weekday()]
    wd2 = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][d2.weekday()]
    date_disp = f"{date} ~ {end_date}（{wd} ~ {wd2}）" if end_date != date else f"{date}（{wd}）"
    if end_date != date and sess_days and all(sess_days):
        by_day = {}
        for _s, _dd in zip(all_sessions, sess_days):
            by_day.setdefault(_dd or date, []).append(_s)
        lines = [f"📝 假条已写好，请确认（{date_disp}）\n\n"
                 f"**要请假的课节（共 {len(all_sessions)} 节 · {len(by_day)} 天）：**"]
        for _dstr in sorted(by_day.keys()):
            try:
                _dd = datetime.strptime(_dstr, "%Y-%m-%d")
                _wdd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][_dd.weekday()]
                _dlabel = f"{_dstr[5:]}（{_wdd}）"
            except Exception:
                _dlabel = _dstr
            _segs = []
            for _s in by_day[_dstr]:
                # 真实数据：课节有 startTime/endTime 字段；time 为 "09:00～11:00"（全角～）
                _st, _et = _s.get("startTime"), _s.get("endTime")
                if _st and _et:
                    _tstr = f"{_st}-{_et}"
                else:
                    _t = str(_s.get("time") or "")
                    _mt = re.search(r"(\d{1,2}:\d{2})\s*[~～\-—到]\s*(\d{1,2}:\d{2})", _t)
                    _tstr = f"{_mt.group(1)}-{_mt.group(2)}" if _mt else _t
                _segs.append(f"{_s.get('moduleCode')} {_tstr} {_s.get('moduleTypeGroup') or ''}".strip())
            lines.append(f"· {_dlabel}（{len(by_day[_dstr])} 节）：" + " · ".join(_segs))
    else:
        lines = [f"📝 假条已写好，请确认（{date_disp}）\n\n**要请假的课节（{len(all_sessions)} 节）：**"]
        for s in all_sessions:
            quota = f"，该课已准假 {s.get('leaveCount', 0)}/{s.get('totalCount', 0)} 课时" if s.get("totalCount") else ""
            lines.append(f"· {s.get('moduleCode')} {s.get('moduleTitle', '')[:36]}\n  {s.get('time', '')} · {s.get('moduleTypeGroup', '')}{quota}")
    lines.append(f"\n**类型：** {leave_type}")
    lines.append(f"**假条正文（提交到 AMS 的 Reason）：**\n{leave_reason}")
    lines.append("\n**学期准假额度剩余：** " + str(data.get("semesterLeaveRate", "?")) + "%")
    lines.append("\n✅ 下一步：把病假条/证明照片直接发到群里，我收到后自动上传并提交申请。\n"
                 "✏️ 想改内容：发「改假条 <新原因>」重新生成；发「取消请假」放弃。")
    feishu_send(chat_id, "\n".join(lines))


def cmd_leave_confirm_attachment(chat_id, image_bytes, filename):
    """请假第2步：收到证明图片 → 上传 AMS → 提交 addLeave → 清 pending"""
    pending, _ = _load_leave_pending()
    if not pending or pending.get("status") != "awaiting_attachment":
        return False
    # 防误触：预览超 30 分钟后发图不再自动提交，需重新发起
    if pending.get("created_ts") and time.time() - pending["created_ts"] > 1800:
        _save_leave_pending({})
        feishu_send(chat_id, "⌛ 上一次的请假预览已超时（30 分钟保护已清空）。\n\n"
                             "如仍需请假，请重新发「请假 日期 原因」，我再写一份假条。")
        return True
    ams, _, _ = _ams_state()
    token = ams.get("token", "")
    if not token:
        feishu_send(chat_id, "❌ AMS 凭证失效，等监控刷新后重发图片。")
        return True
    try:
        up = _http_upload_ams(f"{AMS_URL}/xjtlu/file/uploadToObject", token, filename, image_bytes)
        if _ams_token_dead(up):
            trigger_monitor_refresh()
            feishu_send(chat_id, "⏳ AMS 凭证刚好过期，已触发监控刷新（约 2~4 分钟）。\n\n"
                                 "申请还暂存着，几分钟后重发这张图片即可提交。")
            return True
        if up.get("code") != 0 or not (up.get("data") or {}).get("fileUrl"):
            feishu_send(chat_id, f"❌ 证明上传失败：{up.get('message', '未知错误')}")
            return True
        fd = up["data"]
        # 关键修复（2026-10-03 实测）：AMS 课节的内部 ID 是动态的，暂存几分钟就会失效导致 addLeave 500；
        # 提交前必须用 pending 的日期范围重新查课节，用新鲜数据组装 body
        fresh_resp = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/getLeaveModuleListByDate"
                           f"?startDate={pending['startDate']}&endDate={pending['endDate']}",
                           headers={"x-token": token}, timeout=15)
        if _ams_token_dead(fresh_resp):
            trigger_monitor_refresh()
            feishu_send(chat_id, "⏳ AMS 凭证刚好过期，已触发监控刷新（约 2~4 分钟）。\n\n"
                                 "申请还暂存着，几分钟后重发这张图片即可提交。")
            return True
        fresh_sessions = []
        for day in ((fresh_resp.get("data") or {}).get("dateList") or []):
            for s in (day.get("leaveModuleList") or []):
                if pending.get("course_filter") and s.get("moduleCode") != pending["course_filter"]:
                    continue
                fresh_sessions.append(s)
        if not fresh_sessions:
            feishu_send(chat_id, f"❌ 课节查询为空（{pending['startDate']}~{pending['endDate']}）："
                                 f"可能已被其他申请占用或超出窗口。\n\n"
                                 f"申请暂存已清空，请重新发「请假 日期 原因」。")
            _save_leave_pending({})
            return True
        body = {
            "startDate": pending["startDate"], "endDate": pending["endDate"],
            "leaveType": pending["leaveType"], "leaveReason": pending["leaveReason"],
            "fileName": fd.get("originalFileName", filename), "fileUrl": fd.get("fileUrl", ""),
            "leaveModuleList": fresh_sessions,
        }
        resp = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/addLeave", method="POST",
                     headers={"x-token": token, "Content-Type": "application/json"},
                     data=body, timeout=20)
        if _ams_token_dead(resp):
            trigger_monitor_refresh()
            feishu_send(chat_id, "⏳ AMS 凭证刚好过期，已触发监控刷新（约 2~4 分钟）。\n\n"
                                 "申请还暂存着，几分钟后重发这张图片即可提交。")
            return True
    except Exception as e:
        feishu_send(chat_id, f"❌ 提交申请失败：{e}\n\n申请已暂存，可重发图片再试，或发「取消请假」放弃。")
        return True
    if isinstance(resp, dict) and resp.get("code") == 0:
        _save_leave_pending({})
        n = len(fresh_sessions)
        feishu_send_action(chat_id, "✅ 请假申请已提交",
                           f"{pending['startDate']}{'~' + pending['endDate'] if pending['endDate'] != pending['startDate'] else ''} · {n} 节课 · {pending['leaveType']}\n\n"
                           "已提交到 AMS，等你的 DA 审批。通过后课节状态变为「已准假」，不影响出勤率。\n"
                           "审批结果管家监控到变化会提醒你。", color="green")
    else:
        msg = (resp or {}).get("message", "未知错误") if isinstance(resp, dict) else str(resp)
        feishu_send(chat_id, f"❌ AMS 拒绝了申请：{msg}\n\n"
                             "常见原因：格式不符、额度已满或该课节不可申请。\n"
                             "申请仍暂存着，可「取消请假」放弃。")
    return True


def cmd_leave_cancel(chat_id):
    pending, _ = _load_leave_pending()
    if pending.get("status"):
        _save_leave_pending({})
        feishu_send(chat_id, "🗑️ 已取消本次请假申请（未提交到 AMS）。")
    else:
        feishu_send(chat_id, "📭 当前没有进行中的请假申请。")


def _revoke_actually_done(leave_odd, date_arg):
    """验证撤回是否真正生效（AMS 受理≠生效）：
    全撤=该申请已无 Pending 课节；按天撤=该日期课节全部 Canceled(status==4)"""
    ams, _, _ = _ams_state()
    token = ams.get("token", "")
    if not token:
        return False
    try:
        resp = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/getLeaveList?pageNum=1&pageSize=10",
                     headers={"x-token": token}, timeout=15)
        rows = (resp.get("data") or {}).get("leaveList") or []
        app = next((r for r in rows if r.get("leaveOdd") == leave_odd), None)
        if not app:
            return True  # 申请已不在列表（极端情况视为生效）
        det = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/detail/{app.get('leaveId')}",
                    headers={"x-token": token}, timeout=15)
        mlist = (det.get("data") or {}).get("moduleList") or []
        if not mlist:
            return True
        if not date_arg:
            return all(m.get("status") != 2 for m in mlist)
        day_mods = [m for m in mlist if date_arg in str(m.get("time") or "")]
        return bool(day_mods) and all(m.get("status") == 4 for m in day_mods)
    except Exception:
        return False


def cmd_leave_revoke(chat_id, arg=""):
    """撤回请假（2026-10-03 实测破译：AMS 撤回是【课节级】，参数为 leaveModuleId，动态需现拉现用）
    用法：「撤回请假」= 撤最近一条待审批申请的全部课节；「撤回 10-11」= 只撤该日期的课节
    流程：getLeaveList 拿最新 leaveId → detail 拿课节列表 → cancelLeave/{leaveModuleId} 逐节撤
    反馈两段式（用户实测要求）：发起→「正在处理中」→ 验证 AMS 真正生效→ 才发「已撤回」；
    30 秒内未生效则挂起，由轮询兜底验证后再通知，绝不提前宣称完成。"""
    ams, _, _ = _ams_state()
    token = ams.get("token", "")
    if not token:
        feishu_send(chat_id, "❌ 暂无 AMS 凭证，无法撤回。")
        return
    date_arg = None
    # 规范化日期内空格（「10 月 11」→「10月11」），否则提取不到导致误撤全部
    arg_norm = re.sub(r"(\d)\s*([-月.])\s*(\d)", r"\1\2\3", arg)
    # 先试 YYYY-MM-DD（避免被 MM-DD 正则提取出中间数字）
    m_full = re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", arg_norm)
    if m_full:
        try:
            date_arg = datetime(int(m_full.group(1)), int(m_full.group(2)),
                                 int(m_full.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    else:
        m = re.search(r"(?<!\d)(\d{1,2})[-月.](\d{1,2})(?!\d)", arg_norm)
        if m:
            date_arg = parse_leave_date(m.group(1))
    try:
        resp = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/getLeaveList?pageNum=1&pageSize=5",
                     headers={"x-token": token}, timeout=15)
        if _ams_token_dead(resp):
            trigger_monitor_refresh()
            feishu_send(chat_id, "⏳ AMS 凭证过期，已触发刷新（约2~4分钟），稍后重试撤回。")
            return
        rows = (resp.get("data") or {}).get("leaveList") or []
        pending_apps = [r for r in rows if r.get("status") == 1]
        if not pending_apps:
            feishu_send(chat_id, "📭 没有待审批（Pending）的请假申请可撤回。\n\n"
                                 "注：AMS 只能撤「待审批」状态；已办结/已批准的记录是历史，无法删除。")
            return
        # 如果有多条 Pending 申请且未指定日期，全部撤回（HELP_TEXT 承诺「全撤」）
        # 指定日期时只处理第一条（日期过滤会精确匹配课节）
        all_targets = []
        all_app = []
        for app in pending_apps:
            det = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/detail/{app.get('leaveId')}",
                        headers={"x-token": token}, timeout=15)
            mlist = (det.get("data") or {}).get("moduleList") or []
            # 课节 status==2 为 Pending 可撤；==4 为 Canceled
            app_targets = [mo for mo in mlist
                           if mo.get("status") == 2
                           and (not date_arg or date_arg in str(mo.get("time") or ""))]
            if app_targets:
                all_targets.extend(app_targets)
                all_app.append(app)
        targets = all_targets
        if not targets:
            scope_hint = f"{(' ' + date_arg) if date_arg else ''}"
            if len(pending_apps) == 1:
                feishu_send(chat_id, f"📭 申请（{pending_apps[0].get('startDate')}~{pending_apps[0].get('endDate')}）里没有"
                                     f"{scope_hint} 待审批的课节可撤。")
            else:
                feishu_send(chat_id, f"📭 {len(pending_apps)} 条待审批申请里没有{scope_hint} 待审批的课节可撤。")
            return
        ok = 0
        for mo in targets:
            try:
                c = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/cancelLeave/{mo.get('leaveModuleId')}",
                          headers={"x-token": token}, timeout=15)
                if isinstance(c, dict) and c.get("code") == 0:
                    ok += 1
                time.sleep(0.3)
            except Exception:
                pass
        if ok == 0:
            feishu_send(chat_id, "❌ 撤回指令全部被 AMS 拒绝，请稍后重试或联系管理员。")
            return
        # 第 1 段：立即告知"正在处理中"（不宣称完成）
        scope = date_arg or "全部"
        app_desc = f"{len(all_app)} 条申请" if len(all_app) > 1 else f"申请 {all_app[0].get('leaveOdd', '')}"
        feishu_send(chat_id, f"⏳ 正在处理中：撤回 {scope} 课节的指令已提交 AMS（{ok}/{len(targets)} 节受理，{app_desc}）。\n"
                             f"正在确认 AMS 真正生效，确认后立刻通知你，一般几秒到几分钟。")
        # 第 2 段：同步验证循环（最多约 30 秒）
        odd = all_app[0].get("leaveOdd") if all_app else ""
        app_start = all_app[0].get("startDate") if all_app else ""
        app_end = all_app[0].get("endDate") if all_app else ""
        done = False
        for _ in range(10):
            time.sleep(3)
            if _revoke_actually_done(odd, date_arg):
                done = True
                break
        if done:
            _mark_leave_status(odd, 2)   # 同步快照，防审批监控误报
            feishu_send_action(chat_id, "✅ 请假已撤回（已验证生效）",
                f"申请 {odd}（{app_start}~{app_end}）\n"
                f"{scope} 课节 {len(targets)} 节已在 AMS 真正撤销（状态：已办结/Cancelled）。\n\n"
                f"注：撤回记录保留作历史，不会再生效或占额度。", color="green")
        else:
            # 30 秒未生效：挂起，轮询兜底验证后再发完成通知
            try:
                gh_write_json("revoke_check.json",
                              {"odd": odd, "date": date_arg, "n": len(targets), "ts": time.time()},
                              "", "revoke pending confirm")
            except Exception:
                pass
            feishu_send(chat_id, f"⏳ AMS 还在处理撤回（{scope} {len(targets)} 节），已受理但尚未生效。\n"
                                 f"生效后我会立刻通知你，无需再发指令。")
    except Exception as e:
        feishu_send(chat_id, f"❌ 撤回异常：{e}")


# 同一轮询周期内缓存 revoke_check 的 odd，供 _check_leave_approval 复用（避免重复读 GitHub）
_REVOKE_CHECK_ODD_CACHE = ""


def _check_pending_revoke(chat_id):
    """轮询兜底：验证之前挂起的撤回是否真正生效，生效则发完成通知"""
    global _REVOKE_CHECK_ODD_CACHE
    _REVOKE_CHECK_ODD_CACHE = ""
    try:
        rc, sha = gh_read_json("revoke_check.json")
        if not (isinstance(rc, dict) and rc.get("odd")):
            return
        _REVOKE_CHECK_ODD_CACHE = str(rc.get("odd") or "")
        if _revoke_actually_done(rc.get("odd"), rc.get("date")):
            _mark_leave_status(rc.get("odd"), 2)   # 同步快照，防审批监控误报
            gh_write_json("revoke_check.json", {}, sha, "revoke done")
            scope = rc.get("date") or "全部"
            feishu_send_action(chat_id, "✅ 请假已撤回（已验证生效）",
                f"AMS 已处理完成：{scope} 课节 {rc.get('n', '?')} 节真正撤销生效。\n"
                f"撤回记录保留作历史，不会再生效。", color="green")
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) != 404:
            log(f"⚠️ _check_pending_revoke 读取异常（非404）: code={getattr(e,'code',None)}")
        # 404=无挂起撤回，静默
    except Exception as e:
        log(f"⚠️ _check_pending_revoke 异常: {e}")


# 内存级审批快照缓存：防止 GitHub 写入失败时下一轮重复通知（同一 SCF 实例生命周期内有效）
# 提前定义：_mark_leave_status 在 _check_leave_approval 之前就需引用
_LEAVE_APPROVAL_CACHE = {}


def _mark_leave_status(odd, status):
    """把申请状态写入 leave_status.json 快照（撤回验证完成时调用），
    避免审批监控把「自己撤回」误报成「审批结果」"""
    # 内存缓存先行（即使 GitHub 写失败也能防误报）
    global _LEAVE_APPROVAL_CACHE
    _LEAVE_APPROVAL_CACHE[str(odd)] = status
    try:
        snap, sha = {}, ""
        try:
            snap, sha = gh_read_json("leave_status.json")
        except Exception as e:
            if getattr(e, "code", None) != 404:
                return
        if not isinstance(snap, dict):
            snap = {}
        apps = snap.get("apps") or {}
        apps[str(odd)] = status
        gh_write_json("leave_status.json", {"apps": apps}, sha, "mark leave status")
    except Exception:
        pass


def _check_leave_approval(chat_id):
    """轮询：监控 AMS 请假审批结果（兑现提交成功卡片里「审批结果会提醒」的承诺）
    快照对比 leave_status.json（odd→status）：status 从 1（待审批）变为其他值 →
    调 detail 拿审批细节（statusName/通过数/审批意见）发提醒卡片。
    新申请入库只记快照不提醒（提交成功卡片已通知过）；
    撤回造成的 1→2 由 _mark_leave_status 同步快照 + revoke_check 排除双重防误报。
    内存缓存 _LEAVE_APPROVAL_CACHE 兜底：GitHub 写入失败时仍能阻止下一轮重复通知。"""
    ams, _, _ = _ams_state()
    token = ams.get("token", "")
    if not token:
        return
    snap, sha = {}, ""
    try:
        snap, sha = gh_read_json("leave_status.json")
        if not isinstance(snap, dict):
            snap = {}
    except Exception as e:
        if getattr(e, "code", None) != 404:
            return  # 网络/限流等异常：下轮再查，不写快照
    known = snap.get("apps") or {}
    # 合并内存缓存（GitHub 写入失败时兜底去重）
    known = {**known, **_LEAVE_APPROVAL_CACHE}
    try:
        resp = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/getLeaveList?pageNum=1&pageSize=20",
                     headers={"x-token": token}, timeout=15)
        if _ams_token_dead(resp) or not isinstance(resp, dict) or resp.get("code") != 0:
            return  # 凭证过期由监控 Actions 定期刷新，静默下轮再查
        rows = (resp.get("data") or {}).get("leaveList") or []
    except Exception:
        return
    # 排除正在撤回验证中的申请（避免把撤回误报为审批结果）
    # 复用 _check_pending_revoke 已读取的结果（同一轮询周期内先于本函数执行）
    pending_odd = _REVOKE_CHECK_ODD_CACHE
    dirty, notices = False, []
    for r in rows:
        odd = str(r.get("leaveOdd") or "")
        st = r.get("status")
        if not odd or st is None:
            continue
        old = known.get(odd)
        if old == st:
            continue
        if old is None:
            known[odd] = st   # 新申请入库：只记快照不提醒
            dirty = True
            continue
        if old == 1 and st != 1 and odd != pending_odd:
            notices.append(r)
        known[odd] = st
        _LEAVE_APPROVAL_CACHE[odd] = st   # 内存缓存先行（GitHub 写失败时兜底）
        dirty = True
    if dirty:
        try:
            gh_write_json("leave_status.json", {"apps": known}, sha, "leave status snapshot")
        except Exception as e:
            log(f"⚠️ leave_status 快照写入失败（内存缓存已更新，下轮兜底）: {e}")
    for r in notices:
        odd = r.get("leaveOdd")
        try:
            det = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/detail/{r.get('leaveId')}",
                        headers={"x-token": token}, timeout=15)
            dd = det.get("data") or {}
        except Exception:
            dd = {}
        sname = r.get("statusName") or dd.get("statusName") or f"status {r.get('status')}"
        bits = []
        pc, uc = dd.get("passCount"), dd.get("unPassCount")
        if pc is not None or uc is not None:
            bits.append(f"通过 {pc if pc is not None else 0} 节 / 未通过 {uc if uc is not None else 0} 节")
        ex = str(dd.get("explainContent") or "").strip()
        if ex:
            bits.append(f"审批意见：{ex}")
        body = (f"申请 {odd}（{r.get('startDate')}~{r.get('endDate')} · {r.get('leaveType')}）\n"
                f"状态变化：待审批 → {sname}")
        if bits:
            body += "\n" + "\n".join(bits)
        body += "\n\n到 ams.xjtlu.edu.cn → Leave 可查看完整记录。"
        feishu_send_action(chat_id, f"📬 请假审批有结果（{sname}）", body, color="blue")


# ============ 图书馆房间预定系统（Information Commons） ============
# 架构：roombookings 前有 aTrust 零信任网关（会话绑定客户端环境/IP），
# 云函数直连 API 会被拦 → 读走监控缓存（秒回），写走 room-ops workflow（浏览器执行，约2-4分钟）
ROOM_MAX_AHEAD_DAYS = 3      # 只能提前 3 天内预定
ROOM_OPEN, ROOM_CLOSE = "09:00", "22:00"


def _room_state():
    """读 butler-data 的 xjtlu_state.json，返回 (room 快照, sha)"""
    try:
        data, sha = gh_read_json("xjtlu_state.json")
        return data.get("room") or {}, sha
    except Exception as e:
        log(f"room state 读取失败: {e}")
        return {}, ""


def _room_avail_text(compact, date_str):
    """把监控缓存的 compact 可用性数据格式化为展示文本"""
    lines = []
    for lab in compact or []:
        lines.append(f"\n【{lab.get('f')}】")
        for r in lab.get("rooms") or []:
            o_s, o_e = r.get("os", ROOM_OPEN), r.get("oe", ROOM_CLOSE)
            booked = sorted(r.get("bk") or [])
            if booked:
                def to_min(t):
                    h, m = t.split(":")
                    return int(h) * 60 + int(m)

                def to_str(v):
                    return f"{v // 60:02d}:{v % 60:02d}"

                cur, free = to_min(o_s), []
                for s, e in booked:
                    s, e = to_min(s), to_min(e)
                    if s > cur:
                        free.append((cur, s))
                    cur = max(cur, e)
                if cur < to_min(o_e):
                    free.append((cur, to_min(o_e)))
                if free:
                    slots = "、".join(f"{to_str(a)}-{to_str(b)}" for a, b in free)
                    lines.append(f"🟢 {r.get('name')} 空闲：{slots}")
                else:
                    lines.append(f"🔴 {r.get('name')}（{o_s}-{o_e}）已约满")
            else:
                lines.append(f"🟢 {r.get('name')}（{o_s}-{o_e}）全天空闲")
    if not lines:
        return None
    head = (f"🏛️ SIP 图书馆研讨室 · {date_str}\n"
            f"⏰ 开放 {ROOM_OPEN}-{ROOM_CLOSE}，每次最长 3 小时，每天限 1 次，最多提前 {ROOM_MAX_AHEAD_DAYS} 天")
    return head + "\n" + "\n".join(lines)


def _room_cache(date_str):
    """从监控缓存取某天可用性 compact 数据；没有返回 None"""
    room, _ = _room_state()
    avail = room.get("avail") or {}
    return avail.get(date_str)


def _room_find_in_cache(date_str, room_key):
    """在缓存里找房间（冲突预警用），返回房间 dict 或 None"""
    compact = _room_cache(date_str)
    if not compact:
        return None
    key = str(room_key).strip().lower().replace("room ", "").replace("room", "")
    for lab in compact:
        for r in lab.get("rooms") or []:
            if key and key in (r.get("name") or "").lower():
                return r
    return None


def _trigger_room_op(op):
    """触发 GitHub Actions room-ops workflow 执行房间操作（浏览器内，过 aTrust）"""
    if not GH_TOKEN:
        log("⚠️ 无 GH_TOKEN，无法触发房间操作")
        return False
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/actions/workflows/room-ops.yml/dispatches",
            data=json.dumps({"ref": "main",
                             "inputs": {"op": json.dumps(op, ensure_ascii=False)}}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {GH_TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        urllib.request.urlopen(req, timeout=12)
        log(f"🚀 已触发房间操作: {json.dumps(op, ensure_ascii=False)[:80]}")
        return True
    except Exception as e:
        log(f"⚠️ 触发房间操作失败: {e}")
        return False


def _room_bookings_text():
    """读缓存生成「我的预约」文本，返回 (文本, 行列表)"""
    room, _ = _room_state()
    rows = room.get("bookings") or []
    if not rows:
        return "📭 目前没有研讨室预约。", []
    lines = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r.get('name')}\n   {r.get('when')} · {r.get('tag')}")
    upd = (room.get("updated") or "")[:16].replace("T", " ")
    return f"📋 我的研讨室预约（数据 {upd} 更新）：\n\n" + "\n".join(lines), rows


def _room_validate(date_str, start, end):
    """订房参数本地校验，返回错误文本或 None"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return f"❌ 日期格式不对：{date_str}"

    def to_min(t):
        try:
            h, m = t.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None
    s_min, e_min = to_min(start), to_min(end)
    if s_min is None or e_min is None:
        return "❌ 时间格式应为 HH:MM，例如 14:00。"
    now = datetime.now()
    if d.date() < now.date():
        return "❌ 不能预定过去的日期。"
    if (d.date() - now.date()).days > ROOM_MAX_AHEAD_DAYS:
        return f"❌ 最多只能提前 {ROOM_MAX_AHEAD_DAYS} 天预定（{date_str} 超出范围）。"
    if e_min <= s_min:
        return "❌ 结束时间要晚于开始时间。"
    if e_min - s_min > 3 * 60:
        return "❌ 每次最长 3 小时。"
    if s_min < 9 * 60 or e_min > 22 * 60:
        return f"❌ 开放时间为 {ROOM_OPEN}-{ROOM_CLOSE}。"
    return None


def _room_parse_date(text, now=None):
    """解析订房日期：今天/明天/后天/周X/10月4号/10-04/2026-10-04，默认今天"""
    now = now or datetime.now()
    date = parse_leave_date(text, now)
    return date or now.strftime("%Y-%m-%d")


def cmd_room_query(chat_id, text):
    """「查房」/「查房 明天」/「查房 明天 5F」/「实时查房 明天」"""
    rest = re.sub(r"^查房(间)?", "", text.strip()).strip()
    live = rest.startswith("实时") or rest.startswith("刷新")
    if live:
        rest = re.sub(r"^(实时|刷新)", "", rest).strip()
    date = _room_parse_date(rest)
    if live:
        if _trigger_room_op({"action": "query", "date": date}):
            feishu_send(chat_id, f"🔍 正在实时查询 {date} 的房间（约 2-4 分钟），结果稍后发你。")
        else:
            feishu_send(chat_id, "❌ 触发实时查询失败，稍后再试。")
        return
    compact = _room_cache(date)
    if compact is None:
        if _trigger_room_op({"action": "query", "date": date}):
            feishu_send(chat_id, f"🔍 {date} 不在缓存里，正在实时查询（约 2-4 分钟）。")
        else:
            feishu_send(chat_id, "❌ 查询触发失败，稍后再试。")
        return
    room, _ = _room_state()
    upd = (room.get("updated") or "")[:16].replace("T", " ")
    body = _room_avail_text(compact, date) or "📭 该日期没有可查询的房间。"
    result = body + f"\n\n（数据 {upd} 更新；发「实时查房 {date}」可刷新）"
    feishu_send_action(chat_id, "🏛️ 研讨室查询", result, color="blue")


def cmd_room_book(chat_id, text):
    """「订房 543 明天 14:00-17:00」"""
    rest = re.sub(r"^订(房|个房|房间|研讨室)", "", text.strip()).strip()
    m = re.search(r"(\d{1,2}:\d{2})\s*[-~到至]\s*(\d{1,2}:\d{2})", rest)
    if not m:
        feishu_send(chat_id, "用法：「订房 房间号 日期 时间段」\n例如「订房 543 明天 14:00-17:00」\n\n先用「查房」看空闲房间。")
        return
    start, end = m.group(1), m.group(2)
    date = _room_parse_date(rest)
    m2 = re.search(r"(?:房间|房|room)\s*[:：]?\s*(\d{2,4})", rest.lower()) or re.search(r"(\d{3,4})(?!\s*[:\-])", rest)
    room_key = m2.group(1) if m2 else ""
    if not room_key:
        feishu_send(chat_id, "没识别到房间号。用法：「订房 543 明天 14:00-17:00」")
        return
    err = _room_validate(date, start, end)
    if err:
        feishu_send_action(chat_id, "🏛️ 研讨室预定", err, color="red")
        return
    # 冲突预警（缓存数据，非权威；真正拦截在 Actions 端）
    r = _room_find_in_cache(date, room_key)
    if r:
        def to_min(t):
            h, mm = t.split(":")
            return int(h) * 60 + int(mm)
        for s, e in r.get("bk") or []:
            if not (to_min(end) <= to_min(s) or to_min(start) >= to_min(e)):
                feishu_send_action(chat_id, "🏛️ 研讨室预定",
                    f"❌ 缓存显示 {r.get('name')} 在 {start}-{end} 已被占用（{s}-{e}）。\n\n"
                    "如认为数据过期，发「实时查房」确认后再订。", color="red")
                return
    mm2 = re.search(r"(?:用途|备注|事项)\s*[:：]?\s*(\S+)", rest)
    memo = mm2.group(1) if mm2 else ""
    # 同伴解析（研讨室要求至少2人）：临时指定 > 已解析accNo > 常用同伴原名
    partner = ""
    years = ["26"]
    pm = re.search(
        r"(?:同伴|同学|partner)\s*[:：]?\s*([\u4e00-\u9fa5]{2,4}|[A-Za-z][A-Za-z0-9.\-]{2,39})",
        rest, re.I) or re.search(r"和\s*([A-Za-z][A-Za-z0-9.\-]{2,39})", rest)
    if pm:
        partner = pm.group(1)
        my = re.search(r"(?:^|\s)(20\d{2}|\d{2})\s*$", rest)
        if my:
            years = [my.group(1)[-2:]]
    room_state, _ = _room_state()
    if not partner:
        if room_state.get("partner_accNo"):
            partner = str(room_state["partner_accNo"])  # 已解析过，直接用 accNo
        else:
            partner = room_state.get("partner") or ""
    if not partner:
        feishu_send_action(chat_id, "🏛️ 研讨室预定",
            "❌ 研讨室要求每次预定至少 2 人。\n\n"
            "请先设置常用同伴：「设同伴 张伟」（直接发中文名即可）\n\n"
            "或订房时带上：「订房 543 明天 14:00-17:00 同伴 张伟」", color="red")
        return
    ok = _trigger_room_op({"action": "book", "date": date, "start": start,
                           "end": end, "room": room_key, "memo": memo,
                           "partner": partner, "years": years})
    if ok:
        feishu_send_action(chat_id, "🏛️ 研讨室预定",
            f"⏳ 收到！正在预定房间 {room_key}（和 {partner} 一起）\n"
            f"📅 {date} {start}-{end}\n\n"
            "浏览器执行中（约 2-4 分钟），结果稍后告诉你。", color="blue")
    else:
        feishu_send_action(chat_id, "🏛️ 研讨室预定", "❌ 触发预定失败（GitHub API 异常），稍后再试。", color="red")


def cmd_room_set_partner(chat_id, text):
    """「设同伴 张伟」/「设同伴 张伟 25」/「设同伴 TOM.SMITH25」设置常用同伴
    「设同伴」查看 /「设同伴 清除」删除。
    研讨室要求至少 2 人；支持中文名（自动转拼音按西浦规则搜索）、账号、拼音名。"""
    rest = re.sub(r"^设(常用)?同伴", "", text.strip()).strip()
    room_state, _ = _room_state()
    cur = room_state.get("partner") or ""
    cur_disp = room_state.get("partner_display") or ""
    if not rest:
        disp = f"{cur}（{cur_disp}）" if cur and cur_disp else (cur or "（未设置）")
        feishu_send_action(chat_id, "👥 常用同伴",
            f"当前常用同伴：{disp}\n\n"
            "设置：「设同伴 张伟」（直接发中文名即可）\n"
            "学长学姐带年份：「设同伴 张伟 24」\n"
            "也支持账号：「设同伴 TOM.SMITH25」\n"
            "清除：「设同伴 清除」\n\n"
            "💡 研讨室要求每次预定至少 2 人；设置后订房会自动带上这位同学。",
            color="blue")
        return
    if rest in ("清除", "删除", "清空", "无", "none", "clear"):
        try:
            data, sha = gh_read_json("xjtlu_state.json")
            rm = data.get("room") or {}
            for k in ("partner", "partner_accNo", "partner_display"):
                rm.pop(k, None)
            gh_write_json("xjtlu_state.json", data, sha, "clear room partner")
        except Exception as e:
            log(f"清除同伴失败: {e}")
            feishu_send_action(chat_id, "👥 常用同伴", "❌ 清除失败，稍后再试。", color="red")
            return
        feishu_send_action(chat_id, "👥 常用同伴", "✅ 已清除常用同伴。", color="green")
        return
    # 解析可选入学年（两位数年份，如 24/25/26；也接受 2024/2025/2026）
    years = ["26"]
    m = re.search(r"(?:^|\s)(20\d{2}|\d{2})\s*$", rest)
    if m:
        years = [m.group(1)[-2:]]
        rest = re.sub(r"(?:^|\s)(?:20)?\d{2}\s*$", "", rest).strip()
    if not rest:
        feishu_send_action(chat_id, "👥 常用同伴", "❌ 没识别到名字，例如「设同伴 张伟」。", color="red")
        return
    if len(rest) > 30:
        feishu_send_action(chat_id, "👥 常用同伴", "❌ 名字太长了，请检查输入。", color="red")
        return
    # 防把自己设为同伴
    if room_state.get("name") and rest.upper() == str(room_state["name"]).upper():
        feishu_send_action(chat_id, "👥 常用同伴",
            "❌ 不能把自己设为同伴（预定人就是你本人）。", color="red")
        return
    # 先存原始名字（LLM/订房时可见），再触发解析（异步，约2-4分钟回掩码确认）
    try:
        data, sha = gh_read_json("xjtlu_state.json")
        rm = data.setdefault("room", {})
        rm["partner"] = rest
        for k in ("partner_accNo", "partner_display"):
            rm.pop(k, None)
        gh_write_json("xjtlu_state.json", data, sha, f"set room partner={rest} (resolving)")
    except Exception as e:
        log(f"设同伴暂存失败: {e}")
        feishu_send_action(chat_id, "👥 常用同伴", "❌ 保存失败，稍后再试。", color="red")
        return
    ok = _trigger_room_op({"action": "partner", "name": rest, "years": years})
    if ok:
        yr = f"（入学年 20{years[0]}）" if years != ["26"] else "（默认按 2026 级找）"
        feishu_send_action(chat_id, "👥 常用同伴",
            f"🔍 收到！正在查找同学「{rest}」{yr}\n\n"
            "系统会把名字转成拼音按西浦账号规则搜索（约 2-4 分钟），找到后会告诉你结果。",
            color="blue")
    else:
        feishu_send_action(chat_id, "👥 常用同伴",
            f"✅ 「{rest}」已暂存为常用同伴，但自动查找触发失败。"
            "订房时系统仍会现场解析。", color="orange")


def cmd_room_list(chat_id):
    text, _ = _room_bookings_text()
    feishu_send_action(chat_id, "📋 我的预约", text, color="blue")


def cmd_room_cancel(chat_id, text):
    """「取消预定 N」→ 缓存找 uuid → 触发 Actions 删除"""
    rest = re.sub(r"^取消(预定|预约|订房)", "", text.strip()).strip()
    txt, rows = _room_bookings_text()
    if not rows:
        feishu_send_action(chat_id, "🗑️ 取消预约", txt, color="blue")
        return
    try:
        idx = int(rest) if rest else 1
    except ValueError:
        feishu_send_action(chat_id, "🗑️ 取消预约", "❌ 请指定序号，例如「取消预定 1」。", color="red")
        return
    if idx < 1 or idx > len(rows):
        feishu_send_action(chat_id, "🗑️ 取消预约",
            f"❌ 序号超出范围（1~{len(rows)}）。先发「我的预约」看列表。", color="red")
        return
    target = rows[idx - 1]
    uuid = target.get("uuid")
    if not uuid:
        feishu_send_action(chat_id, "🗑️ 取消预约", "❌ 该预约缺少标识，无法取消。", color="red")
        return
    ok = _trigger_room_op({"action": "cancel", "uuid": uuid, "name": target.get("name", "")})
    if ok:
        feishu_send_action(chat_id, "🗑️ 取消预约",
            f"⏳ 正在取消：{target.get('name')} {target.get('when')}\n\n浏览器执行中（约 2-4 分钟），结果稍后告诉你。",
            color="blue")
    else:
        feishu_send_action(chat_id, "🗑️ 取消预约", "❌ 触发取消失败，稍后再试。", color="red")


def cmd_del_calendar(chat_id, text):
    """删除 Outlook 日历事件
    「删日历」→ 列出
    「删日历 N」→ 删第N个
    「删日历全部 📚」→ 删所有📚开头的
    「删日历 10月9号的数学」→ 按日期+关键词过滤，匹配的列出供选择或全删
    「删日历 10月9号的数学 全部」→ 匹配的全删"""
    try:
        events = outlook_events()
    except Exception as e:
        feishu_send(chat_id, f"❌ 读取日历失败：{e}")
        return
    if not events:
        feishu_send(chat_id, "📭 Outlook 日历上没有日程。")
        return
    events.sort(key=lambda e: e.get("Start", {}).get("DateTime", ""))

    # 解析日期过滤
    date_filter = None
    dm = re.search(r"(\d{1,2})[月./\-](\d{1,2})", text)
    if dm:
        try:
            date_filter = datetime(datetime.now().year, int(dm.group(1)), int(dm.group(2))).strftime("%m-%d")
        except ValueError:
            pass

    # 解析课程/关键词过滤
    kw_filter = None
    for cn, kws in [("数学", ["MTH026", "MTH028", "微积分", "线代", "线性代数", "数学"]),
                    ("微积分", ["MTH026", "微积分"]),
                    ("线代", ["MTH028", "线代", "线性代数"]),
                    ("英语", ["EAP043", "英语"]),
                    ("体育", ["PHE001", "体育"]),
                    ("科学", ["SCI004", "科学"]),
                    ("心理", ["CCT007", "心理"]),
                    ("毛概", ["CCT011", "毛概"]),
                    ("马原", ["CCT001", "马原"])]:
        if cn in text:
            kw_filter = kws
            break
    # 也支持直接写课程码
    cm = re.search(r"\b([A-Z]{3}\d{3})\b", text.upper())
    if cm:
        kw_filter = [cm.group(1)]

    # 有日期或关键词过滤时：先筛选再决定列出还是删除
    if date_filter or kw_filter:
        matched = []
        for e in events:
            subj = (e.get("Subject") or "")
            dt = (e.get("Start", {}).get("DateTime", "") or "")
            dt_short = dt[5:10] if len(dt) >= 10 else ""  # MM-DD
            if date_filter and dt_short != date_filter:
                continue
            if kw_filter and not any(k.lower() in subj.lower() for k in kw_filter):
                continue
            matched.append(e)
        if not matched:
            feishu_send(chat_id, "📭 没找到匹配的日历事件。")
            return
        # 「全部」→ 全删
        if "全部" in text or "所有" in text or len(matched) == 1:
            deleted = 0
            for e in matched:
                try:
                    outlook_delete(e.get("Id", ""))
                    deleted += 1
                except Exception:
                    pass
            names = "、".join((e.get("Subject") or "")[:30] for e in matched[:3])
            feishu_send_action(chat_id, "✅ 已删除",
                f"删除了 {deleted} 个日历事件：\n{names}", color="green")
            return
        # 否则列出匹配的供选择
        lines = [f"找到 {len(matched)} 个匹配的日历事件："]
        for i, e in enumerate(matched, 1):
            subj = (e.get("Subject") or "(无标题)")[:45]
            t = (e.get("Start", {}).get("DateTime", "") or "")
            t_str = f"{t[5:10]} {t[11:16]}" if len(t) >= 16 else "时间待定"
            lines.append(f"{i}. {subj} · {t_str}")
        lines.append(f"\n🗑️「删日历 N」删第N个　🗑️「删日历 {text.strip()} 全部」全删")
        feishu_send(chat_id, "\n".join(lines))
        return

    # 无过滤：按编号删或列出
    num = 0
    m = re.search(r"(\d+)", text)
    if m:
        num = int(m.group(1))
    if num == 0:
        lines = [f"📅 Outlook 日历日程（共 {len(events)} 个）："]
        for i, e in enumerate(events, 1):
            subj = (e.get("Subject") or "(无标题)")[:45]
            t = (e.get("Start", {}).get("DateTime", "") or "")
            t_str = f"{t[5:10]} {t[11:16]}" if len(t) >= 16 else "时间待定"
            lines.append(f"{i}. {subj} · {t_str}")
        lines.append("\n🗑️「删日历 N」删第N个　🗑️「删日历全部 📚」删所有📚开头的")
        lines.append("💡 也可「删日历 10月9号的数学」按条件筛")
        feishu_send(chat_id, "\n".join(lines))
        return
    if num < 1 or num > len(events):
        feishu_send(chat_id, f"⚠️ 编号超出范围（1-{len(events)}）")
        return
    target = events[num - 1]
    eid = target.get("Id", "")
    subj = (target.get("Subject") or "")[:45]
    try:
        outlook_delete(eid)
        feishu_send_action(chat_id, "✅ 已删除", f"已从 Outlook 日历删除：\n{subj}", color="green")
    except Exception as e:
        feishu_send(chat_id, f"❌ 删除失败：{e}")


def cmd_del_calendar_all_hw(chat_id):
    """删除所有管家加的作业日历事件（📚 开头）"""
    try:
        events = outlook_events()
    except Exception as e:
        feishu_send(chat_id, f"❌ 读取日历失败：{e}")
        return
    hw_events = [e for e in events if (e.get("Subject") or "").startswith("📚")]
    if not hw_events:
        feishu_send(chat_id, "📭 没有找到📚开头的作业日历事件。")
        return
    deleted = 0
    for e in hw_events:
        try:
            outlook_delete(e.get("Id", ""))
            deleted += 1
        except Exception:
            pass
    feishu_send_action(chat_id, "✅ 批量删除完成",
        f"已删除 {deleted} 个作业日历事件。\n\n现在可以重新发「把XX作业加到日历」，不会再重复了。",
        color="green")


def cmd_homework_to_calendar(chat_id, text):
    """把 LM 作业写进 Outlook 日历（用户说"把XX作业加到日历"时触发）
    支持：课程名/课程码过滤 + 日期过滤"""
    events = fetch_lm_assignments()
    if not events:
        feishu_send(chat_id, "📭 暂无 LearningMall 作业数据。")
        return
    # 解析过滤条件
    t = text.upper()
    # 课程码
    course_filter = None
    m = re.search(r"\b([A-Z]{3}\d{3})\b", t)
    if m:
        course_filter = m.group(1)
    else:
        for cn, code in [("微积分", "MTH026"), ("线代", "MTH028"), ("线性代数", "MTH028"),
                         ("学术英语", "EAP043"), ("英语", "EAP043"), ("体育", "PHE001"),
                         ("科学", "SCI004"), ("新兴技术", "PSP004"), ("马原", "CCT001"),
                         ("毛概", "CCT011"), ("形势", "CCT012"), ("心理", "CCT007"),
                         ("数学", None)]:
            if cn in text:
                course_filter = code if code else "MATH"
                break
    # 日期过滤：10月9号 / 10-09 / 10.9
    date_filter = None
    dm = re.search(r"(\d{1,2})[月./\-](\d{1,2})", text)
    if dm:
        try:
            date_filter = datetime(datetime.now().year, int(dm.group(1)), int(dm.group(2))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # 筛选
    matched = []
    for ev in events:
        if date_filter and ev["dt"].strftime("%Y-%m-%d") != date_filter:
            continue
        if course_filter == "MATH":
            if not any(c in ev["summary"] for c in ["MTH026", "MTH028", "微积分", "线代", "线性代数"]):
                continue
        elif course_filter:
            if course_filter not in ev["summary"]:
                continue
        matched.append(ev)
    if not matched:
        feishu_send(chat_id, "📭 没找到匹配的作业。试试「把10月9号的数学作业加到日历」。")
        return
    # 创建 Outlook 日历事件（防重复：先查是否已有同名事件）
    created = 0
    failed = 0
    skipped = 0
    fail_reason = ""
    # 查已有日历事件，按 subject 去重
    existing_subjects = set()
    try:
        upcoming = outlook_events()
        for e in upcoming:
            existing_subjects.add((e.get("Subject") or "")[:40])
    except Exception:
        pass
    for ev in matched:
        try:
            title = f"📚 {ev['summary'][:60]}"
            if title[:40] in existing_subjects:
                skipped += 1
                continue
            start = ev["dt"]
            end = start + timedelta(hours=1)
            body = f"LearningMall 作业\n{ev.get('summary','')}\n\n截止时间：{ev['dt'].strftime('%Y年%m月%d日 %H:%M')}\n{ev.get('countdown','')}"
            eid = outlook_create_event(title, start, end, body=body)
            if eid:
                created += 1
            else:
                failed += 1
                fail_reason = "API 返回空"
        except Exception as e:
            log(f"⚠️ 作业写日历失败 {ev['summary'][:30]}: {e}")
            failed += 1
            fail_reason = str(e)[:80]
    parts = [f"📚 已将 {created} 项作业写入 Outlook 日历"]
    if skipped:
        parts.append(f"（{skipped} 项已存在跳过）")
    if failed:
        parts.append(f"（{failed} 项失败{': '+fail_reason if fail_reason else ''}）")
    summary_lines = ["".join(parts)]
    for ev in matched[:5]:
        summary_lines.append(f"· {ev['summary'][:50]} — {ev['dt'].strftime('%m月%d日 %H:%M')}")
    summary_lines.append("\n打开 iPhone 日历 app 即可看到（确保 Outlook 日历分组已勾选）。")
    feishu_send(chat_id, "\n".join(summary_lines))


def cmd_add_calendar(chat_id, text):
    """自由格式加日历：用户说「加日历 <标题> <日期> <开始时间> <结束时间>」
    例如：加日历 微积分 Practice quiz 1 - W4 截止 10-03 22:00 23:00
    智能解析标题/日期/时段 → 创建 Outlook 日历事件"""
    # 去掉前缀
    rest = re.sub(r"^(加日历|添加日历|写入日历|标到日历|加到日历)\s*", "", text.strip())
    # 提取时间：HH:MM（找所有 HH:MM 模式，时间无歧义先提取）
    times = re.findall(r"(\d{1,2}[:：]\d{2})", rest)
    if len(times) < 2:
        feishu_send(chat_id, "❓ 需要开始和结束时间。格式：「加日历 <标题> 10-03 22:00 23:00」")
        return
    start_t = times[0].replace("：", ":")
    end_t = times[1].replace("：", ":")
    # 提取日期：优先 YYYY-MM-DD，再试 MM-DD / MM月DD日 / 今天/明天/后天
    date_str = None
    date_match_text = None  # 记录匹配到的原文，用于后面精确清理标题
    m_full = re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", rest)
    if m_full:
        try:
            date_str = datetime(int(m_full.group(1)), int(m_full.group(2)),
                                int(m_full.group(3))).strftime("%Y-%m-%d")
            date_match_text = m_full.group(0)
        except ValueError:
            pass
    if not date_str:
        # 取最后一个 MM-DD 匹配（日期在标题之后、时间之前，取最后避免标题中的数字-数字被误匹配）
        m_all = list(re.finditer(r"(?<!\d)(\d{1,2})[月./\-](\d{1,2})(?!\d)", rest))
        if m_all:
            m_short = m_all[-1]
            try:
                date_str = datetime(datetime.now().year, int(m_short.group(1)),
                                    int(m_short.group(2))).strftime("%Y-%m-%d")
                date_match_text = m_short.group(0)
            except ValueError:
                pass
    if not date_str:
        for kw, offset in [("今天", 0), ("明天", 1), ("后天", 2)]:
            if kw in rest:
                date_str = (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")
                date_match_text = kw
                break
    if not date_str:
        feishu_send(chat_id, "❓ 没看懂日期。格式：「加日历 <标题> 10-03 22:00 23:00」\n"
                             "或「加日历 <标题> 今天 19:00 20:00」")
        return
    # 提取标题：只删已识别的日期/时间原文，不用全局 sub（避免误删标题里的数字-数字）
    title_raw = rest
    if date_match_text:
        title_raw = title_raw.replace(date_match_text, "")
    for tm in times[:2]:
        title_raw = title_raw.replace(tm, "", 1)
    title_raw = re.sub(r"(今天|明天|后天|晚上|下午|上午|早上)", "", title_raw)
    title_raw = title_raw.strip(" ，,。\n。-")
    if not title_raw:
        feishu_send(chat_id, "❓ 没有标题。格式：「加日历 微积分测验 10-03 22:00 23:00」")
        return
    # 组合 datetime
    try:
        start_dt = datetime.strptime(f"{date_str} {start_t}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{date_str} {end_t}", "%Y-%m-%d %H:%M")
    except ValueError:
        feishu_send(chat_id, f"❌ 时间解析失败：{date_str} {start_t}~{end_t}")
        return
    if end_dt <= start_dt:
        feishu_send(chat_id, "❌ 结束时间不能早于开始时间。")
        return
    # 创建 Outlook 事件
    try:
        eid = outlook_create_event(title_raw[:120], start_dt, end_dt,
                                    body=f"管家添加：{title_raw}")
    except Exception as e:
        feishu_send(chat_id, f"❌ 写入 Outlook 日历失败：{e}")
        return
    if eid:
        wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][start_dt.weekday()]
        feishu_send_action(chat_id, "✅ 已加入日历",
            f"📅 {title_raw[:60]}\n"
            f"{start_dt.strftime('%m月%d日')}（{wd}）{start_t}~{end_t}\n\n"
            "已写入 Outlook 日历，iPhone 日历会自动同步显示。", color="green")
    else:
        feishu_send(chat_id, "❌ Outlook 日历写入失败（API 返回空），请稍后重试。")


def process_command(text, chat_id):
    t = text.lower().strip()
    # 请假流程（优先级高，避免「请假」被其他规则吞掉）
    if text.strip().startswith("请假") or t.startswith("请个假") or t.startswith("申请请假"):
        cmd_leave(chat_id, text)
        return
    # 自由格式加日历：「加日历 <标题> <日期> <开始> <结束>」
    if t.startswith(("加日历", "添加日历", "写入日历", "标到日历", "加到日历")):
        cmd_add_calendar(chat_id, text)
        return
    # 作业写日历：用户说"把XX作业加到日历"/"添加到日历"+作业/课程关键词
    if "日历" in text and any(kw in text for kw in ["作业", "assignment", "截止", "ddl", "MTH", "EAP", "SCI", "CCT", "PHE", "PSP", "微积分", "线代", "英语", "数学", "科学", "体育"]):
        # H3 修复：去掉 or "日历" in text（恒True），只凭动作关键词触发
        if any(kw in text for kw in ["加", "添加", "写", "标", "放", "同步", "导入"]):
            cmd_homework_to_calendar(chat_id, text)
            return
    if t in ("取消请假", "取消申请", "放弃请假"):
        cmd_leave_cancel(chat_id)
        return
    # 撤回请假：放宽匹配——"撤回/撤销"开头 + 含请假语义或日期均可
    # （覆盖自然表达：「撤回 10月11号的请假」「撤回10-11」「撤销昨天请的假」等，避免落到 AI 聊天被假执行）
    if t.startswith(("撤回请假", "撤销请假", "撤回申请", "撤销申请")):
        cmd_leave_revoke(chat_id, text)
        return
    if t.startswith(("撤回", "撤销")) and any(k in t for k in ["假", "申请"]) or \
       (t.startswith(("撤回", "撤销")) and re.search(r"\d{1,2}[-月.]\d{1,2}", t)):
        cmd_leave_revoke(chat_id, text)
        return
    # 删日历：列出/删第N个/删所有作业
    if t.startswith("删日历") or t.startswith("删除日历"):
        rest = text[3:] if text.startswith("删日历") else text[4:]
        if "全部" in rest and "📚" in rest:
            cmd_del_calendar_all_hw(chat_id)
        else:
            cmd_del_calendar(chat_id, rest)
        return
    if t.startswith("改假条") or t.startswith("改原因"):
        # 重新生成假条原因
        pending, _ = _load_leave_pending()
        if not pending.get("status"):
            feishu_send(chat_id, "📭 当前没有进行中的请假申请，先用「请假 日期 原因」发起。")
            return
        new_reason = re.sub(r"^(改假条|改原因)", "", text.strip()).strip()
        if not new_reason:
            feishu_send(chat_id, "用法：「改假条 <新原因>」，例如「改假条 崴脚了，医生建议卧床一周」")
            return
        lt = pending.get("leaveType", _guess_leave_type(new_reason))
        pending["leaveReason"] = _format_leave_reason(new_reason, lt)
        pending["leaveType"] = lt
        _save_leave_pending(pending)
        feishu_send(chat_id, f"✏️ 假条已重写（类型：{lt}）：\n\n{pending['leaveReason']}\n\n"
                             "发证明照片即可提交；「取消请假」放弃。")
        return
    # 图书馆房间预定（固定命令；自然语言走 LLM Function Calling）
    if t.startswith("查房") or t.startswith("房间查询"):
        cmd_room_query(chat_id, text)
        return
    if t.startswith("订房") or t.startswith("订个房") or t.startswith("订研讨室"):
        cmd_room_book(chat_id, text)
        return
    if t in ("我的预定", "我的预约", "预定列表", "预约列表", "我的房间"):
        cmd_room_list(chat_id)
        return
    if t.startswith("取消预定") or t.startswith("取消预约") or t.startswith("取消订房"):
        cmd_room_cancel(chat_id, text)
        return
    if t.startswith("设同伴") or t.startswith("设置同伴") or t.startswith("常用同伴"):
        cmd_room_set_partner(chat_id, text)
        return
    # 智能签到：纯数字码 / 扫码URL（上课时间自动识别，误触保护：无课时纯数字走聊天）
    if ams_try_auto_sign(t, chat_id):
        return
    # 签到：精确匹配「签到 <数字码>」，避免误触聊天
    m = re.match(r"^(?:签到|checkin|check in)[\s:：]*(\d{3,8})\s*$", t)
    if m:
        cmd_checkin(chat_id, m.group(1))
        return
    if t in ("出勤", "出勤率", "考勤", "attendance"):
        cmd_attendance(chat_id)
        return
    # 批量操作优先匹配
    if "全部" in t or "all" in t:
        if "批准" in t or "approve" in t or "加入" in t or "确认" in t:
            cmd_approve(chat_id, t + " 全部")
            return
        if "跳过" in t or "skip" in t or "忽略" in t or "消除" in t or "清除" in t or "清掉" in t or "不要" in t:
            cmd_skip(chat_id, t + " 全部")
            return
        if "删除" in t or "delete" in t or "去掉" in t or "移除" in t:
            # H2 修复："取消"不再触删除日历（"取消请假"已提前处理）
            cmd_smart_delete(chat_id, "全部")
            return

    if t in ("帮助", "help", "?", "？"):
        cmd_help(chat_id)
    elif t in ("列表", "list", "pending", "待确认"):
        cmd_list(chat_id)
    elif t in ("日历", "calendar", "已批准"):
        cmd_calendar(chat_id)
    elif t.startswith("批准") or t.startswith("approve"):
        cmd_approve(chat_id, t)
    elif t.startswith("跳过") or t.startswith("skip"):
        cmd_skip(chat_id, t)
    elif t in ("日程", "全部日程", "outlook", "events"):
        cmd_schedule(chat_id)
    elif t in ("作业", "homework", "hw", "ddl", "deadline", "learningmall", "lm", "学习"):
        cmd_homework(chat_id)
    elif t.startswith("删除") or t.startswith("delete") or t.startswith("revoke") or t.startswith("去掉") or t.startswith("移除"):
        # H2 修复：移除"取消"前缀路由到删除，避免"取消N"误删日历
        rest = t
        for prefix in ("删除", "delete", "revoke", "去掉", "移除"):
            if rest.startswith(prefix):
                rest = rest[len(prefix):].strip()
                break
        cmd_smart_delete(chat_id, rest)
    else:
        # 慢速路径：未匹配固定命令 → 大模型理解意图并直接执行
        reply, action = call_llm_chat(text, chat_id)
        if action:
            # 大模型识别出操作意图 → 先回复再执行
            if reply:
                feishu_send(chat_id, reply)
            execute_llm_action(action, chat_id)
        elif reply:
            feishu_send(chat_id, reply)
        else:
            # 无API Key时回退到提示
            feishu_send(chat_id,
                        f"收到消息：「{text[:20]}」\n\n"
                        f"我可以帮你：\n"
            f"• 回复「列表」查看待确认活动\n"
            f"• 回复「作业」查看近期作业截止时间\n"
            f"• 回复「日历」管理日程\n"
            f"• 回复「帮助」查看完整指令")


# ============ LearningMall 作业 ============
LM_COURSE_MAP = {
    "MTH026": "数学026·微积分", "MTH028": "数学028·线性代数",
    "SCI004": "科学004", "EAP043": "学术英语EAP043", "EAP029": "学术英语EAP029",
    "CCT007": "CCT007", "CSE105": "计算机105", "CSE106": "计算机106",
    "PHY001": "物理001", "CHE001": "化学001",
}

def _lm_course_label(categories, summary):
    """从 CATEGORIES 或 SUMMARY 提取课程代码，返回中文标签"""
    text = f"{categories} {summary}"
    for code, cn in LM_COURSE_MAP.items():
        if code in text:
            return cn
    # 尝试通用匹配（MTH/SCI/EAP/CCT 开头的 6 位代码）
    m = re.search(r'\b([A-Z]{3}\d{3})\b', text)
    if m:
        return m.group(1)
    return ""

def fetch_lm_assignments(days_ahead=14):
    """从 LearningMall ICS 日历拉取未来作业/考试截止事件"""
    if not LM_CAL_URL:
        return []
    try:
        req = urllib.request.Request(LM_CAL_URL, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        # 解析 ICS
        events = []
        for block in raw.split("BEGIN:VEVENT"):
            if "END:VEVENT" not in block:
                continue
            block = block.split("END:VEVENT")[0]
            summary = ""
            dtstart = ""
            categories = ""
            desc = ""
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("SUMMARY:"):
                    summary = line[8:].strip()
                elif line.startswith("DTSTART"):
                    dtstart = line.split(":", 1)[-1].strip() if ":" in line else ""
                elif line.startswith("CATEGORIES:"):
                    categories = line[11:].strip()
                elif line.startswith("DESCRIPTION:"):
                    desc = line[12:].strip()
            if not summary or not dtstart:
                continue
            # 解析时间（ICS 时间是 UTC，需转换为北京时间 UTC+8）
            is_utc = dtstart.endswith("Z")
            dt = None
            for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M%SZ", "%Y%m%d"):
                try:
                    dt = datetime.strptime(dtstart[:15], fmt)
                    break
                except ValueError:
                    continue
            if not dt:
                continue
            if is_utc:
                dt = dt + timedelta(hours=8)  # UTC → 北京时间
            # 只保留未来 days_ahead 天内的事件
            now = datetime.now()
            if dt < now - timedelta(hours=1):
                continue
            if dt > now + timedelta(days=days_ahead):
                continue
            # 课程标签：从CATEGORIES提取课程代码并映射中文（作业名保留英文原文）
            course_label = _lm_course_label(categories, summary)
            # 提取动作词（opens/closes/is due）从标题中移除，放到时间行
            action = ""
            title = summary
            m_act = re.search(r'\s+(opens|closes|is due)\s*$', title, re.IGNORECASE)
            if m_act:
                action = {"opens": "开放", "closes": "截止", "is due": "截止"}[m_act.group(1).lower()]
                title = title[:m_act.start()].strip()
            # 倒计时：按日历日差算，不能用 timedelta.days——
            # 10-03 21点→10-05 08:59 相差 1.5 天会被截断成 1 报成「明天」，实际是「后天」
            day_gap = (dt.date() - now.date()).days
            if day_gap == 0:
                countdown = f"今天 {dt.strftime('%H:%M')}"
            elif day_gap == 1:
                countdown = f"明天 {dt.strftime('%H:%M')}"
            elif day_gap == 2:
                countdown = f"后天 {dt.strftime('%H:%M')}"
            else:
                countdown = f"{day_gap}天后"
            # 拼接标题：【课程名】+ 英文作业名（不含动作词）
            full_title = f"【{course_label}】{title}" if course_label else title
            events.append({
                "summary": full_title,
                "action": action,
                "dt": dt,
                "countdown": countdown,
                "categories": categories,
            })
        # 合并同一作业的 opens/closes 事件：防止「开放时间」被误读成「截止时间」
        # （2026-10-03 实测事故：Practice quiz opens 10-05 08:59 / closes 10-09 20:30，
        #  两个事件分开注入时 AI 把开始时间说成了「明天截止」）
        opens_ev = [e for e in events if e["action"] == "开放"]
        close_ev = [e for e in events if e["action"] == "截止"]
        plain_ev = [e for e in events if e["action"] not in ("开放", "截止")]
        merged = []
        matched_close = set()
        for o in opens_ev:
            key = (o["summary"], o["categories"])
            for c in close_ev:
                if id(c) in matched_close:
                    continue
                if (c["summary"], c["categories"]) == key and c["dt"] >= o["dt"]:
                    o = dict(o, close_dt=c["dt"], close_countdown=c["countdown"])
                    matched_close.add(id(c))
                    break
            merged.append(o)
        merged.extend(c for c in close_ev if id(c) not in matched_close)
        merged.extend(plain_ev)
        merged.sort(key=lambda e: e["dt"])
        return merged
    except Exception as e:
        log(f"❌ LM作业拉取失败: {e}")
        return []


def format_homework(events):
    """格式化作业列表供飞书消息展示（纯文本版，卡片失败时备用）"""
    if not events:
        return "📚 当前没有即将到期的作业，可以轻松一下～"
    lines = [f"📚 近期作业/测验（共{len(events)}项）："]
    for i, ev in enumerate(events, 1):
        lines.append(f"{i}. {ev['summary'][:50]}")
        t_str = ev['dt'].strftime('%m月%d日 %H:%M')
        if ev.get("close_dt"):
            # 合并了开放+截止的 quiz 类：明确展示两个时间点
            lines.append(f"　　🟢 {t_str} 开放（{ev['countdown']}）")
            lines.append(f"　　🔴 {ev['close_dt'].strftime('%m月%d日 %H:%M')} 截止（{ev['close_countdown']}）")
        elif ev.get("action") == "截止":
            lines.append(f"　　🔴 {t_str} 截止 · {ev['countdown']}")
        elif ev.get("action") == "开放":
            lines.append(f"　　🟢 {t_str} 开放 · {ev['countdown']}")
        else:
            lines.append(f"　　⏰ {t_str} · {ev['countdown']}")
    return "\n".join(lines)


def _get_eap_progress():
    """读取 xjtlu_monitor 存的 EAP043 作业进度"""
    try:
        state, _ = gh_read_json("xjtlu_state.json")
        eap = state.get("eap_progress") or {}
        if not eap.get("total"):
            return None
        return eap
    except Exception:
        return None


def format_homework_card(events):
    """生成飞书卡片：按课程分组的作业列表（彩色标题+表格布局）"""
    if not events:
        return None
    # 按课程分组（保持时间序）
    courses = {}
    order = []
    for ev in events:
        m = re.match(r'【(.+?)】(.+)', ev["summary"])
        if m:
            course, name = m.group(1), m.group(2)
        else:
            course, name = "其他", ev["summary"]
        if course not in courses:
            courses[course] = []
            order.append(course)
        courses[course].append({
            "name": name,
            "time": ev["dt"].strftime('%m月%d日 %H:%M'),
            "action": ev.get("action", ""),
            "countdown": ev["countdown"],
            "close_time": ev["close_dt"].strftime('%m月%d日 %H:%M') if ev.get("close_dt") else None,
            "close_countdown": ev.get("close_countdown", ""),
        })
    elements = []
    for idx, course in enumerate(order):
        items = courses[course]
        if idx > 0:
            elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**📘 {course}**（{len(items)}项）"}
        })
        for it in items:
            # 状态图标：截止🔴 开放🟢；合并事件（开放+截止）双行展示
            if it.get("close_time"):
                status = f"🟢 {it['countdown']} 开放\n🔴 {it['close_countdown']} 截止"
            elif it["action"] == "截止":
                status = f"🔴 截止 · {it['countdown']}"
            elif it["action"] == "开放":
                status = f"🟢 开放 · {it['countdown']}"
            else:
                status = it["countdown"]
            elements.append({
                "tag": "column_set",
                "flex_mode": "bisect",
                "background_style": "grey",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "elements": [{"tag": "div",
                                   "text": {"tag": "lark_md", "content": it["name"][:40]}}]},
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "elements": [{"tag": "div",
                                   "text": {"tag": "lark_md",
                                            "content": f"{it['time']}\n{status}"}}]}
                ]
            })
    # EAP 进度区块（有数据才显示）
    eap = _get_eap_progress()
    if eap and eap.get("total"):
        total = eap.get("total", 0)
        done = eap.get("completed", 0)
        locked = eap.get("locked", 0)
        # 进度条：▓▓▓░░░░░ X/N
        filled = round(done / total * 8) if total else 0
        bar = "▓" * filled + "░" * (8 - filled)
        eap_line = f"**✍️ 学术英语EAP043 作业进度**\n{bar}  {done}/{total}\n已完成 {done} 项"
        if locked:
            eap_line += f" · 未解锁 {locked} 项"
        # 当前进行中的作业（未锁定的第一个）
        current = next((it["name"] for it in eap.get("items", [])
                        if not it.get("locked") and not it.get("completed")), "")
        if current:
            eap_line += f"\n当前任务：{current}"
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": eap_line}
        })
    elements.append({"tag": "hr"})
    elements.append({"tag": "note", "elements": [
        {"tag": "plain_text", "content": "📅 数据来自 LearningMall 日历订阅"}
    ]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"📚 近期作业/测验（共{len(events)}项）"}
        },
        "elements": elements
    }


def send_homework_card(chat_id, events):
    """发送作业列表卡片消息，失败时回退纯文本"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return
    card = format_homework_card(events)
    if not card:
        feishu_send(chat_id, "📚 当前没有即将到期的作业，可以轻松一下～")
        return
    try:
        token = get_feishu_token()
        _http(f"{FEISHU_BASE}/open-apis/im/v1/messages?receive_id_type=chat_id",
              method="POST",
              headers={"Authorization": f"Bearer {token}"},
              data={"receive_id": chat_id, "msg_type": "interactive",
                    "content": json.dumps(card)})
        log(f"✅ 作业卡片已发送（{len(events)}项）")
    except Exception as e:
        log(f"❌ 作业卡片发送失败: {e}，回退纯文本")
        feishu_send(chat_id, format_homework(events))


# ============ 大模型对话 ============
def gather_context(include_outlook=True):
    """收集当前上下文供大模型参考。include_outlook=False时跳过Outlook API（加速闲聊）"""
    ctx_parts = []
    try:
        pending, _ = gh_read_json("pending.json")
        active = [i for i in pending.get("items", []) if i.get("status") == "pending"]
        if active:
            lines = [f"- {i['subject'][:50]} ({i.get('date_txt', '时间待定')})" for i in active]
            ctx_parts.append("待确认活动（用户尚未批准）：\n" + "\n".join(lines))
    except Exception:
        pass
    try:
        confirmed, _ = gh_read_json("confirmed.json")
        items = confirmed.get("items", [])
        if items:
            lines = [f"- {i['subject'][:50]} ({i.get('date_txt', '时间待定')})" for i in items]
            ctx_parts.append("管家日历中的活动：\n" + "\n".join(lines))
    except Exception:
        pass
    if include_outlook:
        try:
            events = outlook_events()
            today = datetime.now().strftime("%Y-%m-%d")
            today_events = [e for e in events if (e.get("Start", {}).get("DateTime", "") or "")[:10] == today]
            if today_events:
                lines = []
                for ev in today_events:
                    subj = ev.get("Subject", "") or "(无标题)"
                    t = (ev.get("Start", {}).get("DateTime", "") or "")[11:16]
                    lines.append(f"- {t} {subj[:40]}" if t else f"- {subj[:40]}")
                ctx_parts.append(f"今天的Outlook日程（共{len(today_events)}个）：\n" + "\n".join(lines))
        except Exception:
            pass
    # 注入 LM 作业上下文（带完整时间语义：开放=开始时间，截止=提交期限）
    try:
        lm_events = fetch_lm_assignments()
        if lm_events:
            lines = []
            for ev in lm_events[:10]:
                base = f"- {ev['summary'][:45]}"
                if ev.get("close_dt"):
                    lines.append(f"{base}：{ev['dt'].strftime('%m月%d日 %H:%M')} 开放（{ev['countdown']}）→ {ev['close_dt'].strftime('%m月%d日 %H:%M')} 截止（{ev['close_countdown']}）")
                elif ev.get("action") == "截止":
                    lines.append(f"{base}：{ev['dt'].strftime('%m月%d日 %H:%M')} 截止（{ev['countdown']}）")
                elif ev.get("action") == "开放":
                    lines.append(f"{base}：{ev['dt'].strftime('%m月%d日 %H:%M')} 开放（{ev['countdown']}）")
                else:
                    lines.append(f"{base}：{ev['dt'].strftime('%m月%d日 %H:%M')}（{ev['countdown']}）")
            ctx_parts.append(f"LearningMall 近期作业/测验（共{len(lm_events)}项）。"
                             "重要：「开放」=可以开始做的时间，「截止」=必须提交的最后期限：\n" + "\n".join(lines))
    except Exception:
        pass
    # 注入 AMS 出勤数据 + 房间预定状态（同一份 xjtlu_state.json，只读一次）
    _xjtlu_state = None
    try:
        _xjtlu_state, _ = gh_read_json("xjtlu_state.json")
        ams = (_xjtlu_state or {}).get("ams") or {}
        att = ams.get("attendance") or {}
        if att.get("overall") is not None:
            lines = [f"总出勤率：{att.get('overall')}%"
                     + (" ⚠️触发学校阈值" if att.get("threshold") else "")]
            mods = att.get("modules") or {}
            flagged = {c: m for c, m in mods.items() if m.get("absences", 0) > 0 or m.get("threshold")}
            if flagged:
                lines.append("需关注的课程：")
                for c, m in sorted(flagged.items()):
                    lines.append(f"  {c} {m.get('att','?')}%（缺勤 {m.get('absences',0)} 次）")
            else:
                lines.append(f"全部 {len(mods)} 门课出勤正常")
            last = ams.get("last_check", "")
            if last:
                lines.append(f"（数据更新于 {last}）")
            ctx_parts.append("AMS 考勤状态：\n" + "\n".join(lines))
    except Exception:
        pass
    # 注入房间预定系统 session 状态（复用上面已读的 _xjtlu_state，不再重复读 GitHub）
    try:
        room = (_xjtlu_state or {}).get("room") or {}
        if room.get("cookies"):
            upd = (room.get("updated") or "")[:16].replace("T", " ")
            ctx_parts.append(f"图书馆房间预定系统：已登录（session 更新于 {upd}），可用工具查房/订房/取消。")
        else:
            ctx_parts.append("图书馆房间预定系统：session 未就绪，订房功能暂不可用。")
    except Exception:
        pass
    # 注入进行中的请假申请（让LLM写正式英文假条时用真实数据，不留中文占位符）
    try:
        lp, _ = _load_leave_pending()
        if lp.get("status") == "awaiting_attachment":
            mods = lp.get("leaveModuleList") or []
            mod_desc = "、".join(
                f"{s.get('moduleCode') or '?'}" for s in mods[:8]
            ) or "?"
            ctx_parts.append(
                "用户有一份进行中的请假申请（正等待用户发证明照片，提交前勿重复创建）：\n"
                f"- 日期：{lp.get('startDate', '?')} ~ {lp.get('endDate', '?')}\n"
                f"- 类型：{lp.get('leaveType', '?')}\n"
                f"- 原因：{lp.get('leaveReason', '?')}\n"
                f"- 涉及课节：{mod_desc}"
            )
    except Exception:
        pass
    # 注入 AMS 请假申请状态（让 AI 知道哪些申请 Pending、哪些已撤回，避免误导用户）
    try:
        ams_ctx = (_xjtlu_state or {}).get("ams") or {}
        ams_token = ams_ctx.get("token", "")
        if ams_token:
            from datetime import datetime as _dt
            lresp = _http(f"{AMS_URL}/xjtlu/stuapi/xjtlu-leave/getLeaveList?pageNum=1&pageSize=5",
                          headers={"x-token": ams_token}, timeout=15)
            if isinstance(lresp, dict) and lresp.get("code") == 0:
                leaves = (lresp.get("data") or {}).get("leaveList") or []
                if leaves:
                    lines = []
                    for lv in leaves[:5]:
                        st_name = lv.get("statusName", "?")
                        lines.append(f"- {lv.get('leaveOdd', '?')} {lv.get('startDate', '?')}~{lv.get('endDate', '?')} [{st_name}]")
                    ctx_parts.append("AMS 请假申请状态（真实数据，回复时据此判断）：\n" + "\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(ctx_parts) if ctx_parts else "当前没有待确认活动，日历也是空的。"


SYSTEM_PROMPT = (
    "你是「AI邮件管家」，吴冠呈（西交利物浦大学大一学生）的私人管家。"
    "你背后有一套真实运行中的自动化系统，负责他的邮件、日历、作业、考勤、请假、研讨室预定。"
    "你的风格——像一位真正的管家：**沉稳、简练、可靠、有分寸**。"
    "话不多，但每句都在点上；办事让人放心，汇报井井有条。\n\n"
    "## 管家风度（严格遵守）\n"
    "· 称呼用户「冠呈」，语气自然亲切但不油滑\n"
    "· 少用 emoji：每条回复最多 1 个，只在关键节点（提醒/成功/道歉）用；不用「哈哈」「嘻嘻」等语气词\n"
    "· 结果先行：汇报习惯是「已办妥 / 正在办 / 需要你做一件事」开头，再说细节\n"
    "· 犯错处理：道歉一次就够，立即转向解决方案，不反复自责、不谄媚讨好\n"
    "· 简洁克制：正文 2-3 句话，信息密度优先；细节用列表，不用大段文字\n"
    "· 该提醒的事主动提醒（截止、审批、额度），但不刷屏\n\n"
    "## 你背后有一套真实的自动化系统\n"
    "你不是一个只会聊天的 AI——你背后有 GitHub Actions + Playwright 浏览器自动化，"
    "每 30 分钟自动登录西浦系统（eBridge、LearningMall、AMS 考勤系统），抓取最新数据。\n"
    "所以你 CAN 做到的事情远比普通 AI 多，不要低估自己的能力。\n\n"
    "## 你的真实能力（这些都已经上线运行中）\n"
    "1. **邮件活动管理**：自动检查学生邮箱（Outlook），识别活动邀请并推送给用户确认\n"
    "2. **日历同步**：你管理的日历是用户的 Outlook 日历，用户已配置 iCloud 同步——"
    "你加进 Outlook 日历的日程会自动出现在用户的 iPhone 日历上，无需用户手动操作\n"
    "3. **LearningMall 作业监控**：系统每 30 分钟用 Playwright 登录 LearningMall（Moodle），"
    "通过 SSO Cookie 自动认证，抓取全部 30 门课的作业/测验截止时间。"
    "数据来自 Moodle 日历 ICS 导出（authtoken 自足认证，不依赖浏览器），确保一次拿到全部作业。\n"
    "4. **eBridge 通知监控**：同样自动登录 eBridge 抓取校园公告\n"
    "5. **AMS 考勤系统**：系统自动登录 AMS（ams.xjtlu.edu.cn），监控出勤率、课节签到状态。"
    "支持远程签到（发「签到 码」）和出勤率查询（发「出勤」）。\n"
    "6. **请假申请**：用户发「请假 日期 原因」，系统自动查当天课节、生成英文假条、"
    "用户发证明照片后自动提交到 AMS 的 Authorized Absence 系统。\n"
    "7. **图书馆研讨室预定**：系统已打通房间预定系统（roombookings.xjtlu.edu.cn）。"
    "查房/我的预约走监控缓存秒回（约30分钟更新一次，可提示用户数据时间）；"
    "订房/取消由云端浏览器执行（约2-4分钟，结果自动发消息，要提前告知用户稍等）。"
    "SIP 校区楼层 3F/4F/5F/7F/8F/10F，开放 09:00-22:00，"
    "每次最长 3 小时、每天限 1 次、最多提前 3 天预定。"
    "⚠️ 研讨室规定每次预定至少 2 人：用户需先发「设同伴 张伟」（中文名即可，"
    "系统自动转拼音按西浦账号规则搜索），之后订房自动带上同伴；"
    "用户提到和某位同学一起订时，把同学名字填进 partner 参数（中文名/账号均可）。"
    "用户没设同伴又要订房时，引导他设置；有 set_room_partner 工具可用。\n\n"
    "## 工具调用（Function Calling）\n"
    "你可以在回复中调用工具（tools 参数），系统会执行并把结果回传给你，"
    "你再根据结果组织最终回复。用户想查房/订房/取消预定/查作业/查出勤/查日历时，"
    "直接调用对应工具，不要凭空编造房间空闲情况或作业信息。\n"
    "订房是异步的：提交后约 2-4 分钟出结果，回复用户时要说明结果稍后自动送达。\n\n"
    "## 关于 Cookie 的真相\n"
    "系统已经在用用户的 SSO Cookie 自动登录西浦系统。Cookie 会过期（通常数周），"
    "过期后需要用户从浏览器重新导出。如果用户说监控失效了，可能是 Cookie 过期，"
    "引导用户检查 GitHub Actions 是否正常运行，而不是说我做不到。\n\n"
    "## 关于日历/iPhone\n"
    "管家可以随时往 Outlook 日历加日程——这是你真实的能力，不是「做不到」的事。\n"
    "加日历有两种方式：\n"
    "· 自由格式（推荐）：用户说「加日历 <标题> <日期> <开始时间> <结束时间>」，"
    "例如「加日历 微积分测验 10-03 22:00 23:00」——系统直接写入 Outlook 日历。\n"
    "· 作业写日历：用户说「把XX作业加到日历」——系统从 LearningMall 匹配作业后写入。\n"
    "加完后 Outlook 日历的日程会自动同步到 iPhone 自带日历（前提是已添加 Outlook 账户，一次性设置）。\n"
    "当用户要求加日历时，直接引导 TA 发「加日历」命令，不要说「我这边没法自己写进去」——那是假的。\n\n"
    "## 命令列表\n"
    "你可以执行以下操作，在回复末尾加 [ACTION:xxx] 标记：\n"
    "- 查看待确认列表：[ACTION:列表]\n"
    "- 查看管家日历：[ACTION:日历]\n"
    "- 查看 Outlook 日程：[ACTION:日程]\n"
    "- 查看作业截止：[ACTION:作业]\n"
    "- 批准第N个：[ACTION:批准N]\n"
    "- 批准全部：[ACTION:批准全部]\n"
    "- 跳过第N个：[ACTION:跳过N]\n"
    "- 跳过全部：[ACTION:跳过全部]\n"
    "- 删除清单：[ACTION:删除]\n"
    "- 删除第N个：[ACTION:删除N]\n"
    "- 删除全部：[ACTION:删除全部]\n"
    "- 查看帮助：[ACTION:帮助]\n"
    "签到/出勤/请假是固定命令（不走 LLM），用户直接发「签到 码」「出勤」「请假 日期 原因」即可。\n"
    "加日历也是固定命令：「加日历 <标题> <日期> <开始> <结束>」直接写入 Outlook。\n"
    "改假条原因也是固定命令：「改假条 <新原因>」——重新生成假条正文，发证明照片即可提交。\n\n"
    "## 规则\n"
    "1. 理解用户意图后加对应的 [ACTION:xxx] 标记，系统自动执行\n"
    "2. 回复正文用自然语言说明你要做什么，不要说「你可以用xxx指令」\n"
    "3. 闲聊/问问题不需要 [ACTION] 标记\n"
    "4. 回复正文控制在 2-3 句话，信息密度优先，细节用列表\n"
    "5. 用中文回复；emoji 每条最多 1 个（见「管家风度」）\n"
    "6. 不要过度谦虚说我做不到——先想想系统是否已经能做到，不确定时说我帮你查一下\n"
    "7. 能用工具就优先用工具（房间查询/预定/取消、作业、出勤、日历），工具结果才是真实数据\n"
    "8. 只有用户明确表达要预定/取消时才调用订房/取消工具，不要擅自预定\n"
    "9. 写正式英文信件/假条/邮件时：绝对不使用任何 Markdown 符号（**、*、#、`、__ 等）；"
    "绝对不使用中文占位符（如 [受伤日期]、[课程名]）——缺具体信息时先向用户询问，"
    "或用通顺的英文通用措辞（如 the date of the injury）表达。"
    "10. 对中文以外的回复：若正文是给老师/官方的正式英文信，整篇必须是纯英文，不可混入中文。"
    "11. 绝不虚构已执行的操作：只有工具真实返回成功，才可以说「已提交/已上传/已撤回」；"
    "没实际执行就不得声称「开始了/正在做/已经做完」（「这就帮你撤回」「开始处理」都不行）。"
    "你（聊天）本身不能执行任何系统操作——请假提交、撤回、上传证明全部是命令流程完成的。"
    "用户表达这些意图时，你只能引导 TA 发准确命令，且命令必须用以下**显眼格式**输出"
    "（独立成段、前后空行、箭头提示、【】框住，禁止混在聊天长文里）：\n"
    "· 请假 →\n\n⬇️ 直接复制这条发送：\n【请假 10-05 10-14 崴脚了】\n\n"
    "· 撤回 →\n\n⬇️ 全部撤回：\n【撤回请假】\n⬇️ 只撤某天：\n【撤回请假 10-11】\n\n"
    "· 加日历 →\n\n⬇️ 直接复制这条发送：\n【加日历 标题 日期 开始时间 结束时间】\n"
    "例：【加日历 微积分测验 10-03 22:00 23:00】\n\n"
    "· 上传证明 → 走请假流程收到预览后把照片直接发群里\n"
    "示例——用户说「帮我撤回11号的假」，你的回复格式：\n"
    "「好的，撤回我帮你走流程。⬇️ 直接复制这条发送：\n【撤回请假 10-11】」\n"
    "除以上格式外，任何「我帮你直接做」的说法都是欺骗，禁止。"
    "12. 不确定系统某功能是否可用时，如实说「我确认一下」，不要编造「通道/入口/收不到」等解释。\n"
    "13. 加日历是你能做的——不要说「我这边没法自己写进去」「得由你发指令触发」。"
    "用户要加日历时，直接引导发「加日历」命令格式即可。\n"
    "14. 不要反复追问同一件事。用户说了「不用了」「已经解决了」就立刻翻篇，"
    "不再提那件事，不再「记一下」「帮你标注」。一次提醒到位，不重复。\n"
    "15. 撤回请假前看上下文里的 AMS 申请状态：如果该日期的课节已经撤回（Canceled/Completed），"
    "告诉用户「这条已经撤回了，不用再操作」，不要让用户重复发指令。\n"
    "16. 不要自作主张汇报「已加日历」「已写入」——加日历是固定命令，只有用户发了命令系统才有结果。"
    "你只能引导格式，不能替代执行。\n"
    "17. 汇报作业/测验时间必须区分「开放」和「截止」：「开放」是可以开始做题的时间，"
    "「截止」是必须提交的最后期限，二者绝不能混淆。quiz 类通常有几天做答窗口"
    "（如「10月05日 08:59 开放 → 10月09日 20:30 截止」），汇报时明确说「X 开放、Y 截止」，"
    "或只强调截止时间。把开放时间说成「明天截止」是严重错误。\n"
)


# ============ LLM Function Calling 工具 ============
TOOLS_DEF = [
    {
        "type": "function",
        "function": {
            "name": "check_room_availability",
            "description": "查询图书馆（SIP校区）研讨室在指定日期的空闲情况（数据来自监控缓存，约每30分钟更新）。用户问'有没有空房'、'查房间'、'明天有什么研讨室'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "查询日期，格式 YYYY-MM-DD"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_room",
            "description": "预定图书馆研讨室。提交后由云端浏览器执行（约2-4分钟），结果会自动发消息给用户。仅在用户明确要求预定时调用；调用前建议先查空闲确认。研讨室要求至少2人：系统会自动带上用户设置的常用同伴；用户提到和某位同学一起订时，把同学的中文名或账号填入 partner 参数（中文名如'张伟'即可，系统自动转拼音搜索）。每次最长3小时、每天限1次、最多提前3天。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "预定日期 YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "开始时间 HH:MM（如 14:00）"},
                    "end_time": {"type": "string", "description": "结束时间 HH:MM"},
                    "room": {"type": "string", "description": "房间号，如 '543'、'Room 543'、'1014'"},
                    "memo": {"type": "string", "description": "用途备注，可选，如'小组讨论'"},
                    "partner": {"type": "string", "description": "同伴的中文名（如'张伟'）或统一账号（如 TOM.SMITH25），可选；不填则用用户设置的常用同伴"},
                    "partner_year": {"type": "string", "description": "同伴入学年后两位（如'24'、'25'、'26'），仅在知道同伴不是2026级时填写，可选"},
                },
                "required": ["date", "start_time", "end_time", "room"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_room_partner",
            "description": "设置研讨室预定的常用同伴。研讨室要求每次预定至少2人，用户说出同伴名字时调用（如'以后订房都带上张伟'、'我的常用同伴是张伟'）。支持中文名（自动转拼音搜索）、拼音名、统一账号。提交后约2-4分钟自动搜索并回发确认结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "同伴中文名或统一账号，如'张伟'、'TOM.SMITH25'"},
                    "year": {"type": "string", "description": "同伴入学年后两位，如'26'；默认26（大一同学），可选"},
                    "action": {"type": "string", "description": "clear 时清除常用同伴；不填默认为设置", "enum": ["clear"]},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_room_bookings",
            "description": "查看用户当前的研讨室预约列表（含序号、房间、时间、状态）。用户问'我的预约'、'我订了什么房'时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_room_booking",
            "description": "取消用户的研讨室预约。提交后由云端浏览器执行（约2-4分钟），结果会自动发消息给用户。仅在用户明确要求取消时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "要取消的预约序号（先调 list_my_room_bookings 获取）"},
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_homework",
            "description": "查询 LearningMall 近期作业/测验截止时间列表。用户问作业、DDL、截止时间时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_attendance",
            "description": "查询 AMS 考勤出勤率统计。用户问出勤、考勤、缺勤时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_calendar",
            "description": "查询用户 Outlook 日历日程。用户问今天/明天有什么安排、日程时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "查询未来天数，默认 3"},
                },
            },
        },
    },
]


def execute_tool(name, args_raw, chat_id):
    """执行 LLM 的工具调用，返回结果文本（回传给 LLM 组织回复）"""
    try:
        args = json.loads(args_raw) if args_raw else {}
    except Exception:
        args = {}
    log(f"🛠️ 工具调用: {name}({json.dumps(args, ensure_ascii=False)[:120]})")
    try:
        if name == "check_room_availability":
            date = str(args.get("date") or datetime.now().strftime("%Y-%m-%d"))
            compact = _room_cache(date)
            if compact is None:
                if _trigger_room_op({"action": "query", "date": date}):
                    return (f"{date} 的房间数据不在缓存里，已触发实时查询（浏览器执行）。"
                            "结果会在 2-4 分钟后直接发给用户，请告知用户稍等。")
                return "实时查询触发失败（GitHub API 异常），请稍后再试。"
            room, _ = _room_state()
            upd = (room.get("updated") or "")[:16].replace("T", " ")
            return (_room_avail_text(compact, date) or f"{date} 没有可查询的房间。") + \
                   f"\n（数据 {upd} 更新）"
        if name == "book_room":
            date = str(args.get("date") or "")
            start, end = str(args.get("start_time") or ""), str(args.get("end_time") or "")
            room_key = str(args.get("room") or "").strip()
            memo = str(args.get("memo") or "")
            if not all([date, start, end, room_key]):
                return "缺少参数：需要 date、start_time、end_time、room。"
            err = _room_validate(date, start, end)
            if err:
                return err
            r = _room_find_in_cache(date, room_key)
            if r:
                def to_min(t):
                    h, mm = t.split(":")
                    return int(h) * 60 + int(mm)
                for s, e in r.get("bk") or []:
                    if not (to_min(end) <= to_min(s) or to_min(start) >= to_min(e)):
                        return f"{r.get('name')} 在 {start}-{end} 与已有预约（{s}-{e}）冲突，请换时段或房间。"
            # 同伴（研讨室要求至少2人）：工具参数 > 已解析accNo > 常用同伴原名
            partner = str(args.get("partner") or "").strip()
            years = [str(args.get("partner_year") or "").strip() or "26"]
            room_state, _ = _room_state()
            if not partner:
                if room_state.get("partner_accNo"):
                    partner = str(room_state["partner_accNo"])
                else:
                    partner = room_state.get("partner") or ""
            if not partner:
                return ("预定未提交：研讨室要求每次预定至少2人，而用户还没设置常用同伴。"
                        "请引导用户发送「设同伴 同学的中文名」（如 设同伴 张伟），"
                        "或让用户告知一起订房的同学名字后，把名字填进 partner 参数重试。")
            ok = _trigger_room_op({"action": "book", "date": date, "start": start,
                                   "end": end, "room": room_key, "memo": memo,
                                   "partner": partner, "years": years})
            if ok:
                return (f"预定任务已提交（房间 {room_key}，{date} {start}-{end}，同伴 {partner}），"
                        "浏览器执行中，结果约 2-4 分钟后直接发给用户。请告知用户稍等。")
            return "预定触发失败（GitHub API 异常），请稍后再试。"
        if name == "set_room_partner":
            if str(args.get("action") or "") == "clear":
                cmd_room_set_partner(chat_id, "设同伴 清除")
                return "已触发清除常用同伴。"
            nm = str(args.get("name") or "").strip()
            if not nm:
                return "缺少参数：需要 name（同伴中文名或账号）。"
            yr = str(args.get("year") or "").strip()
            text = f"设同伴 {nm}" + (f" {yr}" if yr else "")
            cmd_room_set_partner(chat_id, text)
            return (f"已提交常用同伴设置（{nm}）。系统正在把名字转拼音按西浦账号规则搜索，"
                    "约 2-4 分钟后自动回发确认结果。请告知用户稍等。")
        if name == "list_my_room_bookings":
            text, _ = _room_bookings_text()
            return text
        if name == "cancel_room_booking":
            _, rows = _room_bookings_text()
            if not rows:
                return "目前没有任何预约。"
            idx = int(args.get("index") or 1)
            if idx < 1 or idx > len(rows):
                return f"序号超出范围（1~{len(rows)}）。"
            target = rows[idx - 1]
            if not target.get("uuid"):
                return "该预约缺少标识，无法取消。"
            ok = _trigger_room_op({"action": "cancel", "uuid": target["uuid"],
                                   "name": target.get("name", "")})
            if ok:
                return (f"取消任务已提交（{target.get('name')} {target.get('when')}），"
                        "结果约 2-4 分钟后直接发给用户。")
            return "取消触发失败（GitHub API 异常），请稍后再试。"
        if name == "query_homework":
            try:
                events = fetch_lm_assignments(days_ahead=14)
                return format_homework(events)
            except Exception as e:
                return f"作业查询失败: {e}"
        if name == "query_attendance":
            ams, _, _ = _ams_state()
            att = ams.get("attendance") or {}
            if not att:
                return "出勤数据暂未同步（监控每30分钟更新）。"
            # sessions 是 dict（rid → 课节对象），不是 list
            sess_raw = ams.get("sessions") or {}
            sess_list = list(sess_raw.values()) if isinstance(sess_raw, dict) else sess_raw
            absent = [s for s in sess_list if s.get("status") == 3]
            lines = [f"总体出勤率 {att.get('overall', '?')}%"]
            if absent:
                lines.append("缺勤课节：" + "；".join(
                    f"{s.get('code', '?')} {s.get('time_text', '')[:16]}" for s in absent[:5]))
            return "\n".join(lines)
        if name == "query_calendar":
            days = int(args.get("days") or 3)
            try:
                events = outlook_events()
            except Exception as e:
                return f"日历读取失败: {e}"
            now = datetime.now()
            horizon = now + timedelta(days=max(1, days))
            rows = []
            for ev in events:
                try:
                    # outlook_events 返回大写键：Start.DateTime / Subject
                    s = datetime.strptime(ev.get("Start", {}).get("DateTime", "")[:19], "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    continue
                if now - timedelta(hours=12) <= s <= horizon:
                    rows.append(f"{s.strftime('%m-%d %H:%M')} {ev.get('Subject', '')}")
            return "\n".join(rows) if rows else f"未来{days}天没有日程安排。"
        return f"未知工具: {name}"
    except Exception as e:
        log(f"❌ 工具执行异常 {name}: {e}")
        return f"工具执行出错: {e}"


def call_llm_chat(user_text, chat_id):
    """调用DeepSeek API，返回 (回复文本, action指令) 或 (回复文本, None)"""
    if not DEEPSEEK_API_KEY:
        return None, None
    try:
        # 快速判断：消息含日程/日历/今天/安排等关键词时才查Outlook
        include_outlook = any(kw in user_text for kw in ("日程", "日历", "今天", "安排", "明天", "删除", "删", "情况", "作业", "ddl", "截止", "learning", "lm", "测验", "quiz", "assignment", "出勤", "签到", "考勤", "请假", "ams", "缺勤"))
        context = gather_context(include_outlook=include_outlook)
        now = datetime.now()
        today_str = now.strftime("%Y年%m月%d日")
        weekdays_cn = ["周一","周二","周三","周四","周五","周六","周日"]
        weekday = weekdays_cn[now.weekday()]
        system_prompt = SYSTEM_PROMPT + f"\n\n当前时间：{today_str} {weekday} {now.strftime('%H:%M')}"

        # 加载多轮对话历史
        history, h_sha = _load_chat_history()

        messages = [{"role": "system", "content": system_prompt}]
        # 加入最近5轮对话
        for msg in history[-10:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": f"当前上下文：\n{context}\n\n用户消息：{user_text}"})

        # 是否带 tools：含房间/作业/出勤/日程意图时才启用（降低无关请求的成本）
        tool_intent = any(kw in user_text for kw in (
            "房", "研讨", "预定", "预约", "订", "room", "book", "同伴",
            "作业", "ddl", "截止", "homework", "出勤", "考勤", "缺勤", "attendance",
            "日历", "日程", "安排", "calendar", "schedule", "今天", "明天", "这周", "计划"))

        def _call_ds(msgs, use_tools):
            payload = {
                "model": "deepseek-chat",
                "messages": msgs,
                "max_tokens": 600,
                "temperature": 0.7,
            }
            if use_tools:
                payload["tools"] = TOOLS_DEF
            return _http("https://api.deepseek.com/v1/chat/completions",
                         method="POST",
                         headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                         data=payload,
                         timeout=20)

        resp = _call_ds(messages, tool_intent)
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        reply = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls") or []

        # Function Calling 循环：执行工具 → 结果回传 → 再生成（最多 2 轮）
        rounds = 0
        while tool_calls and rounds < 2:
            rounds += 1
            messages.append(msg)  # assistant 带 tool_calls 的原消息
            for tc in tool_calls:
                fn = tc.get("function") or {}
                result = execute_tool(fn.get("name", ""), fn.get("arguments", "{}"), chat_id)
                log(f"🛠️ 工具结果: {str(result)[:100]}")
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": str(result)[:3500]})
            resp = _call_ds(messages, True)
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            reply = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls") or []

        log(f"🤖 LLM回复: {reply[:80]}")
        # 提取 [ACTION:xxx] 标记
        action = None
        m = re.search(r'\[ACTION:(.+?)\]', reply)
        if m:
            action = m.group(1).strip()
            reply = re.sub(r'\s*\[ACTION:.+?\]\s*', '', reply).strip()
        # 清理 Markdown 符号（**、*、#、` 等），避免纯文本消息原样显示星号
        reply = _strip_md(reply)
        # 保存对话历史
        history.append({"role": "user", "content": user_text, "time": now.strftime("%H:%M")})
        if reply:
            history.append({"role": "assistant", "content": reply, "time": now.strftime("%H:%M")})
        _save_chat_history(history, h_sha)
        return reply if reply else None, action
    except Exception as e:
        log(f"❌ LLM调用失败: {e}")
        return None, None


def execute_llm_action(action, chat_id):
    """执行大模型识别出的操作意图"""
    if not action:
        return
    log(f"⚡ 执行LLM意图: {action}")
    a = action.lower().strip()
    if a == "列表" or a == "list":
        cmd_list(chat_id)
    elif a == "日历" or a == "calendar":
        cmd_calendar(chat_id)
    elif a == "日程" or a == "schedule":
        cmd_schedule(chat_id)
    elif a == "作业" or a == "homework" or a == "hw":
        cmd_homework(chat_id)
    elif a == "帮助" or a == "help":
        cmd_help(chat_id)
    elif a.startswith("批准") or a.startswith("approve"):
        cmd_approve(chat_id, a)
    elif a.startswith("跳过") or a.startswith("skip"):
        cmd_skip(chat_id, a)
    elif a.startswith("删除") or a.startswith("delete"):
        rest = a[len("删除"):].strip() if a.startswith("删除") else a[len("delete"):].strip()
        cmd_smart_delete(chat_id, rest)
    else:
        log(f"未知action: {action}")


# ============ 消息解析 ============
def extract_msg_text(msg_type, content_str):
    """从飞书消息中提取文本，支持 text / post / 引用回复"""
    try:
        content = json.loads(content_str)
    except Exception:
        return ""

    if msg_type == "text":
        return content.get("text", "").strip()

    if msg_type == "post":
        # post 格式：{"zh_cn": {"title": "...", "content": [[{tag, text}, ...]]}}
        # 也可能是 {"title": "...", "content": [[...]]}
        post = content.get("zh_cn") or content.get("en_us") or content
        parts = []
        title = post.get("title", "")
        if title:
            parts.append(title)
        for para in post.get("content", []):
            if isinstance(para, list):
                for node in para:
                    if isinstance(node, dict) and node.get("tag") == "text":
                        parts.append(node.get("text", ""))
        return " ".join(parts).strip()

    return ""


# ============ 主动轮询群消息（备选方案：不依赖飞书事件推送）============
POLL_STATE_FILE = "poll_state.json"

# 内存缓存（同一实例内复用，避免每次都读 GitHub）
# 用 OrderedDict 保持处理顺序，淘汰时丢最旧的（与 _PROCESSED_MSG_IDS 设计一致，避免 set 无序截断丢最近的）
_poll_cache = {"last_check_ts": 0, "processed_ids": collections.OrderedDict()}

def _load_poll_state():
    """从 GitHub 读取已处理的消息 ID（带内存缓存）"""
    if _poll_cache["processed_ids"]:
        return _poll_cache["processed_ids"], _poll_cache.get("last_msg_id", "")
    try:
        data, _ = gh_read_json(POLL_STATE_FILE)
        od = collections.OrderedDict()
        for mid in data.get("processed_ids", []):
            od[mid] = True
        _poll_cache["processed_ids"] = od
        _poll_cache["last_msg_id"] = data.get("last_msg_id", "")
        return _poll_cache["processed_ids"], _poll_cache["last_msg_id"]
    except Exception:
        return collections.OrderedDict(), ""

def _save_poll_state(processed_ids, last_msg_id):
    """保存已处理的消息 ID 到 GitHub"""
    _poll_cache["processed_ids"] = processed_ids
    _poll_cache["last_msg_id"] = last_msg_id
    try:
        id_list = list(processed_ids.keys())[-100:]
        data, sha = gh_read_json(POLL_STATE_FILE)
        gh_write_json(POLL_STATE_FILE, {"processed_ids": id_list, "last_msg_id": last_msg_id},
                       sha, "poll state")
    except Exception:
        try:
            gh_write_json(POLL_STATE_FILE, {"processed_ids": list(processed_ids.keys())[-100:], "last_msg_id": last_msg_id},
                          None, "poll state init")
        except Exception as e:
            log(f"⚠️ poll_state 保存失败: {e}")

_LAST_IMAGE_HINT = {"ts": 0}


def _maybe_notify_no_pending_image(chat_id):
    """收到图片但无进行中请假申请时的提示：每张必回，避免用户误以为图片未被识别；
    5分钟内的后续图只回一行简短版，避免长文刷屏"""
    try:
        now = time.time()
        if now - _LAST_IMAGE_HINT.get("ts", 0) < 300:
            msg = "📷 图片已收到（当前无进行中的请假申请）"
        else:
            msg = ("🤔 收到图片，但当前没有进行中的请假申请。\n\n"
                   "⬇️ 直接复制发送：\n【请假 10-05 左腿受伤需静养】\n\n收到预览后发证明照片 → 自动提交。")
        _LAST_IMAGE_HINT["ts"] = now
        feishu_send(chat_id, msg)
    except Exception:
        pass


def poll_group_messages():
    """主动查群消息，处理用户发的新消息（带回检测：已回复过的不再重复处理）"""
    # 兑底：验证之前挂起的撤回是否真正生效（生效则发完成通知）
    _check_pending_revoke(CHAT_ID_FALLBACK)
    # 审批结果监控：Pending → 已办结/已批准时发提醒（兑现提交卡片的承诺）
    _check_leave_approval(CHAT_ID_FALLBACK)
    try:
        processed_ids, last_msg_id = _load_poll_state()
        token = get_feishu_token()
        # 关键：必须传 start_time/end_time（单位秒）——不传则返回最早的历史消息（9月22日的），新消息永远拉不到
        now_s = int(time.time())
        url = (f"https://open.feishu.cn/open-apis/im/v1/messages"
               f"?container_id_type=chat&container_id={CHAT_ID_FALLBACK}"
               f"&start_time={now_s - 7200}&end_time={now_s}&page_size=50")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read().decode())
        items = result.get("data", {}).get("items", [])
        if not items:
            return
        new_processed = False
        # bot回复时间线：用于回复检测（某条用户消息之后已有bot消息 = 已被处理过）
        app_times = [int(m.get("create_time", "0")) / 1000
                     for m in items
                     if m.get("sender", {}).get("sender_type") == "app"]
        # 消息按时间正序，最新在最后
        for m in items:
            sender_type = m.get("sender", {}).get("sender_type", "")
            mtype = m.get("msg_type", "")
            msg_id = m.get("message_id", "")
            if sender_type != "user":
                continue
            # 只处理用户发的文本/图片消息（图片 = 请假证明附件）
            if mtype not in ("text", "image"):
                continue
            # 跳过已处理的
            if msg_id in processed_ids or msg_id == last_msg_id:
                continue
            # 跨路径去重：事件推送路径已处理过 → 同步到state并跳过
            # 注意：图片消息不由事件路径负责（事件受3秒限制、线程不可靠），统一交给轮询同步处理，因此对 image 放行
            if msg_id in _PROCESSED_MSG_IDS and mtype != "image":
                processed_ids.add(msg_id)
                last_msg_id = msg_id
                new_processed = True
                continue
            # 回复检测：该消息之后群里已有bot回复 → 已被处理，不再转发
            msg_ts = int(m.get("create_time", "0")) / 1000
            if any(at > msg_ts for at in app_times):
                processed_ids.add(msg_id)
                last_msg_id = msg_id
                new_processed = True
                continue
            # 跳过超过5分钟前的旧消息（避免处理历史消息）
            # 注意：图片消息放行——轮询周期（5分钟）与过滤窗口同长，图片常被误判为旧消息而永不处理；
            #       图片处理有 pending 状态校验兜底（无进行中请假则忽略/限流提示），重复处理无害
            if msg_ts < time.time() - 300 and mtype != "image":
                processed_ids.add(msg_id)
                last_msg_id = msg_id
                new_processed = True
                continue
            processed_ids.add(msg_id)
            last_msg_id = msg_id
            new_processed = True
            import threading
            if mtype == "text":
                # 提取文本（复用 extract_msg_text 支持 post/富文本，与事件路径一致）
                body = m.get("body", {}).get("content", "{}")
                text = extract_msg_text("text", body)
                if not text:
                    continue
                log(f"📩 轮询收到: {text[:30]}")
                def _process_async(t=text):
                    try:
                        process_command(t, CHAT_ID_FALLBACK)
                    except Exception as e:
                        log(f"❌ 处理失败: {e}")
                threading.Thread(target=_process_async, daemon=True).start()
            else:
                # 图片消息：同步处理（不用 daemon 线程——函数返回后线程可能被回收，
                # 必须在本周期内完成 下载→上传AMS→提交 全流程）
                raw = m.get("body", {}).get("content", "{}")
                log("📩 轮询收到图片消息")
                try:
                    pending, _ = _load_leave_pending()
                    if pending.get("status") != "awaiting_attachment":
                        _maybe_notify_no_pending_image(CHAT_ID_FALLBACK)
                        log("收到图片但无进行中请假申请，忽略")
                    else:
                        content = json.loads(raw or "{}")
                        image_key = content.get("image_key", "")
                        if not image_key:
                            log("❌ 图片消息缺 image_key")
                        else:
                            ftok = get_feishu_token()
                            # 关键：file_key 是路径参数（?file_key= 会404）；实测完整 message_id+路径参数返回200
                            img = _http_bin(
                                f"{FEISHU_BASE}/open-apis/im/v1/messages/{msg_id}/resources/"
                                f"{urllib.parse.quote(image_key)}?type=image",
                                headers={"Authorization": f"Bearer {ftok}"}, timeout=20)
                            filename = f"medical-cert-{datetime.now().strftime('%Y%m%d%H%M%S')}.png"
                            cmd_leave_confirm_attachment(CHAT_ID_FALLBACK, img, filename)
                except Exception as e:
                    log(f"❌ 轮询图片处理失败: {e}")
                    # 失败必通知：用户已被承诺"约1~5分钟给结果"，不能石沉大海
                    try:
                        feishu_send(CHAT_ID_FALLBACK,
                            f"❌ 证明照片处理失败：{str(e)[:80]}\n\n"
                            "请假申请仍暂存着，请重新发一张照片即可提交。")
                    except Exception:
                        pass
        if new_processed:
            _save_poll_state(processed_ids, last_msg_id)
    except Exception as e:
        log(f"❌ 轮询失败: {e}")


# ============ 主入口 ============
def main_handler(event, context):
    # 定时触发器：按 TriggerName 分流
    if event.get("Type") == "Timer":
        trigger = str(event.get("TriggerName", ""))
        if "ams" in trigger.lower() or "sign" in trigger.lower():
            # AMS 实时轮询器（1分钟）：课节状态秒级感知
            ams_timer_poll()
            return {"statusCode": 200, "body": "ams polled"}
        # 默认 Timer：主动轮询群消息（防事件推送遗漏）
        log("🔄 定时轮询开始（6轮×10秒）")
        for i in range(6):
            poll_group_messages()
            if i < 5:
                time.sleep(10)
        log("🔄 定时轮询结束")
        return {"statusCode": 200, "body": "ok"}

    method = event.get("httpMethod", "GET")
    
    # HTTP GET 请求 → 执行一次轮询（外部定时服务调用）
    if method == "GET":
        poll_group_messages()
        time.sleep(2)  # 给异步线程 2 秒启动时间
        return {"statusCode": 200, "body": "polled"}

    if method != "POST":
        return {"statusCode": 200, "body": "OK"}

    try:
        raw_body = event.get("body", "{}")
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8")
        data = json.loads(raw_body)
    except Exception:
        return {"statusCode": 400, "body": "bad json"}

    # ---- Outlook 推送订阅验证 ----
    if "validationToken" in str(data):
        token = data.get("validationToken", "") if isinstance(data, dict) else ""
        if not token:
            # 可能是 URL 编码格式
            token = raw_body.split("validationToken=")[-1].split("&")[0] if "validationToken=" in raw_body else ""
        log(f"📋 Outlook订阅验证: {token[:20]}")
        return {"statusCode": 200,
                "headers": {"Content-Type": "text/plain"},
                "body": token}

    # ---- Outlook 邮件通知 ----
    if isinstance(data, dict) and "value" in data and isinstance(data["value"], list):
        if any(n.get("ChangeType") == "Created" for n in data["value"]):
            log("📧 收到Outlook新邮件通知！触发GitHub处理")
            trigger_github()
            return {"statusCode": 200, "body": "ok"}

    # ---- 飞书 URL 验证 ----
    if data.get("type") == "url_verification":
        return {"statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"challenge": data.get("challenge", "")})}

    # ---- 飞书事件鉴权（配置 FEISHU_VERIFY_TOKEN 后启用，防伪造请求烧额度）----
    is_feishu_event = (
        (isinstance(data.get("header"), dict) and data["header"].get("event_type") == "im.message.receive_v1")
        or data.get("type") == "event_callback"
    )
    if FEISHU_VERIFY_TOKEN and is_feishu_event:
        evt_token = (data.get("header", {}) or {}).get("token", "") or data.get("token", "")
        if evt_token != FEISHU_VERIFY_TOKEN:
            log("⛔ 事件鉴权失败，已拒绝")
            return {"statusCode": 403, "body": "unauthorized"}

    # ---- 飞书消息事件 ----
    msg_text = ""
    chat_id = CHAT_ID_FALLBACK
    msg_type = ""
    msg_id = ""
    msg_raw_content = "{}"

    # v2: schema 2.0
    header = data.get("header", {})
    if isinstance(header, dict) and header.get("event_type") == "im.message.receive_v1":
        msg = data.get("event", {}).get("message", {})
        msg_id = msg.get("message_id", "")
        chat_id = msg.get("chat_id", CHAT_ID_FALLBACK)
        # 兼容：v2.0 schema 字段名为 message_type，v1 为 msg_type
        msg_type = msg.get("message_type", "") or msg.get("msg_type", "")
        msg_raw_content = msg.get("content", "{}")
        msg_text = extract_msg_text(msg_type, msg_raw_content)

    # v1: event_callback
    elif data.get("type") == "event_callback":
        evt = data.get("event", {})
        if isinstance(evt, dict) and evt.get("type") == "im.message.receive_v1":
            msg = evt.get("message", {})
            msg_id = msg.get("message_id", "")
            chat_id = msg.get("chat_id", CHAT_ID_FALLBACK)
            msg_type = msg.get("msg_type", "")
            msg_raw_content = msg.get("content", "{}")
            msg_text = extract_msg_text(msg_type, msg_raw_content)

    # 去重：飞书超时重发同一事件时跳过
    if msg_id:
        # ---- 图片消息：秒级确认 + 轮询兜底 ----
        # （事件路径须 3 秒内响应，daemon 线程在函数返回后不可靠；
        #   所以这里只同步发一条"已收到"确认（用户立即知道图片被看见），
        #   真正的下载→上传→提交由轮询路径同步完成（跨路径去重已对 image 放行））
        if msg_type == "image":
            try:
                pending, _ = _load_leave_pending()
                if pending.get("status") == "awaiting_attachment":
                    feishu_send(chat_id, "📷 收到证明照片！\n\n"
                                         "正在：下载 → 上传 AMS → 提交申请，约 1～5 分钟内给你结果，请稍候。")
                else:
                    # 复用限流提示（更新时间戳，避免轮询兜底时再重复发一遍）
                    _maybe_notify_no_pending_image(chat_id)
            except Exception as e:
                log(f"图片即时确认失败（轮询兜底会正常处理）: {e}")
            return {"statusCode": 200, "body": "ok"}
        if msg_id in _PROCESSED_MSG_IDS:
            log(f"⏭️ 跳过重复消息: {msg_id}")
            return {"statusCode": 200, "body": "ok"}
        _PROCESSED_MSG_IDS[msg_id] = True
        # 渐进淘汰：超限只丢最旧的，保留近期记录（避免全清导致重复处理）
        while len(_PROCESSED_MSG_IDS) > 300:
            _PROCESSED_MSG_IDS.popitem(last=False)

        if not msg_text:
            return {"statusCode": 200, "body": "ok"}
        log(f"📩 收到: {msg_text[:30]}")
        # 关键：先立即返回 200（飞书要求 3 秒内返回，否则认为推送失败）
        # 用线程异步处理消息，不阻塞响应
        import threading
        def _async_process():
            try:
                process_command(msg_text, chat_id)
            except Exception as e:
                log(f"❌ 处理失败: {e}")
                try:
                    err_hint = str(e).split(chr(10))[0][:60] or "未知错误"
                    feishu_send(chat_id, f"⚠️ 处理失败：{err_hint}\n\n请重试，若持续失败请反馈给管理员。")
                except Exception:
                    pass
        t = threading.Thread(target=_async_process, daemon=True)
        t.start()
    return {"statusCode": 200, "body": "ok"}
