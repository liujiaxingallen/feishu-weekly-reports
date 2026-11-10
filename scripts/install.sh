#!/bin/bash
# 飞书周报安装向导（macOS / Linux / Windows）
#
# 做四件事：检查环境 → 自动取 openId → 扫描群归纳项目让你勾选 → 生成配置和定时任务。
# 定时任务按平台自动选：macOS 用 launchd，Linux 用 crontab，Windows 用 schtasks。
# 重复运行安全：已有配置会先备份，项目选择可以重来。
#
#     bash install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CH="$HOME/.claude"
CONFIG="$CH/lark-weekly-config.json"
PLATFORM="$(uname -s)"
LABEL="com.$(whoami | tr -cd '[:alnum:]').lark-weekly-guard"

say() { printf '%s\n' "$*"; }
die() { printf '\n错误：%s\n' "$*" >&2; exit 1; }

say "== 1. 环境检查 =="
PYTHON_BIN="$(command -v python3 || true)"
CLAUDE_BIN="$(command -v claude || true)"
LARK_BIN="$(command -v lark-cli || true)"
[ -n "$PYTHON_BIN" ] || die "找不到 python3"
[ -n "$CLAUDE_BIN" ] || die "找不到 claude CLI"
[ -n "$LARK_BIN" ]   || die "找不到 lark-cli"
say "  平台    : $PLATFORM"
say "  python3 : $PYTHON_BIN"
say "  claude  : $CLAUDE_BIN"
say "  lark-cli: $LARK_BIN"

say ""
say "== 2. 飞书授权 =="
OPEN_ID="$("$LARK_BIN" auth status 2>/dev/null | "$PYTHON_BIN" -c '
import json,sys
try:
    print(json.load(sys.stdin)["identities"]["user"].get("openId",""))
except Exception:
    print("")
' || true)"
[ -n "$OPEN_ID" ] || die "拿不到 openId。先运行：lark-cli auth login --recommend"
say "  当前用户 open_id：$OPEN_ID"

say ""
say "== 3. 项目归纳 =="
say "  正在扫描你最近的群和消息，归纳项目候选（约 2 分钟）..."
if ! "$PYTHON_BIN" "$SCRIPT_DIR/discover.py" > /tmp/lark-discover.json 2>/tmp/lark-discover.err; then
  say "  自动归纳失败，将使用配置里的现有项目。原因："
  head -5 /tmp/lark-discover.err | sed 's/^/    /'
else
  "$PYTHON_BIN" - <<'PY'
import json
d = json.load(open("/tmp/lark-discover.json"))
ps = d.get("projects", [])
print(f"\n  归纳出 {len(ps)} 个项目：\n")
for i, p in enumerate(ps, 1):
    print(f"  {i}. {p.get('name')}  [{p.get('confidence','?')}]")
    print(f"     {p.get('description','')}")
    print(f"     群：{', '.join(p.get('groups', [])[:5])}{' ...' if len(p.get('groups',[]))>5 else ''}")
print()
PY
  say "  全部保留直接回车；只想保留部分就输入编号（如 1,3,5）："
  read -r KEEP
  if [ -n "${KEEP:-}" ]; then
    "$PYTHON_BIN" - "$KEEP" <<'PY'
import json, sys
keep = {int(x) for x in sys.argv[1].replace("，", ",").split(",") if x.strip().isdigit()}
d = json.load(open("/tmp/lark-discover.json"))
d["projects"] = [p for i, p in enumerate(d.get("projects", []), 1) if i in keep]
json.dump(d, open("/tmp/lark-discover.json", "w"), ensure_ascii=False)
print(f"  保留 {len(d['projects'])} 个项目")
PY
  fi
fi

say ""
say "== 4. 写配置 =="
[ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)" && say "  已备份旧配置"
"$PYTHON_BIN" - "$OPEN_ID" "$CLAUDE_BIN" "$PYTHON_BIN" "$LABEL" <<'PY'
import json, os, sys
open_id, claude_bin, python_bin, label = sys.argv[1:5]
path = os.path.expanduser("~/.claude/lark-weekly-config.json")
cfg = json.load(open(path)) if os.path.exists(path) else {}
cfg.setdefault("user", {})["open_id"] = open_id
cfg.setdefault("runtime", {}).update({"claude_bin": claude_bin, "python_bin": python_bin})
cfg.setdefault("launchd", {})["label"] = label
cfg.setdefault("schedule", {
    "due": "20:45",
    "checks": [{"weekday": 3, "time": "20:50"}, {"weekday": 3, "time": "22:30"},
               {"weekday": 4, "time": "08:00"}, {"weekday": 4, "time": "10:15"}],
    "max_attempts": 4, "rerun_timeout": 2400,
})
if os.path.exists("/tmp/lark-discover.json"):
    try:
        d = json.load(open("/tmp/lark-discover.json"))
        if d.get("projects"):
            cfg["projects"] = d["projects"]
    except Exception:
        pass
