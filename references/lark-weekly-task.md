# 飞书周报自动生成任务

## 触发时机
- 唯一触发源（2026-10-22 起）：launchd 任务 `com.fusheng.lark-weekly-guard`，四个时刻 **周四 20:50 / 22:30、周五 08:00 / 10:15**。
  **不再使用 CronCreate**：cron 依赖 Claude Code 进程处于活跃状态，用户晚上没开 Claude Code 就完全不触发（2026-10-22 实测 `lastFiredAt` 为空、`scheduled_tasks.json` 自 10-16 建好后一次都没被改过）。launchd 不依赖进程，是可靠的那条路。
  （2026-10-10 从「周五凌晨 4:00」改到晚上 9 点，当时给的理由是「避开 DarkWake 断网」。**那个理由后来被证伪**：10-15 晚 21:45 照样中了同样的招，真正原因是内网网关不可达、跟时段无关。这个时间调整保留，但它不是解决方案，详见下面「已知故障」。）
- 兜底一（会话启动时）：hook 跑 `~/.claude/lark-weekly-check.py`，命中「授权失效/临期」或「上次周报早于最近一个应跑完的周四且无有效定时任务」时提醒。
  注意 due 是跟**周四**对齐的，不是「距上次 ≥7 天」——后者有个洞：漏跑后接下来几天距上次还不到 7 天，兜底会继续沉默，要等到第 8 天才报，那一期等于白丢（2026-09-30 修）。
- 守卫（launchd）：`com.fusheng.lark-weekly-guard` 在**周四 20:50 / 22:30、周五 08:00 / 10:15** 跑 `~/.claude/lark-weekly-guard.py`。本期没跑成时用 `claude -p` 补跑一次，成功就静默。补跑前先探测网关，不通就不启动 claude（省掉 `ENOTFOUND` 的十几分钟空等）。
  **告警时机（2026-11-10 改）：第二次检查（周四 22:30）之后周报还没生成，就主动发 bot 提醒「上线 Claude 或任意助手手动触发周报」**；第一次失败静默，避免刚触发就跑造成的误报。它绕开了 Claude 会话，是唯一不依赖进程的触发路径。

  四个时间点：**周四 20:50 → 周四 22:30 → 周五 08:00 → 周五 10:15**。前一个不成，后一个接着上。

- **Claude 不可用会立即告警，不等第二次检查**（2026-10-26 加，2026-11-10 并入统一告警）：网关通但 `claude` 本身不可用时（`ANTHROPIC_AUTH_TOKEN` 失效、CLI 损坏），重试不会自己好，guard 会跳过补跑、直接用 bot 发「请上线 Claude 或助手手动触发」的提醒。判断方法是补跑前跑一次极短的 `claude -p "只回复 OK"`，90 秒超时。本机没有独立的登录文件，认证就是 `~/.claude/settings.json` 里的 `ANTHROPIC_AUTH_TOKEN`。

## 执行步骤（全程自动，不要中途停下来问用户）

