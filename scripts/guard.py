#!/usr/bin/env python3
"""飞书周报的会话外守卫：先补跑，补不动才告警。

为什么需要（2026-10-10 加，2026-10-26 更新为当前架构）：
    定时任务依赖 Claude Code 进程处于活跃状态，用户没开就完全不触发，而且
    失败时日志和告警一起空白。所以用 launchd 驱动这个守卫，它不依赖 Claude
    会话，机器开着就能跑。

    guard 在配置的四个时刻跑（默认周四 20:50 / 22:30、周五 08:00 / 10:15）：
      - 本期没跑成 → 先探测网关，再用 `claude -p` 补跑一次，成功就静默
      - 补跑机会用完、或 Claude 本身不可用 → 用 bot 发告警（bot 不依赖 user token）

判断逻辑：
    本期算成功，当且仅当 state 的 last_report_end >= 最近一个「应已跑完」的
    触发日。跑成功才会更新 state。

配置见 ~/.claude/lark-weekly-config.json（openId、触发时刻、补跑次数、项目规则）。

⚠️ 拿本脚本做测试时注意，2026-10-10 两个坑都踩过：
    1. **别用真实 `claude -p` 去验证 `try_rerun`**，会套娃，详见该函数注释。
    2. **别让测试写到真实状态文件**。测试前把 `m.STATE` / `m.GUARD_STATE`
       monkeypatch 到 /tmp，否则会把 `lark-weekly-state.json` 写成伪造值，
       下期守卫据此判定就会乱补跑、乱告警。
"""
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load as load_config, hhmm  # noqa: E402

CFG = load_config()

STATE = os.path.join(HOME, ".claude/lark-weekly-state.json")
GUARD_STATE = os.path.join(HOME, ".claude/lark-weekly-guard.state")
REPORT_LOG = os.path.join(HOME, ".claude/lark-weekly-report.log")
LOG = os.path.join(HOME, ".claude/lark-weekly-guard.log")
TASKS = os.path.join(HOME, ".claude/scheduled_tasks.json")

CLAUDE_BIN = CFG["runtime"]["claude_bin"]
USER_OPEN_ID = CFG["user"]["open_id"]
CLAUDE_PATH = CFG["runtime"]["path"]

# 主触发时刻（配置 schedule.due）决定「今天这一期是否已到点」的分界。
DUE_HOUR, DUE_MINUTE = hhmm(CFG["schedule"]["due"], (20, 45))
# [start] 之后 3 小时内还没 [done]，视为正在跑，别误报也别抢跑。
IN_PROGRESS_GRACE = datetime.timedelta(hours=3)
MAX_ATTEMPTS = int(CFG["schedule"]["max_attempts"])
RERUN_TIMEOUT = int(CFG["schedule"]["rerun_timeout"])
RERUN_PROMPT = (
    "读取 ~/.claude/lark-weekly-task.md，严格执行其中的「飞书周报自动生成任务」全流程"
    "（认证预检+失败时 bot 告警 → 群聊+邮件+文档 → 归类 → 生成 markdown → 创建飞书文档"
    " → 机器人发消息 → 更新状态文件 → CronList 查重后 CronCreate 续期）。"
    "全程自动，不要中途问用户确认。"
)


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG, "a") as f:
            f.write(f"{ts}  {msg}\n")
    except Exception:
        pass


def last_due_date(now=None) -> datetime.date:
    """最近一个「应已跑完」的触发日。

    weekday 和分界时刻都来自配置（`schedule.checks[0]` 和 `schedule.due`），
    换星期、换时刻都不用改代码。
    """
    now = now or datetime.datetime.now()
    wd = int(CFG["schedule"]["checks"][0].get("weekday", 3))
    candidate = now.date() - datetime.timedelta(days=(now.weekday() - wd) % 7)
    if candidate == now.date() and (now.hour, now.minute) < (DUE_HOUR, DUE_MINUTE):
        candidate -= datetime.timedelta(days=7)
    return candidate


