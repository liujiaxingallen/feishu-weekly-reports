#!/usr/bin/env python3
"""飞书周报定时任务的会话启动兜底检查。

检查四类情况，命中就输出提醒（stdout 注入会话上下文）：
  1. auth_expired：user 授权已失效——三源全废，有任务也白搭，**不看 has_task 直接提醒**；
  2. auth_expiring：user 授权剩余 < 7 天，提前催续期（同样不看 has_task）；
  3. due：上次周报早于最近一个应跑完的周四（或从未生成）；
  4. incomplete：上次执行日志缺 [done]（执行中断，可能状态已更新但续期没做）。

3、4 两条仍需 `not has_task` 才提醒（有活跃任务时自然会跑，不必打扰）。

其余情况静默退出。所有判断都 append 到 check 日志，便于验证 hook 是否触发。

认证检查是 2026-09-27 补的：那次 refresh 在 09-25 08:41 才到期、任务 04:39 就触发了，
旧版只看「是否已过期」放行，整轮静默哑掉零告警，用户两天后才发现。
"""
import datetime
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load as load_config, hhmm  # noqa: E402

CFG = load_config()
STATE = os.path.join(HOME, ".claude/lark-weekly-state.json")
TASKS = os.path.join(HOME, ".claude/scheduled_tasks.json")
LOG = os.path.join(HOME, ".claude/lark-weekly-check.log")
REPORT_LOG = os.path.join(HOME, ".claude/lark-weekly-report.log")
SEVEN_DAYS_MS = 7 * 24 * 3600 * 1000
USER_OPEN_ID = CFG["user"]["open_id"]
DUE_HOUR, DUE_MINUTE = hhmm(CFG["schedule"]["due"], (20, 45))
# refresh token 有效期本身就是 7 天（每次用都会滚动顺延），所以阈值不能设 7——
# 那样几乎每次开会话都会误报「快过期」。设 2 天：真剩这么点，说明 5 天以上没跑过，
# 才值得打断用户。
AUTH_WARN_DAYS = 2


def log(reason: str, has_task: bool, auth: str) -> None:
    """append-only 执行日志，用于验证 SessionStart hook 是否真的触发。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG, "a") as f:
            f.write(f"{ts}  reason={reason} has_task={has_task} auth={auth}\n")
    except Exception:
        pass


def auth_health():
    """查 user 授权剩余天数。返回 (状态, 说明)，状态取 ok/expiring/expired/unknown。

    unknown 表示查不动（lark-cli 不在 PATH、超时、输出不是 JSON），此时不打扰用户。
    """
    try:
        proc = subprocess.run(
            ["lark-cli", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(proc.stdout)
    except Exception:
        return "unknown", "auth status 查询失败"

    user = (data.get("identities") or {}).get("user") or {}
    exp_raw = user.get("refreshExpiresAt") or ""
    if not exp_raw:
        return "expired", "无 refreshExpiresAt"

    # 只看 refresh token 的到期时间，不拿 status 说事：
    # status 描述的是 access token（几小时就过期，隔天看就是 needs_refresh），
    # 那个状态 lark-cli 会自动刷新并把 refresh 顺延 7 天，不代表授权坏了。
    # 2026-09-30 实测：status=needs_refresh 时跑 user 命令自动刷新成功，
    # refreshExpiresAt 从 10-04 顺延到 10-07。只有 refresh 真的过期才是失效。
    try:
        exp = datetime.datetime.fromisoformat(exp_raw)
    except Exception:
        return "unknown", "refreshExpiresAt 解析失败"

    remain = (exp - datetime.datetime.now(exp.tzinfo)).total_seconds() / 86400
    if remain <= 0:
        return "expired", exp_raw
    if remain < AUTH_WARN_DAYS:
        return "expiring", f"{remain:.1f} 天（{exp_raw}）"
    return "ok", exp_raw


def last_due_date(now=None):
    """最近一个「应已跑完」的触发日（date）。

    weekday 和分界时刻都从配置读（`schedule.checks[0]` 和 `schedule.due`），
    今天正好是触发日但还没到分界时刻时，回退到上一周。
    """
    now = now or datetime.datetime.now()
    wd = int(CFG["schedule"]["checks"][0].get("weekday", 3))
    candidate = now.date() - datetime.timedelta(days=(now.weekday() - wd) % 7)
    if candidate == now.date() and (now.hour, now.minute) < (DUE_HOUR, DUE_MINUTE):
        candidate -= datetime.timedelta(days=7)
    return candidate


def state_due() -> bool:
    """上次周报是否早于最近一个应跑完的周四（或从未生成过）。

    旧版判的是「距上次 >= 7 天」，有个洞：漏跑之后接下来几天距上次还不到 7 天，
    兜底会一直沉默，要等到第 8 天才提醒，那一期等于白丢。改成跟触发日对齐，
    漏跑的第二天开会话就能被发现。
    """
    if not os.path.exists(STATE):
        return True
    try:
        s = json.load(open(STATE))
        last = s.get("last_report_end")
        if not last:
            return True
        return datetime.datetime.strptime(last, "%Y-%m-%d").date() < last_due_date()
    except Exception:
        return True


def last_run_incomplete() -> bool:
    """上次周报执行是否中断：最后一次 [start] 之后没有对应的 [done]。"""
    if not os.path.exists(REPORT_LOG):
        return False
    try:
        lines = open(REPORT_LOG).read().strip().split("\n")
        last_start = -1
        last_done = -1
        for i, line in enumerate(lines):
            if "[start]" in line:
                last_start = i
            if "[done]" in line:
                last_done = i
        if last_start == -1:
            return False
        return last_done < last_start
    except Exception:
        return False


def has_active_scheduler() -> bool:
    """调度器是否健康。

    2026-10-26 改：唯一触发源已经是 launchd 守卫，不再看 cron。
    """
    try:
        label = CFG["launchd"]["label"]
        p = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        return label in p.stdout
    except Exception:
        return False


if __name__ == "__main__":
    auth_state, auth_note = auth_health()
    due = state_due()
    incomplete = last_run_incomplete()
    has_task = has_active_scheduler()

    if auth_state == "expired":
        reason = "auth_expired"
    elif auth_state == "expiring":
        reason = "auth_expiring"
    elif due:
        reason = "due"
    elif incomplete:
        reason = "incomplete"
    else:
        reason = "ok"
    log(reason, has_task, auth_state)

    msgs = []
    if auth_state == "expired":
        msgs.append(
            f"user 授权已失效（refresh 过期于 {auth_note or '未知时间'}）。"
            "群聊/邮件/文档三源都拉不到数据，定时任务也救不了，"
            "请跑 `lark-cli auth login --recommend` 重新授权，再补跑落下的周报。"
        )
    elif auth_state == "expiring":
        msgs.append(
            f"user 授权剩余 {auth_note}，过期后周报会整体哑掉。"
            "趁现在还能用，跑 `lark-cli auth login --recommend` 续上。"
        )

    if (due or incomplete) and not has_task:
        what = "周报已到期（早于最近一个应跑完的触发日）" if due else "上次周报执行未完成（日志缺 [done]）"
        msgs.append(
            f"检测到 {what}，且调度器不可用（launchd 守卫没加载）。"
            "请读取 ~/.claude/lark-weekly-task.md 生成周报，然后重新加载守卫："
            "`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`。"
        )

    if msgs:
        print("[飞书周报] " + " ".join(msgs))