0. 认证预检（**必须放在所有数据拉取之前，且按剩余天数分三档，不能只看「是否已过期」**）：
   跑 `lark-cli auth status`，读 identities.user 的 `refreshExpiresAt`，算距今天的剩余天数：
   - **剩余 ≥ 7 天**：正常，继续。
   - **剩余 < 2 天但未过期**：记 `[warn] auth` 日志，先按步骤 9 发一条 bot 提醒（「user 授权 X 日后过期，请在过期前跑 `lark-cli auth login --recommend`」），然后**继续跑正文流程**——此时 token 还能用，周报照出。
     （阈值取 2 天而不是 7 天：refresh 有效期本来就是 7 天且会滚动顺延，按 7 天算几乎每周都命中，提醒就成噪音了。）
   - **已过期 / user 身份 status=missing**：三源全废，**不要硬跑**。实测降级到 bot 后的报错分别是：`messages-search` 和 `drive +search` 报 `app_scope_not_applied`（缺 search:message），`mail +triage` 报 `--as bot does not support --mailbox me`。记 `[fail] auth`，按步骤 9 发 bot 告警，**不更新状态文件、不重建 cron**，结束。

   为什么必须报剩余天数：2026-09-25 那次，refresh 在当天 08:41 才到期，而任务是 04:39 触发的，旧版只判「是否已过期」直接放行，结果整轮静默哑掉、零告警，直到 09-27 用户碰巧开会话才暴露。

   为什么告警通道能成立：bot 身份不依赖 user token，恒为 ready，user 身份挂了它照样能发消息。

   **注意 `status` 字段别当依据**：`auth status` 里 user 的 status 日常会显示 `needs_refresh`（access token 只有几小时寿命，隔天再看就是它），这是正常的，跑任何一条 user 命令都会自动刷新，并把 refresh token **顺延 7 天**。2026-09-30 实测：`needs_refresh` 状态下刷新成功，`refreshExpiresAt` 从 10-04 顺延到 10-07。判断健康与否**只看 `refreshExpiresAt` 剩余天数**，只有它到期才是真失效（`status=missing`）。

   顺带一提，这个滚动续期意味着：**每周正常跑就不会断**。真正会断的情况是连续 7 天以上没跑过任何 user 身份命令——这也是 2026-09-25 那次哑火的根源（09-18 刷新过，09-25 04:39 触发时 refresh 只剩 4 小时）。

1. 计算时间范围（**起点以状态文件为准，不要用「今天-N 天」**）：
   - 止点 = 今天 `23:59:59`；
   - 起点 = `last_report_end + 1 天`，取 `00:00:00`——上次周报止点的次日，天然衔接，这是唯一「不重不漏」的算法。两个边界：
     - 状态文件缺失/读不出 → 退回 `今天-6 天`；
     - 算出的起点若早于 `今天-30 天` → 截到 `今天-30 天`（防断档太久把范围拉爆）；
     - **算出的起点晚于止点 → 说明本期已经出过结果，直接结束**（记 `[skip] already-done`，不生成文档、不推送、也不再动 state）。2026-10-16 加：主触发（周四 20:50）和守卫补跑（周四 22:30 / 周五 08:00 / 周五 10:15）是四个独立入口，**可能撞车**，没这道闸，后到的那个会算出 `起点 > 止点`，照样生成一份空周报推给用户。
   - 格式 `YYYY-MM-DDT00:00:00+08:00` ~ `YYYY-MM-DDT23:59:59+08:00`。
   - 正常情况下它跟「今天-6 天」相等（上周五到本周四正好 7 天），平时看不出区别；**一旦漏跑，差别就出来了**。
   - 实例1（正常）：state=`10-02`，`10-09` 跑 → 起点 `10-03`，覆盖 7 天。
   - 实例2（补跑）：state=`09-27`，`10-08` 补跑 → 起点 `09-28`，覆盖 `09-28~10-08` 共 11 天。

   ⚠️ **2026-10-08 修正**：这里原先写的是 `max(今天-6, last_report_end+1)`，**是错的**。max 取较晚的那个，而补跑时恰恰要取**较早**的。按旧公式，state=09-27、10-08 补跑会算出起点 10-02，把 09-28~10-01 整整 4 天丢掉。当时配的实例写的却是 09-19（等于 min 的结果），公式和例子自相矛盾，一直没人发现。
   （也不要简单换成 min：补跑完 state 变成补跑当天，下次 min 会把已经报过的内容再算一遍。以 `last_report_end+1` 为准才对。）