def last_report_end():
    if not os.path.exists(STATE):
        return None
    try:
        raw = json.load(open(STATE)).get("last_report_end")
        return datetime.datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
    except Exception:
        return None


def run_in_progress() -> bool:
    """最后一次 [start] 之后没有 [done]，且开始时间还很近 → 正在跑。"""
    if not os.path.exists(REPORT_LOG):
        return False
    try:
        last_start = last_done = None
        pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(start|done)\]")
        for line in open(REPORT_LOG):
            m = pat.match(line)
            if m:
                ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                if m.group(2) == "start":
                    last_start = ts
                else:
                    last_done = ts
        if last_start is None:
            return False
        if last_done is not None and last_done >= last_start:
            return False
        return datetime.datetime.now() - last_start < IN_PROGRESS_GRACE
    except Exception:
        return False


def auth_expired() -> str:
    """user 授权是否已失效。失效时返回说明文字，否则返回空串。"""
    try:
        proc = subprocess.run(
            ["lark-cli", "auth", "status"],
            capture_output=True, text=True, timeout=15,
        )
        user = (json.loads(proc.stdout).get("identities") or {}).get("user") or {}
        exp_raw = user.get("refreshExpiresAt") or ""
        if not exp_raw:
            return "user 授权状态读不到 refreshExpiresAt"
        exp = datetime.datetime.fromisoformat(exp_raw)
        if exp <= datetime.datetime.now(exp.tzinfo):
            return f"user 授权已于 {exp_raw} 过期"
    except Exception:
        return ""  # 查不动就不当失效处理，交给 due 判断
    return ""


def gateway_ok() -> bool:
    """探测 Anthropic 网关是否可达。

    2026-10-15 加的：那天晚上 21:45 的 cron 和 22:31 的补跑，都在 `ENOTFOUND`
    上耗了 16~36 分钟才失败。原因是 `litellm.hexa.team` 解析到 10.x 内网地址，
    要靠内网/VPN 才通，真断了就是纯等。先花 8 秒探一下，不通就别启动 claude，
    省掉那段空等。
    """
    try:
        s = json.load(open(os.path.join(HOME, ".claude/settings.json")))
        host = urlparse(s.get("env", {}).get("ANTHROPIC_BASE_URL", "")).hostname
        if not host:
            return True  # 读不到配置就当可达，别阻断正常流程
        socket.create_connection((host, 443), timeout=8).close()
        return True
    except Exception:
        return False


def claude_ready() -> tuple[bool, str]:
    """探测 claude CLI 是否真的能跑：存在 + 认证有效。

    2026-10-26 加。本机没有独立的登录文件，认证走 settings.json 里的
    `ANTHROPIC_AUTH_TOKEN` 加自建网关，所以「没登录」和「token 失效」是同一件事。
    gateway_ok() 只能证明网络通，证明不了 token 还能用，这里用一次极短的
    headless 调用来判断。

    返回 (是否可用, 原因)。子进程环境仍用白名单，不能继承 `CLAUDE_CODE_*`，
    否则又会 resume 父转录套娃（见 try_rerun 的注释）。
    """
    if not os.path.exists(CLAUDE_BIN):
        return False, f"claude CLI 不存在（{CLAUDE_BIN}）"
    env = {
        "PATH": CLAUDE_PATH,
        "HOME": HOME,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LARK_GUARD_DEPTH": "1",
    }
    try:
        p = subprocess.run(
            [CLAUDE_BIN, "-p", "只回复 OK", "--output-format", "text"],
            capture_output=True, text=True, timeout=90, env=env, cwd=HOME,
        )
        if p.returncode == 0 and p.stdout.strip():
            return True, "ok"
        detail = (p.stderr or p.stdout or "").strip()[:200]
        return False, detail or f"exit={p.returncode}"
    except subprocess.TimeoutExpired:
        return False, "claude -p 超时（90s）"
    except Exception as e:
        return False, repr(e)


