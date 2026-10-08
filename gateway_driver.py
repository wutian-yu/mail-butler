#!/usr/bin/env python3
"""AI 邮件管家 · 统一驱动器（SCF 云函数下线后的运行入口）

用法：
    python gateway_driver.py live   # 常驻模式：feishu-live 接力任务内循环轮询，每 ~50 秒一轮
    python gateway_driver.py poll   # 单轮模式：feishu-poller 每 5 分钟兜底

三项职责完整复刻原 feishu-gateway 云函数：
  1. ams_timer_poll()       —— 原 ams-poller 每分钟定时器（课节状态监控/签到提醒）
  2. poll_group_messages()  —— 原 msg 定时器（定时推送/订房队列/盯场地/财务日报/图片处理）
  3. 文本消息处理            —— 原 webhook 秒级路径（poll_group_messages 自 v39d 起只管图片，文本由此处理）

去重三层防护（沿用 v40c 方案）：
  - 进程内 seen 集合：live 模式同一进程常驻，同一条消息绝不二次评估
  - butler-data 共享已处理表：跨任务/跨路径（butler.py 兜底、poller 兜底互认）
  - is_replied 时间线判定：其他路径已回复的消息登记后跳过

分工与年龄门槛：
  live 模式 = 第一处理人，消息到达后 ~50-90 秒内处理；
  poll 模式 = 兜底，带 300 秒年龄门槛——live 存活时它永远轮不到新消息（不会抢处理、
  也消除了两端同时开处理同一消息的竞态）；live 停摆时消息在 5 分钟后自然过门槛被接管，零丢失。
"""

import json
import sys
import time
import urllib.request

import feishu_gateway_scf as gw

# 云函数时代的「Actions 保活」已无意义（原意是云函数反向触发 GitHub 兜底 cron 停摆，
# 现在两者同为 Actions，谁也保不了谁）；且每次新进程都会误判超时 → 重复 dispatch。一律屏蔽。
gw._gh_actions_keepalive = lambda *a, **k: None

CYCLE_SECONDS = 50                                  # live 轮询周期（对齐原云函数每分钟节奏）
LOOP_MAX_SECONDS = 5 * 3600 + 50 * 60               # 单任务最长 ~5h50m（平台硬限 6h，留余量）
POLL_AGE_GATE_SECONDS = 300                         # poll 模式年龄门槛


def log(msg):
    print("[" + time.strftime("%H:%M:%S") + "] " + msg, flush=True)


def run_duties():
    """云函数定时器职责一轮：AMS 课节监控 + 定时推送套件/图片处理"""
    try:
        gw.ams_timer_poll()
    except Exception as e:
        log("ams_timer_poll 异常: " + str(e))
    try:
        gw.poll_group_messages()
    except Exception as e:
        log("poll_group_messages 异常: " + str(e))