2. 拉取五个数据源到 /tmp：
   - 群聊：`lark-cli im +messages-search --start <起> --end <止> --page-all --page-size 50 --format json > /tmp/lark-msgs.json`
   - 群名映射：`lark-cli im +chat-list --page-all --page-size 100 --format json > /tmp/lark-chats.json`
   - 邮件：`lark-cli mail +triage --max 200 --format json > /tmp/lark-mails.json`
   - 日历（本周会议）：`lark-cli calendar +agenda --start <起> --end <止> --format json > /tmp/lark-calendar.json`
   - 任务：`lark-cli task +get-my-tasks --format json --page-all > /tmp/lark-tasks.json`
   - 文档：`lark-cli drive +search --mine --edited-since <跨度>d --sort edit_time --page-size 20 --format json > /tmp/lark-docs.json`
     **必须带 `--mine`**（2026-10-16 加）：不带的话 API 会把别人的文档一起返回，而且单次硬上限就是 20 条，撞上限后 `has_more: true`，我自己的文档会被挤出去。10-16 实测对比：不带 `--mine` 拿到 12 篇且 `has_more=true`，带上后 13 篇、`has_more=false`，多出来的 3 篇是「OHO 内测V1用户报名统计」「OHO 国际访谈规划」「OHO 内测体验计划 V1.0」。第四段凭空少三篇，全程不报任何错。
     `--edited-since` 必须**跟随步骤 1 算出的实际跨度**：正常 7 天填 `7d`，补跑 9 天就填 `9d`。写死 `7d` 会在补跑时漏掉开头几天的文档（09-27 补跑 9 天时踩到过，只是那两天恰好没人动文档才没露馅）。
   - **文档搜索必须验非空，空了要重试**：飞书 drive search 有 cold search 问题，**首次查询会返回 `total=0` 的假空结果**。2026-10-08 实测：同一参数 `--edited-since 11d`，第一次 `total=0`，原样重试得 8 条；`7d` 同样出现过 0。所以拿到 `total=0` 时**必须原参重试一次**，两次都空才能判定本期真没有更新的文档。别把假空当真空——那会让周报第四段凭空消失，而且全程不报任何错。

3. 分析归类（**规则来自配置，不要在这里硬编码项目**）：
   - 🔴 **项目归类是敏感信息，绝不能自作主张**。`discover.py` 归纳出的只是**候选**，必须和用户确认后才能写进 `projects` 字段。归类错一条，整期周报的项目归属就错一块。
     2026-11-09 用户纠正过一次：discover 把 OHO 拆成了「研发 / 市场 / CES / 合鹄红人」四块，实际它们**都是 OHO**；界环和 anchor / A2P（A2 Pro）是**同一个界环眼镜项目**；小米海外（SSS 海外、SS2 海外）是**独立的项目**。这些凭群名和关键词是归纳不出来的，只能靠用户拍板。
   - **新项目出现时也要随时和用户确认**，不要自己猜一个归类就写进去。
   - 读 `~/.claude/lark-weekly-config.json` 的 `projects` 数组。每项有 `name`、`description`、`groups`、`keywords`。
   - 判定顺序：群名命中 `groups` → 归到该项目；群名没命中，看消息内容是否命中该项目的 `keywords`。
   - 都没命中的，按群名语义归到「其他」，例如供应链 / 物料、售后、商务、飞书产品反馈、AI 学习、生活。
   - ⚠️ **同一个词可能横跨多个项目**（比如「DVT」可能在两个项目里都在用）。不能只看单个词，要读同一段对话里的项目名和上下文再判。
   - 🔴 **写完必须自检**：逐个项目段落检查，里面出现的专有名词是否属于该项目。如果 A 项目的 `keywords` 出现在 B 项目段落里，就是归错了。
   - 配置里没有 `projects` 时（全新安装未跑 discover），退化为按群名语义粗分，并在周报开头注明「未配置项目规则」。
   - 过滤：p2p 单聊里 `sender.id_type == "app_id"` 的是机器人通知（Mail/Calendar/Approval/Docs 助手、分贝通等），忽略；`id_type == "open_id"` 才是真人，纳入。**字段是 `sender` 对象里的 `id_type`，顶层没有 `sender_type` 这个字段，别按旧措辞去找。**
   - 单聊对象名解析：p2p 单聊的 chat_partner 只有 open_id，用 `lark-cli contact +search-user --user-ids "<逗号分隔open_id>" --as user --format json` 批量反查姓名（字段 `localized_name`）。
   - 反查不到的（外部联系人、通讯录不可见）**不要直接当机器人丢掉**：看这条会话里 `sender.name` 里非「浮生」的那个名字，就是对方姓名（例：夏天、半夏、天行、三谷都是这么认出来的）。只有 `sender.id_type == "app_id"` 才是真机器人。
   - **chat_partner 就是我自己 open_id 的那个会话，是「发给自己的备忘」**：不是机器人，也不是和别人的对话。里面常是选品要求、要准备的材料、待办提醒、临时结论、账号凭证之类。单独归到周报第四段的「给自己的记录」子节，**别因为「对方是我自己」就整条跳过**。
   - 注意 `contact +search-user --user-ids` **单次最多返回 20 个**，且没有自动分页，id 多了要自己分批传（09-27 那次 33 个 id 只回了 20 个）。
   - 邮件过滤：发件地址命中 `config.json` 的 `mail_noise_domains` 就剔除（营销、系统通知、各类 Assistant）；
     `mail_work_domains` 里的域名视为工作往来，优先保留；两边都没命中的，看内容判断是不是工作邮件。
     名单都在配置里，换公司/换人只需要改配置，不用改这份文档。
   - 对保留的工作邮件，用 `lark-cli mail +messages --message-ids "<id1>,<id2>" --html=false --format json 2>/dev/null` 读正文（body_plain_text），提炼 1-2 句要点，不要只列主题。
   - 邮件归类：唤醒词相关 → SE/AI陪伴；低功耗 SDK / 功耗 → OHO 功耗线；其他按内容归到对应项目。