def scheduler_health() -> str:
    """调度器状态，纯信息，用于告警正文。

    2026-10-26 改：唯一触发源已经是 launchd 守卫，不再看 cron。
    """
    try:
        p = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
        for line in p.stdout.splitlines():
            if "lark-weekly-guard" in line:
                code = line.split()[1] if len(line.split()) > 1 else "?"
                return f"launchd 守卫已加载（上次退出码 {code}）"
        return "**launchd 守卫未加载**，触发源缺失，请执行 `launchctl bootstrap` 重新加载 plist"
    except Exception as e:
        return f"调度器状态读不到：{e}"


def load_state() -> dict:
    try:
        return json.load(open(GUARD_STATE))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        with open(GUARD_STATE, "w") as f:
            json.dump(st, f)
    except Exception:
        pass


def try_rerun() -> bool:
    """用 claude -p 补跑周报。返回补跑后 state 是否已跟上。

    ⚠️ 两个坑，2026-10-10 都实测踩过：

    1. **绝不传 `dict(os.environ)`。** Claude Code 的会话变量（`CLAUDE_CODE_*` 等）
       会让子会话 resume 到父转录；父转录里恰好有「跑测试脚本触发补跑」的记录，
       于是子会话又去跑它，再拉起下一代，每代翻倍。当天涨到 21 个
       `claude -p` 嵌 `zsh` 嵌 `claude -p` 的进程才被杀掉。这里只给白名单，
       外加 `LARK_GUARD_DEPTH=1` 当递归闸。
    2. **别拿真实 `claude -p` 去验证本函数。** 要测就 stub 掉 `try_rerun`
       本身，或者只验证 `last_report_end()` 的判定。
    """
    depth = int(os.environ.get("LARK_GUARD_DEPTH", "0") or 0)
    if depth > 0:
        log(f"recursion_blocked depth={depth}")
        return False

    # 仅供测试后门，生产环境不要设。
    prompt = os.environ.get("LARK_GUARD_TEST_PROMPT") or RERUN_PROMPT
    env = {
        "PATH": CLAUDE_PATH,
        "HOME": HOME,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LARK_GUARD_DEPTH": "1",
    }
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=RERUN_TIMEOUT,
            env=env, cwd=HOME,
        )
        tail = (proc.stdout or "").strip()[-300:]
        log(f"rerun_finished exit={proc.returncode} out={tail!r}")
    except subprocess.TimeoutExpired:
        log(f"rerun_timeout after {RERUN_TIMEOUT}s")
    except Exception as e:
        log(f"rerun_crash {e!r}")
    end = last_report_end()
    return end is not None and end >= last_due_date()


def send_bot_alert(text: str) -> str:
    """用 bot 身份发消息。bot 不依赖 user token，是唯一跑得通的告警通道。"""
    try:
        proc = subprocess.run(
            ["lark-cli", "im", "+messages-send", "--as", "bot",
             "--user-id", USER_OPEN_ID, "--markdown", text],
            capture_output=True, text=True, timeout=30,
        )
        out = proc.stdout.strip()
        try:
            data = json.loads(out)
            if data.get("ok"):
                return f"ok message_id={data.get('data', {}).get('message_id')}"
            return f"failed: {out[:200]}"
        except Exception:
            return f"failed: {out[:200] or proc.stderr.strip()[:200]}"
    except Exception as e:
        return f"failed: {e}"


