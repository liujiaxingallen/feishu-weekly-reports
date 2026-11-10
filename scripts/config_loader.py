#!/usr/bin/env python3
"""飞书周报的配置加载器，guard.py / check.py / discover_projects.py 共用。

配置放在 ~/.claude/lark-weekly-config.json。读不到就用内置默认值，
保证脚本在没配置的机器上也不会崩，只是 open_id 为空（doctor 会提示）。
"""
import json
import os

HOME = os.path.expanduser("~")
CONFIG_PATH = os.path.join(HOME, ".claude", "lark-weekly-config.json")

DEFAULTS = {
    "user": {"open_id": "", "name": ""},
    "runtime": {
        "claude_bin": "/opt/homebrew/bin/claude",
        "python_bin": "/usr/local/bin/python3",
        "path": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    },
    "launchd": {"label": "com.lark-weekly-guard"},
    "schedule": {
        "due": "20:45",
        "checks": [
            {"weekday": 3, "time": "20:50"},
            {"weekday": 3, "time": "22:30"},
            {"weekday": 4, "time": "08:00"},
            {"weekday": 4, "time": "10:15"},
        ],
        "last_chance": {"weekday": 4, "time": "10:00"},
        "max_attempts": 4,
        "rerun_timeout": 2400,
    },
    "projects": [],
    "mail_noise_domains": [],
    "mail_work_domains": [],
}


def _merge(base: dict, override) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    """读配置，缺的字段用默认值补齐。任何异常都退回默认值，不抛。"""
    try:
        with open(CONFIG_PATH) as f:
            return _merge(DEFAULTS, json.load(f))
    except Exception:
        return dict(DEFAULTS)


def hhmm(text, default=(0, 0)):
    """'20:45' -> (20, 45)"""
    try:
        h, m = str(text).split(":")
        return int(h), int(m)
    except Exception:
        return default


def weekday_time(spec, default_weekday=3, default_time="20:50"):
    """{"weekday": 3, "time": "20:50"} -> (3, 20, 50)"""
    if not isinstance(spec, dict):
        h, m = hhmm(default_time)
        return default_weekday, h, m
    h, m = hhmm(spec.get("time", default_time))
    return int(spec.get("weekday", default_weekday)), h, m


def project_names(cfg=None):
    cfg = cfg or load()
    return [p.get("name", "") for p in cfg.get("projects", []) if p.get("name")]


def classify_group(group_name: str, cfg=None) -> str:
    """按群名精确匹配项目，命中返回项目名，否则返回空串。"""
    cfg = cfg or load()
    for p in cfg.get("projects", []):
        if group_name in (p.get("groups") or []):
            return p.get("name", "")
    return ""


def is_noise_mail(from_addr, cfg=None) -> bool:
    """发件地址命中黑名单就算噪声。"""
    cfg = cfg or load()
    addr = str(from_addr or "").lower()
    return any(d in addr for d in cfg.get("mail_noise_domains", []))


def is_work_mail(from_addr, cfg=None) -> bool:
    """发件地址命中工作域名白名单。"""
    cfg = cfg or load()
    addr = str(from_addr or "").lower()
    return any(d in addr for d in cfg.get("mail_work_domains", []))