4. 生成 markdown 周报，标题「周报（起~止日期）」，**十段式**。全篇用短条目（`- `）承载信息，**不要把多件事揉进一个长段落**——2026-10-16 用户反馈「内容都堆在一个大段落里」，这条是硬要求。

   **一、关键事项**
   本期对整个项目影响最大的事，单独拎出来，**3~6 条**。每条一两句说清「发生了什么 + 影响是什么」。
   够格的标准：影响交付或上市节奏、涉及技术路线变更、涉及对外承诺或客户、需要跨部门决策、有金额或有交付事故的。
   **宁可少而准，不要把所有事都塞进来**；确实没有就写「本周无重大变化」。

   **二、下周关注与提醒**
   值得下周提前注意的动作和风险，从本周沟通里**主动提炼**（不是复述原话），要给出「要做什么 / 为什么现在得关注 / 时间点」。
   **紧急且重要的排最前**，并用 `**加急**` 标注；凡是有明确时间点的（deadline、开售日、会议、审核周期）必须把日期写出来。

   **三、项目动态**
   按项目归类（SE/AI 陪伴、OHO、其他），**一条一事**。同一件事的细节可以放同一条，但别超过三四句，更不要几件事写成一整段。

   **四、私聊沟通**
   按「跟谁」分组，每组下面用 `-` 列要点。
   **末尾必须有「给自己的记录」子节**：p2p 里 chat_partner 就是我自己 open_id 的那个会话，里面常是我发给自己的备忘和 to-do（选品要求、要准备的材料、提醒自己去问的事、临时记的结论），这些是重要输入，**不能因为「对方是我自己」就跳过**。

   **五、邮件进展**
   读正文后提炼要点，按项目归类（不是只列主题）；本期没有工作邮件就直说。

   **六、我本周更新的文档**
   从 drive 结果提取标题 + url，**用 markdown 链接 `[标题](url)` 写入**（不能只写标题不带链接），按项目归类。

   **七、本周会议**（2026-11-09 加）
   从日历数据里挑出「我参与或组织的会议」，按时间列出：日期 + 标题 + 组织者（我组织的就注明）。
   纯个人日程（如「整理每周日记」「输出 PPT 内容」这种只有我自己、无他人的）不算会议，跳过。
   开过会且有纪要的，尽量带上纪要链接。

   **八、任务进度**（2026-11-09 加）
   从任务数据里分两栏：
   - **未完成**（重点）：列出 summary；有 due 的标日期；**「加急」只看 deadline**（有明确 due 日期的就标 `**加急**`，没有 due 的不标；2026-11-09 用户指定）。
   - **本周完成**：只列 `completed_at` 落在本期范围内的；数量多就只列有代表性的几条 + 总数，不用全列。
   - **任务来源不限于飞书任务**：从本周群聊里识别出的待跟进事项，也并入未完成栏。**边界（2026-11-09 用户确认）只认三类**：① 同事 @我 明确要结果/要确认的；② 我本人回复「我来 / 我负责 / 我去做」的；③ 带明确 deadline 且指向我的。闲聊、泛泛提到、别人的事都不列。

   **九、高光时刻**（2026-11-09 加）
   本周最值得开心的一件事，从项目动态里提炼，**只写一条**，说清「发生了什么 + 为什么值得开心」。
   然后附一句「与历史事件的类比」：把这件开心事类比到一个**真实**的历史事件（产品发布、里程碑、转折点这类）。
   ⚠️ 历史事件必须真实、可查，**绝不编造**；类比要贴切自然，不要为了共鸣硬扯。**用事件类比，不用名人名言**（2026-11-09 用户指定）。

   **十、祝福语**（2026-11-09 加）
   一段话，结构是「**总结本周的正向进展 + 对下一周的期待**」。
   真诚、积极正向，**不做负面共鸣**。
   禁止：`加油`、`塞翁失马`、`福祸相依`、`好事多磨`这类空话和陈词滥调；不煽情、不假大空。
   从本周实际进展里找正向的东西写（一件落地的事、一个确定的节点），落到对下一周的期待上。