def main() -> int:
    # 递归闸：本脚本被补跑会话再次跑到时（环境里带着 LARK_GUARD_DEPTH），
    # 立刻退出。原因见 try_rerun 的注释，2026-10-10 的套娃事故。
    if int(os.environ.get("LARK_GUARD_DEPTH", "0") or 0) > 0:
        log("recursion_blocked_in_main")
        return 0

    due = last_due_date()
    end = last_report_end()

    if end is not None and end >= due:
        st = load_state()
        if st:
            save_state({})  # 本期已成功，清掉尝试计数
        log(f"ok due={due} last_report_end={end}")
        return 0

    if run_in_progress():
        log(f"in_progress due={due} last_report_end={end}")
        return 0

    st = load_state()
    if st.get("period") != due.isoformat():
        st = {"period": due.isoformat(), "attempts": 0, "alerted": False, "checks": 0}
    attempts, alerted = st.get("attempts", 0), st.get("alerted", False)
    checks = st.get("checks", 0) + 1
    st["checks"] = checks
    save_state(st)

    block_reason = ""
    if attempts < MAX_ATTEMPTS:
        # 先探网关。不通就别启动 claude，省掉 `ENOTFOUND` 那 16~36 分钟的空等。
        if not gateway_ok():
            log(f"network_down due={due} 网关不可达，跳过本次补跑")
            if checks < 2:
                return 0  # 第一次检查就不通，先不打扰，还有下次机会
            # 第二次检查仍不通，落到告警分支，提醒用户连内网
        else:
            # 网络通了，再确认 claude 本身能不能用（token 失效 / CLI 坏了）。
            ready, why = claude_ready()
            if not ready:
                # 认证类问题重试不会自己好，必须让人处理，所以不等机会用完。
                block_reason = f"Claude 不可用：{why}"
                log(f"claude_unavailable due={due} {why}")
            else:
                st["attempts"] = attempts + 1
                save_state(st)
                log(f"rerun_start due={due} attempt={attempts + 1}/{MAX_ATTEMPTS} last_report_end={end}")
                if try_rerun():
                    save_state({})
                    log(f"rerun_ok due={due} 周报已由补跑产出")
                    return 0
                log(f"rerun_failed due={due} attempt={attempts + 1}/{MAX_ATTEMPTS}")
                if checks < 2:
                    return 0  # 第一次补跑失败，先不打扰
                # 第二次检查仍失败，落到告警分支

    if alerted:
        log(f"already_alerted due={due}")
        return 0

    auth_note = auth_expired()
    net_down = not gateway_ok()
    if block_reason:
        headline = "⚠️ **飞书周报还没生成：Claude 不可用**"
        tail = (
            f"{block_reason}\n\n"
            "**请上线 Claude 或任意助手手动触发：**\n"
            "1. 更新 `~/.claude/settings.json` 里的 `ANTHROPIC_AUTH_TOKEN`，或用 `claude auth` 重新登录\n"
            "2. 恢复后读取 `~/.claude/lark-weekly-task.md` 触发周报\n"
            "3. 认证问题重试不会自己好，所以这次提前通知你"
        )
    elif net_down:
        headline = "⚠️ **飞书周报还没生成：内网网关连不上**"
        tail = (
            "连续两次检查都连不上网关（`litellm.hexa.team` 解析到内网地址，断网或没连内网/VPN 时就会这样）。\n"
            "**请连上内网/VPN 后，上线 Claude 或任意助手，读取 `~/.claude/lark-weekly-task.md` 手动触发周报。**\n"
            "自动补跑后续还会再试，如果那时网络恢复了会自动补上，你可以先观察、不用急。"
        )
    else:
        headline = "⚠️ **飞书周报还没生成，自动补跑也失败了**"
        tail = (
            "已尝试自动补跑但没成功，需要人工介入。\n"
            "**请上线 Claude 或任意助手，读取 `~/.claude/lark-weekly-task.md` 手动触发周报。**"
        )
    reason = auth_note if auth_note else f"state 停在 {end}，应已覆盖到 {due}"
    text = (
        f"{headline}\n\n"
        f"本期应完成日期：{due}\n"
        f"原因线索：{reason}\n"
        f"调度器：{scheduler_health()}\n\n"
        f"{tail}\n"
        f"排查：`~/.claude/lark-weekly-guard.log`、`~/.claude/lark-weekly-report.log`\n"
        "手动补跑：读取 `~/.claude/lark-weekly-task.md` 执行全流程"
    )
    result = send_bot_alert(text)
    log(f"alert_sent due={due} last_report_end={end} reason={block_reason or '-'} auth={auth_note or '-'} result={result}")
    if result.startswith("ok"):
        st["alerted"] = True
        save_state(st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"crash: {e!r}")
        sys.exit(0)  # 不让 launchd 记为失败而反复重试
