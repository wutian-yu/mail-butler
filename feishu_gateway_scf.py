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
    """读取私有数据仓库中的 JSON 文件，返回 (data, sha)；失败重试1次"""
    url = f"https://api.github.com/repos/{DATA_REPO}/contents/{urllib.parse.quote(path, safe='/')}"
    last_err = None
    for attempt in range(2):
        try:
            resp = _http(url, headers={"Authorization": f"Bearer {GH_TOKEN}",
                                       "Accept": "application/vnd.github+json"})
            content = base64.b64decode(resp["content"]).decode("utf-8")
            return json.loads(content), resp.get("sha", "")
        except Exception as e:
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
    except Exception:
        pass


# ============ Outlook API ============
def get_outlook_token():
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
        return json.loads(r.read().decode())["access_token"]


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
    "📋「列表」待确认活动\n"
    "✅「批准 [N]」加入日历\n"
    "✅「批准全部」批量加入\n"
    "⏭️「跳过 [N]」忽略活动\n"
    "⏭️「跳过全部」批量忽略\n"
    "📊「日历」管家已加入的活动\n"
    "📅「日程」Outlook全部日程\n"
    "🗑️「删除」查看可删除清单\n"
    "🗑️「删除 N / 日期 / 名称」删除指定日程\n"
    "🗑️「删除全部」一键清空\n"
    "🏫「签到 码」远程签到 AMS（72小时内可补）\n"
    "📊「出勤」AMS 出勤率统计\n\n"
    "💡 删除=自动判断从哪移除，不用操心\n"
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
    except Exception:
        pass


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
        for item in active:
            item["status"] = "confirmed"
            confirmed.setdefault("items", []).append(item)
        pending["items"] = [i for i in pending["items"] if i.get("status") != "pending"]
        gh_write_json("pending.json", pending, p_sha, "approve all")
        try:
            confirmed2, c_sha2 = gh_read_json("confirmed.json")
            confirmed2["items"] = confirmed["items"]
            gh_write_json("confirmed.json", confirmed2, c_sha2, "approve all")
        except Exception:
            gh_write_json("confirmed.json", confirmed, c_sha, "approve all")
        feishu_send_action(chat_id, "✅ 批量批准完成", f"已批准 **{count}** 个活动\n\n将自动同步到你的 iPhone 日历。", color="green")
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
    if conflict_msg:
        result_msg += f"\n\n⚠️ **冲突提醒**\n{conflict_msg}"
        feishu_send_action(chat_id, "✅ 已加入日历（有冲突）", result_msg, color="orange")
    else:
        feishu_send_action(chat_id, "✅ 已加入日历", result_msg + "\n\n将自动同步到你的 iPhone 日历。", color="green")
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
        feishu_send(chat_id, "❌ 暂无 AMS 签到凭证（x-token）。\n\n"
                             "监控每 30 分钟自动刷新一次，稍后再试；\n"
                             "若持续失败，请检查监控 Actions 是否正常运行。")
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