5. 创建飞书文档：
   `lark-cli docs +create --doc-format markdown --title "周报（起~止）" --content - < /tmp/lark-weekly.md`
   拿到返回的 url。

6. 机器人给用户发消息：
   `lark-cli im +messages-send --as bot --user-id ou_8c33703b9c34dd07c1549e4c7d535691 --markdown "周报已生成：<url>"`

7. 更新状态文件 `~/.claude/lark-weekly-state.json`：`{"last_report_end": "<今天日期>"}`。

8. 检查调度器（**2026-10-22 起不再用 CronCreate**）：
   - 周报的唯一触发源是 launchd 任务 `com.fusheng.lark-weekly-guard`（周四 20:50 / 22:30、周五 08:00 / 10:15）。
   - 跑完确认一下：`launchctl list | grep lark-weekly-guard` 有输出、第二列退出码为 0 就行。如果没加载，重新 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.fusheng.lark-weekly-guard.plist`。
   - **不要再用 CronCreate 建周报任务**。cron 依赖 Claude Code 进程处于活跃状态，这条已经用 10-22 的实测证伪：周四 20:50 用户没在用 Claude Code，cron 完全没触发，`lastFiredAt` 为空、`scheduled_tasks.json` 从 10-16 建好后一次都没被改过，最后是 22:30 的 launchd 守卫把周报补出来的。
   - 如果 `scheduled_tasks.json` 里还残留着 prompt 含 `lark-weekly-task` 的旧任务，用 `CronDelete` 清掉，避免它偶尔触发时和守卫撞车。

9. 失败告警（**任何一步失败都要走，这是 2026-09-27 事故的核心修复**）：
   ```
   lark-cli im +messages-send --as bot --user-id ou_8c33703b9c34dd07c1549e4c7d535691 \
     --markdown "⚠️ 飞书周报执行失败｜步骤：<步骤>｜原因：<原因>｜详见 ~/.claude/lark-weekly-report.log"
   ```
   - 必须是 `--as bot`：user 身份坏掉（auth 过期正是最常见的失败原因）时 user 通道同样发不出去，bot 身份恒为 ready，是唯一可靠的告警通道。
   - 覆盖范围：步骤 0 的授权到期提醒、步骤 2 三源任一拉取为空/失败、步骤 4 打不开正文、步骤 5 建文档失败、步骤 6 推送失败、步骤 8 续期失败。
   - 旧版教训：失败只往日志文件里写一行，用户不主动去翻就永远不知道。09-25 那次哑了整整两天，直到用户自己碰巧开会话才被发现。**日志是给自己排障用的，告警才是给用户看的。**

## 执行日志（每步必记，用于排障）
每次执行，用 append 方式把关键步骤写到 `~/.claude/lark-weekly-report.log`，每行格式 `时间戳 [标记] 结果`：
- `[start]` 开始，记时间范围
- `[data]` 三个数据源各拉到多少条（群聊/邮件/文档）
- `[doc]` 创建文档结果 + url（失败记失败原因）
- `[msg]` 机器人发消息结果 + message_id
- `[state]` 状态文件更新为哪个日期
- `[cron]` 续期结果：重建的 job_id（及删掉的旧 job_id）
- `[warn]` 非致命问题，如授权临近过期、某个数据源条数异常偏少（照常继续跑）
- `[alert]` 已发出 bot 告警，记结果 + message_id
- `[done]` 全部完成；任一步失败记 `[fail] <步骤> <原因>`

示例：`echo "$(date '+%F %T') [doc] 创建文档成功 url=https://..." >> ~/.claude/lark-weekly-report.log`

## 自动化权限依赖（零手动确认）

本任务每周四 20:50 由持久化定时任务自动触发，无人值守，任何 Bash 权限弹窗都会让流程卡死、无法自动完成。以下命令**已加入** `~/.claude/settings.json` 的 `permissions.allow`，执行时不再弹窗、无需手动确认：

- `Bash(lark-cli *)`：飞书全部操作（auth / im / mail / drive / contact / docs / messages-send）
- `Bash(python3 *)`：**数据处理主力**——解析 /tmp 下的大 JSON（按会话聚合、open_id→姓名映射、机器人过滤、内容截断）。2026-09-30 发现此前漏配，而 09-27 的补跑全程都在用它，等于在凌晨的执行路径上留了颗定时炸弹。
- `Bash(jq *)`、`Bash(awk *)`、`Bash(sort *)`、`Bash(uniq *)`：轻量文本处理
- `Bash(printf *)`：写状态文件 `lark-weekly-state.json`
- `Bash(date *)`：日志时间戳（`$(date ...)` 命令替换里也会单独检查，必须单独 allow）
- `Bash(cd *)`：`cd /tmp; ...` 这类复合命令里的 `cd` 会触发权限弹窗，单独 allow

维护规则：
- 后续新增步骤若用到新命令，先确认它是否已在 allow 列表；没有就先补进 settings.json，再跑定时任务。
- **2026-09-30 教训**：清单曾漏掉 `python3`，而实际执行时数据处理全程依赖它。无人值守下任何未 allow 的命令都会弹窗卡死，卡死的表现是「日志停在半路、用户什么都收不到」——既不报错也不告警，最难排查。流程改完之后，要拿**实际真跑过的那批命令**逐条比对清单，别只信文档里写了什么。
- 排查「定时任务没跑 / 中途中断」时，除看日志外，也要核对 settings.json 的 allow 是否覆盖了本任务用到的全部命令。
- `Bash(echo *)` 已在 allow，但 `$(date)` 命令替换内的 `date` 会单独触发权限检查，所以 `date` 也单独 allow 了。

## 授权保活（2026-10-08 新增）

`~/Library/LaunchAgents/com.fusheng.lark-token-keepalive.plist`：每天 10:23 跑一次
`lark-cli contact +search-user --user-ids me --as user --format json`，让 refresh token 滚动顺延 7 天。

**为什么需要**：refresh token 只在「7 天内有 user 命令」时才续期。连续 7 天不跑（比如国庆 10/1~10/7 没上线）授权就会过期，整套周报随之哑掉——2026-10-07 就是这么断的。这个任务**不依赖 Claude Code 是否运行**，所以能扛过「机器开着但人不在」的长假。

维护：
- 查看：`launchctl list | grep lark-token-keepalive`（第二列是退出码，0 为正常）
- 日志：`~/.claude/lark-keepalive.log`
- 重载：`launchctl bootout gui/$(id -u)/com.fusheng.lark-token-keepalive`，再 `launchctl bootstrap gui/$(id -u) <plist>`
- **PATH 是坑**：lark-cli 是 node 脚本（`#!/usr/bin/env node`），而 launchd 默认 PATH 里没有 homebrew 目录，plist 必须显式写 `EnvironmentVariables.PATH`，否则直接报 `env: node: No such file or directory`（2026-10-08 实测）。
- **局限**：机器完全关机时不跑。那种情况下授权仍会过期，只能等回来后重新 `auth login --recommend`。

