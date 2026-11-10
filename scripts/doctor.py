#!/usr/bin/env python3
"""飞书周报的一键自检：装完跑一次，坏了也跑一次。

逐项检查并给出修复命令。退出码 0 表示全绿，1 表示有红灯。

    python3 doctor.py            # 全量检查
    python3 doctor.py --quick    # 跳过要调 API 的检查（claude / 网关）
"""
import argparse
import datetime
import importlib.util
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import CONFIG_PATH, load as load_config  # noqa: E402

CFG = load_config()

STATE = os.path.join(HOME, ".claude/lark-weekly-state.json")
GUARD_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard.py")
TASK_MD = os.path.join(HOME, ".claude/lark-weekly-task.md")
SCHED_NAME = "lark-weekly-guard"

results = []


def check(name, ok, detail="", fix=""):
    results.append((name, ok, detail, fix))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"：{detail}" if detail else ""))
    if not ok and fix:
        print(f"      → {fix}")


def _load_guard():
    spec = importlib.util.spec_from_file_location("guard", GUARD_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def scheduler_status():
    """跨平台检测定时器是否已配置。返回 (是否ok, 描述, 修复命令或空串)。"""
    import platform
    sysname = platform.system()
    if sysname == "Darwin":
        plist = os.path.join(HOME, "Library/LaunchAgents", CFG["launchd"]["label"] + ".plist")
        if not os.path.exists(plist):
            return False, "plist 不存在", f"bash {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'install.sh')}"
        try:
            out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
            ok = CFG["launchd"]["label"] in out
            return ok, ("已加载" if ok else "plist 存在但未加载"), f"launchctl bootstrap gui/$(id -u) {plist}"
        except Exception as e:
            return False, repr(e), "手动检查 launchctl list"
    elif sysname == "Linux":
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
            ok = SCHED_NAME + ".py" in out
            return ok, ("crontab 已配置" if ok else "crontab 未配置"), "bash scripts/install.sh"
        except Exception as e:
            return False, repr(e), "手动检查 crontab -l"
    elif sysname == "Windows":
        try:
            out = subprocess.run(["schtasks", "/query", "/tn", SCHED_NAME], capture_output=True, text=True, timeout=15).stdout
            ok = "ERROR" not in out and out.strip()
            return ok, ("任务计划已配置" if ok else "任务计划未配置"), "bash scripts/install.sh"
        except Exception as e:
            return False, repr(e), "手动检查 schtasks /query"
    return False, f"未知平台 {sysname}", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    print("飞书周报自检\n")

    # 1. 配置
    cfg_ok = os.path.exists(CONFIG_PATH)
    open_id = CFG["user"].get("open_id", "")
    check("配置文件", cfg_ok, CONFIG_PATH if cfg_ok else "不存在", "跑 install.sh 生成")
    check("用户 openId", bool(open_id), open_id[:14] + "..." if open_id else "为空",
          "在 config.json 里填 user.open_id，或重新跑 install.sh")

    # 2. 必需文件
    for label, path in [("执行步骤 task.md", TASK_MD), ("守卫脚本 guard.py", GUARD_PY)]:
        check(label, os.path.exists(path), path if os.path.exists(path) else "缺失",
              "重新跑 install.sh")

    # 3. 定时器（跨平台检测）
    ok, detail, fix = scheduler_status()
    check("定时器", ok, detail, fix or "重新跑 install.sh")

    # 4. lark-cli 授权
    try:
        p = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True, timeout=15)
        user = (json.loads(p.stdout).get("identities") or {}).get("user") or {}
        exp_raw = user.get("refreshExpiresAt", "")
        if exp_raw:
            exp = datetime.datetime.fromisoformat(exp_raw)
            days = (exp - datetime.datetime.now(exp.tzinfo)).total_seconds() / 86400
            check("飞书授权", days > 0, f"refresh 剩余 {days:.1f} 天（{exp_raw}）",
                  "跑 `lark-cli auth login --recommend` 重新授权")
        else:
            check("飞书授权", False, "读不到 refreshExpiresAt", "跑 `lark-cli auth login --recommend`")
    except Exception as e:
        check("飞书授权", False, repr(e), "确认 lark-cli 已安装并在 PATH 里")

    # 5. 状态与上次周报
    try:
        last = json.load(open(STATE)).get("last_report_end")
        check("周报状态", bool(last), f"last_report_end={last}", "手动跑一次周报")
    except Exception:
        check("周报状态", False, "state 文件不存在或读不出", "手动跑一次周报")

    # 6. 要调 API 的检查
    if not args.quick and os.path.exists(GUARD_PY):
        try:
            g = _load_guard()
            check("内网网关", g.gateway_ok(), "可达" if g.gateway_ok() else "不可达",
                  "连上内网/VPN 后重试")
            ok, why = g.claude_ready()
            check("Claude 可用", ok, why, "更新 settings.json 的 ANTHROPIC_AUTH_TOKEN，或 claude auth 登录")
        except Exception as e:
            check("Claude 可用", False, repr(e), "手动跑 `claude -p '只回复 OK'` 看报错")

    # 7. 项目配置
    projects = CFG.get("projects", [])
    check("项目归类配置", bool(projects), f"{len(projects)} 个项目" + ("" if projects else "（周报会缺少项目分组）"),
          "跑 `python3 discover.py` 生成候选，再写进 config.json")

    failed = [r for r in results if not r[1]]
    print()
    if failed:
        print(f"{len(failed)} 项需要处理：")
        for name, _, detail, fix in failed:
            print(f"  - {name}：{detail}" + (f"（{fix}）" if fix else ""))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