def handle_text_messages(seen, age_gate=0):
    """文本消息处理（原 webhook 路径）。
    seen：进程内已评估过的 message_id 集合；age_gate：只处理超过该秒数的消息（0 = 不限）。"""
    chat_id = gw.CHAT_ID_FALLBACK
    token = gw.get_feishu_token()
    now_s = int(time.time())
    url = ("https://open.feishu.cn/open-apis/im/v1/messages"
           "?container_id_type=chat&container_id=" + chat_id
           + "&start_time=" + str(now_s - 7200) + "&end_time=" + str(now_s + 60)
           + "&page_size=50")
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=20) as r:
            result = json.loads(r.read().decode())
        items = result.get("data", {}).get("items", [])
    except Exception as e:
        # 拉取失败（超时/400/网络抖动）绝不让异常冒泡打死常驻任务；
        # 主动作废 token 缓存，下一轮自动重新获取（自愈飞书 token 过期/失效）。
        gw.invalidate_feishu_token()
        log("拉取消息列表异常(已隔离): " + str(e))
        return 0, 0, 0, 0

    # 时间线 + 已回复判定：bot 在该用户消息后、下一条用户消息前有发言 → 视为已回复
    timeline = [(int(m.get("create_time", "0")),
                  m.get("sender", {}).get("sender_type", "")) for m in items]

    def is_replied(msg_ts_ms):
        next_user_ts = None
        for ts, st in timeline:
            if st == "user" and ts > msg_ts_ms:
                next_user_ts = ts
                break
        for ts, st in timeline:
            if st == "app" and ts > msg_ts_ms and (next_user_ts is None or ts < next_user_ts):
                return True
        return False

    handled = replied = shared = young = 0
    for m in sorted(items, key=lambda x: int(x.get("create_time", "0"))):
        if m.get("sender", {}).get("sender_type") != "user":
            continue
        if m.get("msg_type") != "text":
            continue
        msg_id = m.get("message_id", "")
        if not msg_id or msg_id in seen:
            continue
        try:
            text = json.loads(m.get("body", {}).get("content", "{}")).get("text", "").strip()
        except Exception:
            seen.add(msg_id)
            continue
        if not text:
            seen.add(msg_id)
            continue
        msg_ts_ms = int(m.get("create_time", "0"))
        if age_gate and (now_s * 1000 - msg_ts_ms) < age_gate * 1000:
            # 年龄未到：live 存活时归 live 处理；live 停摆时下轮 poller 过门槛自然接管
            young += 1
            continue
        seen.add(msg_id)  # 确定处置后，本进程内不再重复评估
        if gw._is_msg_processed_shared(msg_id):
            shared += 1
            continue
        if is_replied(msg_ts_ms):
            gw._mark_msg_processed_shared(msg_id)
            replied += 1
            log("skip (replied): " + text[:40])
            continue
        log("handle: " + text[:40])
        try:
            gw.process_command(text, chat_id)
        except Exception as e:
            log("process_command 异常: " + str(e))
            try:
                gw.feishu_send(chat_id, "⚠️ 处理失败：" + str(e).splitlines()[0][:60] + "\n\n请重试。")
            except Exception:
                pass
        finally:
            gw._mark_msg_processed_shared(msg_id)
        handled += 1
    return handled, replied, shared, young


def mode_live():
    log("live 模式启动：周期 " + str(CYCLE_SECONDS) + "s，最长运行 "
        + str(LOOP_MAX_SECONDS // 60) + " 分钟（之后由下一个锚点接力）")
    seen = set()
    end_at = time.time() + LOOP_MAX_SECONDS
    cycle = 0
    while time.time() < end_at:
        cycle += 1
        started = time.time()
        try:
            run_duties()
            handled, replied, shared, young = handle_text_messages(seen, age_gate=0)
        except Exception as e:
            # 兜底隔离：任何未预期的异常都不能打死常驻循环，记录后继续下一轮。
            handled = replied = shared = young = 0
            log("cycle " + str(cycle) + " 异常(已隔离，继续运行): " + str(e))
        if handled or replied or shared:
            log("cycle " + str(cycle) + ": 处理=" + str(handled)
                + " 已回复跳过=" + str(replied) + " 共享表跳过=" + str(shared))
        remain = CYCLE_SECONDS - (time.time() - started)
        left = end_at - time.time()
        if remain > 0 and left > 0:
            time.sleep(min(remain, left))   # 收尾阶段只睡到截止时刻
    log("live 循环结束（共 " + str(cycle) + " 轮）")


def mode_poll():
    seen = set()
    run_duties()
    handled, replied, shared, young = handle_text_messages(seen, age_gate=POLL_AGE_GATE_SECONDS)
    log("poll 单轮: 处理=" + str(handled) + " 已回复跳过=" + str(replied)
        + " 共享表跳过=" + str(shared) + " 未到年龄门槛=" + str(young))


if __name__ == "__main__":
    _mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if _mode == "live":
        mode_live()
    elif _mode == "poll":
        mode_poll()
    else:
        print("用法: gateway_driver.py [live|poll]")
        sys.exit(2)