## 已知故障：内网网关不可达打掉整个流程（2026-10-16 修正根因）

**症状**：定时任务确实触发了（`scheduled_tasks.json` 的 `lastFiredAt` 有记录），但 `lark-weekly-report.log` 里连 `[start]` 都没有，用户什么也收不到。10-02、10-09、10-15 各发生一次。

**根因（10-16 修正）**：`ANTHROPIC_BASE_URL` 指向 `https://litellm.hexa.team`，**它解析到 `10.100.31.1` 这个内网地址**，必须连上内网/VPN 才通。一旦解析不了，对网关的第一次 API 调用就 `ENOTFOUND`，会话当场死掉；prompt 从未执行，所以步骤 9 的 bot 告警也没机会发，**API 层挂掉时告警链路整体失效**。

⚠️ 10-10 我把它归因成「凌晨 DarkWake 网络未就绪」，**那个判断是错的**。10-15 那次发生在周四 21:45 和 22:31，用户当时正在用 Claude Code 干活（21:17 还有正常交互），照样 `ENOTFOUND`。所以跟时段、跟休眠都无关，**只有一个变量：网关可达性**。

**证据**（会话 jsonl 里的原始记录，三起故障同构）：

```
10-02 04:42:59  type=system     Running scheduled task (Oct 2 4:42am)
10-02 06:37:25  type=assistant  API Error: ... DNS (ENOTFOUND)

10-09 04:31:20  type=system     Running scheduled task (Oct 9 4:31am)
10-09 04:50:53  type=assistant  API Error: ... DNS (ENOTFOUND)

10-15 21:45:21  type=system     Running scheduled task (Oct 15 9:45pm)
10-15 21:45:22  type=user       <prompt>
10-15 22:01:20  type=assistant  API Error: ... DNS (ENOTFOUND)   ← cron 那次
10-15 22:31:45  guard 启动 claude -p 补跑
10-15 23:08:16  type=assistant  API Error: ... DNS (ENOTFOUND)   ← 补跑那次
```