def process_command(text, chat_id):
    t = text.lower().strip()
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
        if "删除" in t or "delete" in t or "取消" in t or "revoke" in t or "去掉" in t or "移除" in t:
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
    elif t.startswith("删除") or t.startswith("delete") or t.startswith("取消") or t.startswith("revoke") or t.startswith("去掉") or t.startswith("移除"):
        rest = t
        for prefix in ("删除", "delete", "取消", "revoke", "去掉", "移除"):
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
            # 倒计时
            delta = dt - now
            if delta.days == 0:
                countdown = f"今天 {dt.strftime('%H:%M')}"
            elif delta.days == 1:
                countdown = f"明天 {dt.strftime('%H:%M')}"
            else:
                countdown = f"{delta.days}天后"
            # 拼接标题：【课程名】+ 英文作业名（不含动作词）
            full_title = f"【{course_label}】{title}" if course_label else title
            events.append({
                "summary": full_title,
                "action": action,
                "dt": dt,
                "countdown": countdown,
                "categories": categories,
            })
        events.sort(key=lambda e: e["dt"])
        return events
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
        act = ev.get("action", "")
        lines.append(f"　　⏰ {ev['dt'].strftime('%m月%d日 %H:%M')} · {act} · {ev['countdown']}" if act
                     else f"　　⏰ {ev['dt'].strftime('%m月%d日 %H:%M')} · {ev['countdown']}")
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
            # 状态图标：截止🔴 开放🟢
            if it["action"] == "截止":
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
    # 注入 LM 作业上下文（关键词触发或 include_outlook 时）
    try:
        lm_events = fetch_lm_assignments()
        if lm_events:
            lines = [f"- {ev['summary'][:45]} ({ev['dt'].strftime('%m月%d日 %H:%M')}, {ev['countdown']})" for ev in lm_events[:10]]
            ctx_parts.append(f"LearningMall 近期作业/测验（共{len(lm_events)}项）：\n" + "\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(ctx_parts) if ctx_parts else "当前没有待确认活动，日历也是空的。"


SYSTEM_PROMPT = (
    "你是「AI邮件管家」，一个贴心、智能的私人助理。"
    "用户是西交利物浦大学大一学生，名叫吴冠呈。"
    "你的风格：友善、简洁、有温度，像朋友一样聊天，不要太官方。\n\n"
    "你可以帮用户管理邮件活动和日历日程。\n"
    "你还可以查看用户的 LearningMall 作业截止时间。\n\n"
    "## 你的能力\n"
    "你可以执行以下操作，在回复中用特殊标记表示要执行的操作：\n"
    "- 查看待确认列表：在回复末尾加 [ACTION:列表]\n"
    "- 查看日历：在回复末尾加 [ACTION:日历]\n"
    "- 查看日程：在回复末尾加 [ACTION:日程]\n"
    "- 查看作业：在回复末尾加 [ACTION:作业]\n"
    "- 批准第N个：在回复末尾加 [ACTION:批准N]（N为编号）\n"
    "- 批准全部：在回复末尾加 [ACTION:批准全部]\n"
    "- 跳过第N个：在回复末尾加 [ACTION:跳过N]\n"
    "- 跳过全部：在回复末尾加 [ACTION:跳过全部]\n"
    "- 删除（显示清单）：在回复末尾加 [ACTION:删除]\n"
    "- 删除第N个：在回复末尾加 [ACTION:删除N]\n"
    "- 删除全部：在回复末尾加 [ACTION:删除全部]\n"
    "- 查看帮助：在回复末尾加 [ACTION:帮助]\n\n"
    "## 关于作业\n"
    "当用户问\"有什么作业\"、\"作业是什么\"、\"ddl\"、\"截止\"、\"learning mall\"、\"LM\"时，\n"
    "加 [ACTION:作业] 标记，系统会自动拉取 LearningMall 日历中的作业截止时间并展示。\n"
    "作业数据来自 Moodle 日历 ICS 订阅，包含全部课程的作业/测验截止时间。\n\n"
    "## 规则\n"
    "1. 如果用户表达了操作意图（即使不是标准指令），理解意图后加上对应的[ACTION:xxx]标记，系统会自动执行\n"
    "2. 回复正文用自然语言说明你要做什么，不要说「你可以用xxx指令」\n"
    "3. 如果用户只是闲聊、问问题，不需要加任何[ACTION]标记\n"
    "4. 回复正文控制在2-3句话，简洁有温度\n"
    "5. 用中文回复，可以用少量emoji"
)


def call_llm_chat(user_text, chat_id):
    """调用DeepSeek API，返回 (回复文本, action指令) 或 (回复文本, None)"""
    if not DEEPSEEK_API_KEY:
        return None, None
    try:
        # 快速判断：消息含日程/日历/今天/安排等关键词时才查Outlook
        include_outlook = any(kw in user_text for kw in ("日程", "日历", "今天", "安排", "明天", "删除", "删", "情况", "作业", "ddl", "截止", "learning", "lm", "测验", "quiz", "assignment"))
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

        resp = _http("https://api.deepseek.com/v1/chat/completions",
                     method="POST",
                     headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                     data={
                         "model": "deepseek-chat",
                         "messages": messages,
                         "max_tokens": 600,
                         "temperature": 0.7
                     },
                     timeout=15)
        reply = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        log(f"🤖 LLM回复: {reply[:80]}")
        # 提取 [ACTION:xxx] 标记
        action = None
        m = re.search(r'\[ACTION:(.+?)\]', reply)
        if m:
            action = m.group(1).strip()
            reply = re.sub(r'\s*\[ACTION:.+?\]\s*', '', reply).strip()
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
_poll_cache = {"last_check_ts": 0, "processed_ids": set()}

def _load_poll_state():
    """从 GitHub 读取已处理的消息 ID（带内存缓存）"""
    if _poll_cache["processed_ids"]:
        return _poll_cache["processed_ids"], _poll_cache.get("last_msg_id", "")
    try:
        data, _ = gh_read_json(POLL_STATE_FILE)
        _poll_cache["processed_ids"] = set(data.get("processed_ids", []))
        _poll_cache["last_msg_id"] = data.get("last_msg_id", "")
        return _poll_cache["processed_ids"], _poll_cache["last_msg_id"]
    except Exception:
        return set(), ""

def _save_poll_state(processed_ids, last_msg_id):
    """保存已处理的消息 ID 到 GitHub"""
    _poll_cache["processed_ids"] = processed_ids
    _poll_cache["last_msg_id"] = last_msg_id
    try:
        id_list = list(processed_ids)[-100:]
        data, sha = gh_read_json(POLL_STATE_FILE)
        gh_write_json(POLL_STATE_FILE, {"processed_ids": id_list, "last_msg_id": last_msg_id},
                       sha, "poll state")
    except Exception:
        try:
            gh_write_json(POLL_STATE_FILE, {"processed_ids": list(processed_ids)[-100:], "last_msg_id": last_msg_id},
                          None, "poll state init")
        except Exception:
            pass

def poll_group_messages():
    """主动查群消息，处理用户发的新消息（带回检测：已回复过的不再重复处理）"""
    try:
        processed_ids, last_msg_id = _load_poll_state()
        token = get_feishu_token()
        url = (f"https://open.feishu.cn/open-apis/im/v1/messages"
               f"?container_id_type=chat&container_id={CHAT_ID_FALLBACK}&page_size=30")
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
            # 只处理用户发的文本消息
            if sender_type != "user" or mtype != "text":
                continue
            # 跳过已处理的
            if msg_id in processed_ids or msg_id == last_msg_id:
                continue
            # 跨路径去重：事件推送路径已处理过 → 同步到state并跳过
            if msg_id in _PROCESSED_MSG_IDS:
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
            # 提取文本
            body = m.get("body", {}).get("content", "{}")
            try:
                text = json.loads(body).get("text", "").strip()
            except Exception:
                continue
            if not text:
                continue
            # 跳过超过5分钟前的旧消息（避免处理历史消息）
            if msg_ts < time.time() - 300:
                processed_ids.add(msg_id)
                last_msg_id = msg_id
                new_processed = True
                continue
            log(f"📩 轮询收到: {text[:30]}")
            processed_ids.add(msg_id)
            last_msg_id = msg_id
            new_processed = True
            # 异步处理（不阻塞轮询返回）
            import threading
            def _process_async(t=text):
                try:
                    process_command(t, CHAT_ID_FALLBACK)
                except Exception as e:
                    log(f"❌ 处理失败: {e}")
            threading.Thread(target=_process_async, daemon=True).start()
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

    # v2: schema 2.0
    header = data.get("header", {})
    if isinstance(header, dict) and header.get("event_type") == "im.message.receive_v1":
        msg = data.get("event", {}).get("message", {})
        msg_id = msg.get("message_id", "")
        chat_id = msg.get("chat_id", CHAT_ID_FALLBACK)
        # 兼容：v2.0 schema 字段名为 message_type，v1 为 msg_type
        msg_type = msg.get("message_type", "") or msg.get("msg_type", "")
        msg_text = extract_msg_text(msg_type, msg.get("content", "{}"))

    # v1: event_callback
    elif data.get("type") == "event_callback":
        evt = data.get("event", {})
        if isinstance(evt, dict) and evt.get("type") == "im.message.receive_v1":
            msg = evt.get("message", {})
            msg_id = msg.get("message_id", "")
            chat_id = msg.get("chat_id", CHAT_ID_FALLBACK)
            msg_type = msg.get("msg_type", "")
            msg_text = extract_msg_text(msg_type, msg.get("content", "{}"))

    # 去重：飞书超时重发同一事件时跳过
    if msg_text and msg_id:
        if msg_id in _PROCESSED_MSG_IDS:
            log(f"⏭️ 跳过重复消息: {msg_id}")
            return {"statusCode": 200, "body": "ok"}
        _PROCESSED_MSG_IDS[msg_id] = True
        # 渐进淘汰：超限只丢最旧的，保留近期记录（避免全清导致重复处理）
        while len(_PROCESSED_MSG_IDS) > 300:
            _PROCESSED_MSG_IDS.popitem(last=False)

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

    return {"statusCode": 200, "body": "ok"}

    return {"statusCode": 200, "body": "ok"}
