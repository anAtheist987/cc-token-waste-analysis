# Claude Code 计费与缓存行为分析

*中文(默认) · [English](./README.en.md)*

一份基于实测抓包的技术分析,重点考察 **Claude Code 的对外描述与实际计费/缓存行为之间的落差**。

## 方法

在本机用一个反向代理拦截 Claude Code 发出的全部 `/v1/messages` 请求,解析响应中的 `usage`
字段(`input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` / `output_tokens`)。
样本为 **133 条真实请求**,覆盖一段连续会话及其触发的后台/并行请求。脱敏数据与抓取脚本随仓库附带
(`data/`、`tools/`),数据中不含任何请求正文、鉴权信息或上游地址。

**计价口径(Anthropic 公开价,当代 Opus 档,USD / 1M tokens):**
输入 \$5、缓存读 \$0.50(0.1×)、缓存写 5 分钟 \$6.25(1.25×)、缓存写 1 小时 \$10(2×)、输出 \$25。
缓存倍率结构(读 0.1× / 5 分钟写 1.25× / 1 小时写 2×)与模型无关。本分析关注**结构与相对量级**,
绝对金额随单价浮动。

---

## 核心结论

1. **每个 agent 动作都触发一次安全分类器请求,该请求重读整段会话历史,并计入用户的 token 用量——
   这一成本未在任何官方文档中披露。**
2. **prompt 缓存命中按请求逐次收费(读 0.1×),不是"建好即免费复用";高频小请求会把这笔读费持续放大。**
3. **官方对缓存的多项关键变更与计费影响,采取了"事后/静默"沟通**:1 小时缓存默认值被悄悄改为 5 分钟、
   2026-03 的额度异常消耗仅在社交媒体非正式承认、缓存失效类问题长期挂在 issue 列表。

---

## 一、Auto Mode 安全分类器:每动作重读全历史,计入用户账单

### 它是什么

抓包显示,一类 `max_tokens=64`、仅产出极短裁决的请求,其 system prompt 开头为:

> *"You are a **security monitor for autonomous AI coding agents** … Your job is to evaluate whether
> the agent's latest action should be **blocked**."*

这是 Claude Code **Auto Mode(自动批准模式)** 的安全分类器,公开追踪于
[Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts),
并对应官方 [changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) 中多条记录。
其职责:在 agent 每次执行动作前,把**整段 transcript** + 约 40K 字符的安全规则(威胁模型涵盖
prompt injection、scope creep、删除/外泄等)送入模型,输出 ALLOW/BLOCK 裁决。

