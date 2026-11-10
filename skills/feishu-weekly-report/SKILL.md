---
name: feishu-weekly-report
description: 生成飞书周报并维护其定时循环。当用户要安装、生成、查看、配置、排查或修改「每周飞书周报」（汇总过去一周群聊+邮件+文档、按项目归类、生成飞书文档并经机器人推送）时使用。首次使用时需要先帮用户跑一次安装脚本搭好定时任务。触发词：飞书周报、周报任务、定时周报、weekly report。
---

# 飞书周报

每周自动汇总过去一周的**群聊 + 工作邮件 + 我更新的文档**，按项目归类生成十段式周报，推送到飞书。没跑成会自动补跑，补不动会主动发飞书提醒。

## 首次安装（用户说「装一下」时执行）

前置：macOS / Linux / Windows 任意平台，装了 `claude` CLI 和 `lark-cli`（已用用户自己的飞书账号授权）。

在本技能目录的 `scripts/` 下有个 `install.sh`，用它装：

```bash
bash /<本技能安装路径>/scripts/install.sh
```

它会：检查环境 → 自动取 openId → 扫描用户的群归纳项目候选（让用户勾选）→ 写配置 → 按平台生成定时任务（macOS launchd / Linux crontab / Windows schtasks）→ 自检。

装完跑一次自检确认全绿：

```bash
python3 /<本技能安装路径>/scripts/doctor.py
```

**注意**：项目归类是归纳的候选，必须让用户确认后才能写进配置。归纳结果如果拆错项目（比如把一个项目拆成多块），要按用户的纠正改。

## 日常使用

```bash
python3 scripts/doctor.py       # 自检
python3 scripts/guard.py        # 手动跑一次（没跑成会自动补）
python3 scripts/discover.py     # 重新归纳项目候选（需用户确认后写配置）
```

## 触发机制

四入口自动触发（每周四 20:50 / 22:30、周五 08:00 / 10:15），由平台定时器驱动。守卫会补跑、探测网关、检测 Claude 可用性；第二次检查后周报还没生成就通过飞书主动提醒用户。

## 周报结构（十段式）

一、关键事项；二、下周关注与提醒（紧急标加急）；三、项目动态（按项目一条一事）；四、私聊沟通（末尾含「给自己的记录」）；五、邮件进展；六、我更新的文档；七、本周会议；八、任务进度（有 deadline 才标加急）；九、高光时刻（历史事件类比，不编造）；十、祝福语（本周正向 + 下周期待，禁空话）。

完整执行步骤见 `references/lark-weekly-task.md`（install.sh 会把它复制到 `~/.claude/lark-weekly-task.md`）。

## 配置文件

`~/.claude/lark-weekly-config.json`，含 openId、触发时刻、项目规则、邮件黑名单。改完不用重装，脚本每次运行会重新读。

## 排查

先跑 `python3 scripts/doctor.py`，红灯按提示修。事故史和根因分析见 `references/TROUBLESHOOTING.md`。