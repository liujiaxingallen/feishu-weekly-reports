#!/usr/bin/env python3
"""扫描飞书群和近期消息，归纳项目候选，输出 JSON 供用户勾选。

产品化的关键一步（2026-10-26）：项目归类不该是写死的私有知识。装的时候让脚本
自己看一遍这个人的群名和近期发言，归纳出候选项目，用户确认后写进
`lark-weekly-config.json` 的 `projects` 字段。

用法：
    python3 discover.py            # 输出候选 JSON
    python3 discover.py --days 30  # 看更长时间范围
    python3 discover.py --raw      # 只输出扫描到的群和样本，不调 claude

依赖：lark-cli 已用 user 身份授权；claude CLI 可用（`--raw` 模式不需要）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load as load_config  # noqa: E402

CFG = load_config()
CLAUDE_BIN = CFG["runtime"]["claude_bin"]
CLAUDE_PATH = CFG["runtime"]["path"]

# 群太多时只取最活跃的一批，避免 prompt 过长。
MAX_GROUPS = 60
SAMPLE_PER_GROUP = 2
SAMPLE_CHARS = 90


def sh(cmd, timeout=120):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout, p.stderr, p.returncode


def fetch_chats():
    out, err, rc = sh(["lark-cli", "im", "+chat-list", "--page-all", "--page-size", "100", "--format", "json"], 180)
    try:
        data = json.loads(out)["data"]["chats"]
    except Exception:
        raise SystemExit(f"拉群列表失败（exit={rc}）：{err[:300]}")
    return {c["chat_id"]: c.get("name") or "(无名)" for c in data}


def fetch_messages(days):
    import datetime
    tz = datetime.timezone(datetime.timedelta(hours=8))
    end = datetime.datetime.now(tz)
    start = end - datetime.timedelta(days=days)
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
    out, err, rc = sh([
        "lark-cli", "im", "+messages-search",
        "--start", start.strftime("%Y-%m-%dT00:00:00+08:00"),
        "--end", end.strftime("%Y-%m-%dT23:59:59+08:00"),
        "--page-all", "--page-size", "50", "--format", "json",
    ], 300)
    try:
        msgs = json.loads(out)["data"]["messages"]
    except Exception:
        raise SystemExit(f"拉消息失败（exit={rc}）：{err[:300]}")
    try:
        os.unlink(tmp)
    except Exception:
        pass
    return msgs


def build_samples(chats, msgs):
    """按会话聚合，取最活跃的 MAX_GROUPS 个，每个会话留几条样本。"""
    by_chat = {}
    for m in msgs:
        if m.get("sender", {}).get("id_type") == "app_id":
            continue
        if m.get("chat_type") == "p2p":
            continue  # 项目发现只看群，单聊不参与归纳
        by_chat.setdefault(m["chat_id"], []).append(m)
    ranked = sorted(by_chat.items(), key=lambda kv: -len(kv[1]))[:MAX_GROUPS]

    samples = []
    for cid, items in ranked:
        name = chats.get(cid, "(未知群)")
        texts = []
        for m in items[-SAMPLE_PER_GROUP:]:
            t = re.sub(r"<[^>]+>", " ", str(m.get("content", "")))
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                texts.append(t[:SAMPLE_CHARS])
        samples.append({"chat_id": cid, "name": name, "count": len(items), "samples": texts})
    return samples


def call_claude(prompt, timeout=240):
    env = {
        "PATH": CLAUDE_PATH,
        "HOME": HOME,
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LARK_GUARD_DEPTH": "1",  # 防套娃，同 guard.py
    }
    p = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--output-format", "text"],
        capture_output=True, text=True, timeout=timeout, env=env, cwd=HOME,
    )
    if p.returncode != 0:
        raise SystemExit(f"claude 调用失败：{(p.stderr or p.stdout)[:300]}")
    return p.stdout


def parse_json(text):
    """从模型输出里抠出 JSON，容忍 ```json 代码块和前后废话。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        i, j = text.find("{"), text.rfind("}")
        raw = text[i:j + 1] if i >= 0 and j > i else ""
    return json.loads(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--raw", action="store_true", help="只输出扫描结果，不调 claude")
    args = ap.parse_args()

    chats = fetch_chats()
    msgs = fetch_messages(args.days)
    samples = build_samples(chats, msgs)

    if args.raw:
        print(json.dumps({"groups": samples}, ensure_ascii=False, indent=2))
        return

    listing = "\n".join(
        f"- {s['name']}（{s['count']} 条）" + (" | 例：" + " / ".join(s["samples"]) if s["samples"] else "")
        for s in samples
    )
    prompt = f"""你在帮我归纳飞书里的项目。

下面是最近 {args.days} 天最活跃的群和发言样本：

{listing}

请把它们归纳成 3-8 个项目。一个项目就是一组围绕同一目标的群（例如同一个产品、同一条业务线、同一类事务）。
要求：
- groups 必须从上面的群名里原样复制，不要编造
- keywords 给 3-8 个能判断一条消息属于该项目的词
- confidence 按把握程度给 high / medium / low
- 归不进去的群放进 unclassified_groups

只输出 JSON，不要任何解释：
{{"projects":[{{"name":"","description":"","groups":[],"keywords":[],"confidence":"high"}}],"unclassified_groups":[]}}"""

    text = call_claude(prompt)
    try:
        result = parse_json(text)
    except Exception as e:
        raise SystemExit(f"模型输出解析失败：{e}\n原始输出前 500 字：{text[:500]}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        "\n# 注意：以上是模型归纳的候选，可能把同一个项目拆成多块、或把不同项目混在一起。\n"
        "# 必须和用户确认后，才能把确认结果写进 config.json 的 projects 字段，不要直接采用。",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
