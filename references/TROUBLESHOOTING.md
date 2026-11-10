# 飞书周报排障手册

维护者文档。日常使用看 `SKILL.md`，出问题先跑：

```bash
python3 ~/.claude/lark-weekly-doctor.py
```

## 排障顺序

1. `doctor.py` 哪项红灯
2. `~/.claude/lark-weekly-report.log`：执行到哪一步、有没有 `[done]`
3. `~/.claude/lark-weekly-guard.log`：守卫判定的结果
4. `~/.claude/lark-weekly-check.log`：SessionStart 兜底有没有触发
5. 会话记录：`grep -l "lark-weekly-task" ~/.claude/projects/*/*.jsonl`，找 `Running scheduled task` 后面紧跟的 `API Error`

## 已知故障

### 1. 内网网关不可达（最常见，2026-10-16 定位）

**症状**：定时任务触发了，但 `report.log` 里连 `[start]` 都没有，告警也没发。

**根因**：`ANTHROPIC_BASE_URL` 指向的网关解析到内网地址（本机实测 `litellm.hexa.team -> 10.100.31.1`）。断网或没连内网时，第一次 API 调用就 `ENOTFOUND`，会话当场死掉。prompt 从未执行，所以告警链路也一起失效。

**证据长这样**：

```
21:45:21  type=system     Running scheduled task
21:45:22  type=user       <prompt>
22:01:20  type=assistant  API Error: ... ENOTFOUND
```

中间那段静默是 DNS 重试耗尽的过程。

**诊断**：

```bash
python3 -c "import socket; print(socket.gethostbyname('<你的网关域名>'))"
```

**现有防护**：守卫在补跑前先 `gateway_ok()` 探测，不通就跳过，省掉十几分钟空等；网络问题会等最后一次机会才告警。

### 2. Claude 认证失效

**症状**：收到「飞书周报没跑成：Claude 不可用」。

**根因**：本机没有独立登录文件，认证就是 `~/.claude/settings.json` 里的 `ANTHROPIC_AUTH_TOKEN`；token 失效或 CLI 损坏时补跑也跑不了。

**处理**：更新 token 或 `claude auth` 重新登录，然后读 `lark-weekly-task.md` 手动补跑。守卫会立即告警，不等四次机会用完，因为认证问题重试不会自己好。

### 3. 飞书授权过期

**症状**：`doctor` 报「飞书授权」红灯，或 `check.py` 提示授权失效。

**根因**：refresh token 有效期 7 天，每次使用滚动顺延；连续 7 天以上不跑任何 user 命令就会断（国庆假期踩过）。

**处理**：`lark-cli auth login --recommend`。

**注意**：判断健康只看 `refreshExpiresAt` 剩余天数。`status` 字段日常就是 `needs_refresh`（access token 只有几小时寿命），不代表授权坏了。

## 开发/测试注意

### 递归套娃（2026-10-10 事故）

`try_rerun()` 曾经用 `dict(os.environ)` 把环境全量传给子 `claude`，子会话继承了 Claude Code 的会话变量、resume 到父转录，看到测试脚本又跑一次，每代翻倍。实测涨到 21 个 `claude -p` 嵌套进程。

**现在的防护**：白名单环境变量 + `LARK_GUARD_DEPTH` 递归闸；`main()` 入口也有一道。

**测试时仍然要注意**：
- 别用真实 `claude -p` 验证补跑链路，直接 stub `try_rerun`
- 别让测试写到真实状态文件，把 `m.STATE` / `m.GUARD_STATE` 指到 `/tmp`

### 文档与代码漂移

`SKILL.md` 曾长期写着 `CronCreate` 续期、补跑 2 次、plist 漏时刻。改实现后记得同步文档，或者用 `doctor` 的输出作为事实来源。

## 设计决策时间线

| 时间 | 改动 | 原因 |
|---|---|---|
| 2026-09-27 | 加 auth 预检 + 失败告警 | 授权在触发后几小时才过期，旧版零告警哑了两天 |
| 2026-10-08 | 补跑起点改为 `last_report_end + 1` | 旧的 `max(今天-6, ...)` 会丢掉补跑区间开头几天 |
| 2026-10-10 | 加 launchd 守卫 | cron 崩掉时日志和告警同时空白 |
| 2026-10-16 | 触发时间 21:00 → 20:50，四入口 | 用户要求多档补救 |
| 2026-10-22 | 弃用 CronCreate，全用 launchd | 实测 cron 依赖 Claude 进程活跃，用户没开就完全不触发 |
| 2026-10-26 | 配置外置 + 项目自动发现 + doctor | 准备给同事用，硬编码会直接失效 |

## 为什么现在是这个架构

- **launchd 而不是 cron**：launchd 不依赖 Claude Code 进程，且同一任务不会重叠执行。
- **守卫而不是纯告警**：网络恢复后能自己把周报补出来，用户不用动手。
- **补跑而不是重试原会话**：原会话已经死了，起一个新的 `claude -p` 更干净。
- **四次机会而不是一次**：周报通常几分钟到二十分钟，多给几次窗口能覆盖大部分临时故障。