中间那段静默（10-02 静默 1h54m、10-09 静默 19m32s、10-15 两次分别 15m58s 和 36m31s）都是 DNS 重试耗尽的过程。**这段空等纯属浪费**，所以 10-16 给守卫加了网关预检，不通就不启动 claude。

**排障入口**：`grep -l "lark-weekly-task" ~/.claude/projects/-Users-fusheng/*.jsonl` 找到当次的会话文件，看有没有 `Running scheduled task` 后面紧跟 `API Error`。再直接测网关：`python3 -c "import socket; print(socket.gethostbyname('litellm.hexa.team'))"`。

**注意**：这个故障在 `lark-weekly-check.log` 里**不会**留下 auth 相关线索（授权本身是好的），check.py 会在用户下次开会话时报 `reason=due`，但那已经是几小时后的事。

**已实施的修复**（10-10 初版 + 10-16 修正）：

1. **触发时间挪走**：cron 从 `0 4 * * 5` 改到 `0 21 * * 4`，10-16 又前移到 `50 20 * * 4`（每周四 20:50）。
   ⚠️ 这条当初是基于错误归因（DarkWake）做的，**并没有解决问题**，10-15 21:45 照样中招。保留它是为了避开凌晨那个没人看的时间点，别指望它防网关故障。
2. **守卫绕开会话**：新增 launchd 任务 `~/Library/LaunchAgents/com.fusheng.lark-weekly-guard.plist`，跑 `~/.claude/lark-weekly-guard.py`。判据是「state 的 `last_report_end` 是否 >= 最近一个应跑完的周四」，没跟上就尝试补跑：
   - 补跑用 `claude -p` 跑一遍本文件的全流程，成功就静默；
   - **补跑前先 `gateway_ok()` 探测网关**（10-16 加）：DNS 解析加 TCP 连 443，8 秒超时。不通就不启动 claude，省掉 `ENOTFOUND` 那 16~36 分钟的空等，直接等下次机会；
   - **告警时机（2026-11-10 改）**：用 guard.state 里的 `checks` 计数，**第二次检查（周四 22:30）之后周报还没生成，就发 bot 提醒「上线 Claude 或任意助手手动触发」**。第一次失败静默，避免刚触发就跑造成的误报；网络不通、Claude 不可用、补跑失败三种原因各用不同文案；
   - 每期最多告警 1 条（`alerted` 去重）；「正在跑」（最后一次 `[start]` 后 3 小时内无 `[done]`）会跳过，不误报也不抢跑。
   - 跑四个时刻：**周四 20:50、周四 22:30、周五 08:00、周五 10:15**（10-16 定的节奏，主触发 20:50 之后每隔一段给一次补救机会）。

   它只用 `lark-cli` 和 `claude`，**完全不经过 Claude 会话**。因为 cron 任务 7 天过期、且只有跑成功才续期，连续失败会让触发源彻底消失——这个守卫顺带补上了那个洞：cron 没了它也每周补，补跑流程走到步骤 8 会把 cron 重建回来。

   验证记录（2026-10-10）：伪造 state=2026-10-02 后跑，日志依次 `rerun_start → rerun_finished(rets=0) → rerun_ok`，guard.state 归空；打桩 `try_rerun` 恒失败时，第 1 次不出告警、第 2 次出、第 3 次 `already_alerted`，累计正好 1 条。上一版纯告警脚本的真实发送记录：`message_id=om_x100b63aeb27130a8b309b85be9bd076`。

   ⚠️ **本地测试两个坑**（2026-10-10 都踩过，别重复）：
   - 别用真实 `claude -p` 验证补跑链路，会套娃：子会话继承父环境后 resume 到父转录，看到同一个测试脚本又跑它，每代翻倍（实测涨到 21 个 `claude -p` 嵌 `zsh` 嵌 `claude -p` 的进程）。脚本已加白名单 env + `LARK_GUARD_DEPTH` 递归闸兜底，但测试时仍应直接 stub 掉 `try_rerun`。
   - 别让测试写到真实状态文件。要把 `m.STATE` / `m.GUARD_STATE` monkeypatch 到 /tmp，否则会把 `lark-weekly-state.json` 写成伪造值，下周四守卫据此判定就会乱补跑、乱告警。

   维护：`launchctl list | grep lark-weekly-guard`（第二列退出码，0 为正常）、日志 `~/.claude/lark-weekly-guard.log`、重载沿用 token-keepalive 那套 `bootout` + `bootstrap`。**PATH 同样要在 plist 里显式写**（`claude` 和 `lark-cli` 都是 node 脚本），原因见「授权保活」一节。

## 关键参数
- 用户 openId：`ou_8c33703b9c34dd07c1549e4c7d535691`
- 状态文件：`~/.claude/lark-weekly-state.json`
- 本机时区为北京时间（Asia/Shanghai）。