官方 changelog 的一条记录可与抓包直接对应——*"Fixed auto mode incorrectly blocking actions …
**when the safety classifier ran out of output tokens** while reasoning"*——正对应这里观测到的
`max_tokens=64` 短裁决请求;另见 [issue #39259](https://github.com/anthropics/claude-code/issues/39259)
(分类器不可用时误拦只读操作)。

### 实测成本

| 指标 | 数值 |
|------|------|
| 安全分类器请求数 | **41** |
| 累计 `cache_read` | **2,101,257 token** |
| 累计 `output` | **1,423 token** |
| 读 : 出 比 | **≈ 1,476 : 1** |
| 占全样本全部缓存读的比例 | **≈ 19%** |
| 该类请求缓存读成本 | ≈ \$1.05 |

也就是说:**为产出约 1,400 个裁决 token,系统重复读取了 210 万 token 的会话历史**;这部分占到全样本
缓存读取的近五分之一。这些请求每条都携带完整的安全规则提示 + 全部会话历史(单次读取常达 12 万 token 量级)。

### 落差所在

- **官方将 Auto Mode 描述为安全能力**(见 [Building safeguards for Claude](https://www.anthropic.com/news/building-safeguards-for-claude)
  及 changelog),其防护价值真实存在——这不是无意义请求。
- **但未在任何文档中说明该安全检查的运行成本由用户的 token 预算承担**,也未说明它会**在每个动作上
  重读整段会话历史**。对按量计费或有额度上限的用户,这是一笔可观且不可见的固定开销。

---

## 二、缓存命中按请求逐次计费,高频小请求持续放大读费

Messages API 无状态:每个请求都须重发完整上下文。prompt 缓存命中时,重复前缀按 `cache_read`
计费(0.1× 基础输入价),**且是每个请求都按该价收取一次,而非"建立后免费复用"**。
[官方缓存文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)对此有定义,但用户的
直观预期(连续会话 = 服务端保留状态、不应反复计费)与实际机制存在偏差:

- 服务端不存在"会话"这一计费实体;所谓"会话未中断",在计费上等同于"缓存仍处于热状态",其标价即为
  每次读取 0.1×。
- 因此,**任何高频、低产出的请求(如上一节的安全分类器、或每一跳工具调用)只要携带完整前缀,
  都会反复触发十几万 token 的读费**。请求频次越高,累计读费越高。

全样本中缓存读取累计达 **1,112 万 token**,约占总成本的四分之一,其中近 19% 来自安全分类器一类的
后台请求。

---

## 三、缓存失效:冷启动与并行分支会触发 1.25× 全量重写

缓存未命中时,整段前缀按 `cache_creation`(5 分钟写 1.25×)重写,单价高于基础输入价。样本中的大额
重写均可解释为**冷启动**(首次构建该前缀)或**并行分支**(并发子任务各自建立缓存),而非热缓存被驱逐:

| seq | 间隔 | cache_read | cache_creation(1.25×) | output | 判定 |
|----:|----:|---------:|--------------------:|------:|------|
| #3 | — | 0 | 393,338 | 1,279 | 冷启动,首次构建前缀 |
| #5 | +26s | 27,127 | 367,325 | 2,828 | 并行分支,距 #3 仅 26s(远未到 5min TTL) |
| #9 | +46s | 394,452 | 3,266 | 7,927 | 命中,40 万走 0.1× |
| #16 | +82s | 397,718 | 7,979 | 131 | 命中 |

社区报告了更严重的同类问题:命中正常、却在**稳态下持续多写**本应按 0.1× 读的内容——
[claude-agent-sdk #311](https://github.com/anthropics/claude-agent-sdk-typescript/issues/311)
(空内容轮仍报约 65K `cache_creation`)、
[claude-code #46917](https://github.com/anthropics/claude-code/issues/46917)
(v2.1.100+ 服务端每请求多计约 20K `cache_creation`,载荷字节更少却计费更多)。

---

## 四、描述 vs 实际:缓存 TTL 与计费沟通

### 1 小时缓存默认值被静默改为 5 分钟

Anthropic 自 2026 年 4 月初起,将 Claude Code 默认 prompt 缓存 TTL 从 1 小时降为 5 分钟,**未作公告**,
文档被静默更新为"5 分钟为默认"。该变更对不同用户在不同日期生效。更短的 TTL 意味着会话稍有空闲即缓存
过期,续接时整段前缀须按 1.25× 重建。
([XDA 报道](https://www.xda-developers.com/anthropic-quietly-nerfed-claude-code-hour-cache-token-budget/))

### 缓存失效类注入:文档前提与实际行为不一致

官方缓存文档的前提是"前缀字节稳定即命中";但社区调查指出 Claude Code 会在**可缓存前缀内注入逐请求
变化的内容**,导致前缀哈希失配、命中率下降、token 消耗上升:

| 注入面 | 机制 | 证实程度 |
|--------|------|----------|
| attestation 反滥用数据 | 每请求不同,落入可缓存前缀 → 哈希变化 → 旧缓存被弃 | 社区分析 |
| `<system-reminder>` 块 | 续接时位置漂移,改变 `messages[0]` 结构,使后续前缀失配 | **官方 issue** |
| microcompact sentinel 含时间戳 | 二次压缩对同一位置写入不同字节 | 社区分析 |
| billing sentinel 字符串替换 | 历史含特定词时替换打偏位置,触发未缓存重建 | 源码逆向(单一来源) |
| `ANTI_DISTILLATION_CC` 假 tool 注入 | 向 system prompt 注入防蒸馏内容 | 源码泄露,未经官方确认 |

> 证实程度需区分:`<system-reminder>` 漂移与续接缓存失效有**官方 issue**;`ANTI_DISTILLATION_CC`、
> billing sentinel 等具体名称来自**源码逆向/泄露的单一来源**,Anthropic 未确认。
>
> 相关 issue:[#43657](https://github.com/anthropics/claude-code/issues/43657)、
> [#42338](https://github.com/anthropics/claude-code/issues/42338)、
> [#40524](https://github.com/anthropics/claude-code/issues/40524)、
> [#27048](https://github.com/anthropics/claude-code/issues/27048);
> 综合分析(含逆向,需审慎):[SmartScope](https://smartscope.blog/en/blog/claude-code-token-consumption-cache-bug/)、
> [claude-code-cache-fix](https://github.com/cnighswonger/claude-code-cache-fix)。

### 额度异常消耗:仅非正式承认

2026-03-23 起,各付费档用户报告额度异常快速耗尽。Anthropic 于 3-31 在社交媒体非正式承认
"额度消耗远快于预期……为最高优先级",但当时**无博客、无邮件、无状态页**通告——这一点在
[issue #41930](https://github.com/anthropics/claude-code/issues/41930) 中被集中批评。后续于 5-06
以提高额度的方式作出补偿。
([The Register](https://www.theregister.com/2026/03/31/anthropic_claude_code_limits/)、
[DevClass](https://www.devclass.com/ai-ml/2026/04/01/anthropic-admits-claude-code-users-hitting-usage-limits-way-faster-than-expected/5213575))

---

## 五、可对照的设计:extended thinking 已被官方文档明确

作为对照,并非所有不直观行为都缺乏披露。抓包中回传的 1,155 个 thinking 块**全部为空文本 + 仅含加密
签名**,这与官方文档一致:
[Building with extended thinking](https://docs.claude.com/en/docs/build-with-claude/extended-thinking)
说明 `thinking.display: "omitted"` 时返回空 `thinking` 字段,完整推理加密封装于 `signature` 中
("full thinking content is encrypted and returned in the signature field")。

含义:推理明文对客户端不可见(反蒸馏/安全设计);且后续轮次回传的仅是约 1.6KB 的签名,**推理文本本身
不会作为输入被反复计费**——占缓存读大头的是会话与工具内容,而非 thinking 文本。此项行为有明确文档,
属于"已充分披露"的正面对照。

---

## 六、本分析发现 ↔ 社区 issue 对照

| 发现 | 对应 issue |
|------|------|
| 后台自动请求拖满前缀、消耗大量 token | [#12243](https://github.com/anthropics/claude-code/issues/12243)、[#52502](https://github.com/anthropics/claude-code/issues/52502) |
| 固定前缀(CLAUDE.md/system)每请求按 0.1× 重读 | [#48896](https://github.com/anthropics/claude-code/issues/48896) |
| 命中正常却持续按 1.25× 多写 | [#311](https://github.com/anthropics/claude-agent-sdk-typescript/issues/311)、[#46917](https://github.com/anthropics/claude-code/issues/46917) |
| 注入内容打掉缓存前缀 | [#43657](https://github.com/anthropics/claude-code/issues/43657)、[#42338](https://github.com/anthropics/claude-code/issues/42338)、[#40524](https://github.com/anthropics/claude-code/issues/40524)、[#27048](https://github.com/anthropics/claude-code/issues/27048) |
| usage 数据未回灌模型 | [#26340](https://github.com/anthropics/claude-code/issues/26340) |
| 额度异常消耗(综合) | [#41930](https://github.com/anthropics/claude-code/issues/41930) |

---

## 七、全样本计费汇总(133 条)

| 项 | token | 成本(USD) |
|----|------:|--------:|
| 输入 | 646,967 | \$3.23 |
| 缓存读(0.1×) | 11,123,064 | \$5.56 |
| 缓存写(1.25×) | 1,312,097 | \$8.20 |
| 输出 | 192,313 | \$4.81 |
| **合计** | — | **≈\$21.80** |

---

## 八、缓解建议

- 评估是否需要常开 Auto Mode:其安全分类器会对**每个动作**重读整段历史并计费。
- 缩减常驻前缀(CLAUDE.md、system、tool 定义):它是每次重读/重写的基数。
- 避免制造缓存失效:同一上下文勿并发多会话争用;勿让会话空闲超过 ~5 分钟(当前默认 TTL)。

---

## 附:数据与工具(均已脱敏)

| 文件 | 内容 |
|------|------|
| [`data/capture_sanitized.jsonl`](./data/capture_sanitized.jsonl) | 逐请求的结构 + 计费字段(剥除全部正文、headers、上游地址),133 条 |
| [`data/capture_summary.csv`](./data/capture_summary.csv) | 计费字段汇总 |
| [`tools/`](./tools/) | 抓取与汇总脚本;代理仅转发请求、不记录鉴权信息 |

数据仅含 token 计数与时间戳,不含任何请求正文、鉴权凭据或上游地址。
