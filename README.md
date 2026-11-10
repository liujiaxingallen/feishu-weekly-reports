# feishu-weekly-report

飞书周报自动化：每周自动汇总群聊、邮件、文档，生成十段式周报并推到你的飞书。装一次，之后无人值守。

## 安装

```bash
# 1. 添加 marketplace
claude plugin marketplace add <此仓库的 git 地址>

# 2. 安装插件（在 Claude Code 里，或命令行）
claude plugin install feishu-weekly-report@feishu-weekly-report

# 3. 跟 Claude 说「帮我装一下飞书周报」，它会跑 scripts/install.sh 搭好定时任务
#    或者你自己跑：
#    bash <插件目录>/scripts/install.sh
```

装完自检：

```bash
python3 <插件目录>/scripts/doctor.py
```

## 前置条件

- 任意平台：macOS / Linux / Windows
- 装了 Claude Code CLI
- 装了 lark-cli，并用你自己的飞书账号授权（`lark-cli auth login --recommend`）
- 能访问你配置的 AI 网关

## 它做什么

- 每周四 20:50 起四个时刻自动触发，无人值守
- 拉取群聊、邮件、日历、任务、云文档六类数据
- 按项目归类，生成十段式周报：
  关键事项 / 下周提醒 / 项目动态 / 私聊沟通 / 邮件进展 / 文档 / 会议 / 任务进度 / 高光时刻 / 祝福语
- 创建飞书文档并推送链接
- 失败自动补跑，补不动主动发飞书提醒

## 定时器（按平台自动选择）

| 平台 | 机制 |
|---|---|
| macOS | launchd |
| Linux | crontab |
| Windows | 任务计划（schtasks） |

## 说明

- 项目归类由 `discover.py` 自动归纳候选，安装时请确认，归纳错了要改。
- 排障先跑 `doctor.py`，再看 `~/.claude/lark-weekly-report.log`。

## 许可证

MIT