json.dump(cfg, open(path, "w"), ensure_ascii=False, indent=2)
print(f"  已写入 {path}")
PY

say ""
say "== 5. 任务步骤文档 =="
cp "$SCRIPT_DIR/../references/lark-weekly-task.md" "$CH/lark-weekly-task.md"
say "  已复制到 $CH/lark-weekly-task.md"

say ""
say "== 6. 定时任务 =="
case "$PLATFORM" in
  Darwin) install_macos ;;
  Linux)  install_linux ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT) install_windows ;;
  *) die "不认识这个平台：$PLATFORM。手动跑 guard.py 也能用，只是没有自动定时。" ;;
esac

say ""
say "== 7. 自检 =="
"$PYTHON_BIN" "$SCRIPT_DIR/doctor.py" || true

say ""
say "完成。之后每周四自动生成周报，也可手动跑："
say "  python3 $SCRIPT_DIR/guard.py"


# ---- 平台定时器实现 ----

install_macos() {
  local PLIST_DIR="$HOME/Library/LaunchAgents"
  local PLIST="$PLIST_DIR/$LABEL.plist"
  mkdir -p "$PLIST_DIR"
  local WEEKDAY_TIMES
  WEEKDAY_TIMES="$("$PYTHON_BIN" -c '
import json, os
# 配置用 Python 约定（周一=0）；launchd 和 crontab 都是周日=0、周一=1，所以 +1。
cfg = json.load(open(os.path.expanduser("~/.claude/lark-weekly-config.json")))
for c in cfg["schedule"]["checks"]:
    h, m = c["time"].split(":")
    print(c["weekday"] + 1, int(h), int(m))
')"
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SCRIPT_DIR/guard.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(dirname "$CLAUDE_BIN"):$(dirname "$PYTHON_BIN"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
EOF
    while read -r WD HH MM; do
      cat <<EOF
    <dict><key>Weekday</key><integer>$WD</integer><key>Hour</key><integer>$HH</integer><key>Minute</key><integer>$MM</integer></dict>
EOF
    done <<< "$WEEKDAY_TIMES"
    cat <<EOF
  </array>
  <key>StandardOutPath</key>
  <string>$CH/lark-weekly-guard.log</string>
  <key>StandardErrorPath</key>
  <string>$CH/lark-weekly-guard.log</string>
</dict>
</plist>
EOF
  } > "$PLIST"
  say "  已生成 $PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  say "  已加载 launchd 任务"
}

install_linux() {
  # 生成 crontab 条目。crontab 星期约定：0/7=周日、1=周一、4=周四；Python 周四=3，+1 得 4。
  local TIMES
  TIMES="$("$PYTHON_BIN" -c '
import json, os
cfg = json.load(open(os.path.expanduser("~/.claude/lark-weekly-config.json")))
for c in cfg["schedule"]["checks"]:
    h, m = c["time"].split(":")
    dow = (c["weekday"] + 1) % 7
    print(int(m), int(h), dow)
')"
  # 去掉旧条目再追加新条目
  {
    crontab -l 2>/dev/null | grep -v "lark-weekly-guard" || true
    while read -r MM HH DOW; do
      echo "$MM $HH * * $DOW $PYTHON_BIN $SCRIPT_DIR/guard.py  # lark-weekly-guard"
    done <<< "$TIMES"
  } | crontab -
  say "  已写入 crontab："
  crontab -l | grep lark-weekly-guard | sed 's/^/    /'
}

install_windows() {
  # Windows 用任务计划。四个时刻创建四个任务。路径用 cygpath 转成 Windows 格式（Git Bash 下）。
  local PY_WIN GUARD_WIN
  if command -v cygpath >/dev/null 2>&1; then
    PY_WIN="$(cygpath -w "$PYTHON_BIN")"
    GUARD_WIN="$(cygpath -w "$SCRIPT_DIR/guard.py")"
  else
    PY_WIN="$PYTHON_BIN"
    GUARD_WIN="$SCRIPT_DIR/guard.py"
  fi
  local NAMES
  NAMES="$("$PYTHON_BIN" -c '
import json, os
cfg = json.load(open(os.path.expanduser("~/.claude/lark-weekly-config.json")))
days = ["MON","TUE","WED","THU","FRI","SAT","SUN"]
for i, c in enumerate(cfg["schedule"]["checks"], 1):
    h, m = c["time"].split(":")
    print(f"lark-weekly-guard-{i} {days[c[\"weekday\"]]} {int(h):02d}:{int(m):02d}")
')"
  while read -r NAME DAY TIME; do
    HH="${TIME%%:*}"; MM="${TIME##*:}"
    schtasks //create //f //tn "$NAME" //sc weekly //d "$DAY" //st "$HH:$MM" \
      //tr "\"$PY_WIN\" \"$GUARD_WIN\"" >/dev/null 2>&1
    say "  已创建任务 $NAME（每周$DAY $HH:$MM）"
  done <<< "$NAMES"
}