# Claude Code Billing & Caching Behavior — An Analysis

*[中文(默认)](./README.md) · English*

A measurement-based technical analysis focused on **the gap between how Claude Code is described and how it
actually bills and caches**.

## Method

A local reverse proxy intercepts every `/v1/messages` request Claude Code emits and parses the response `usage`
fields (`input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens` / `output_tokens`).
The sample is **133 real requests** spanning one continuous session and the background/parallel requests it
triggered. Sanitized data and the capture scripts ship with the repo (`data/`, `tools/`); the data contains no
request bodies, no authentication material, and no upstream address.

**Pricing basis (Anthropic public rates, current Opus tier, USD / 1M tokens):**
input \$5, cache read \$0.50 (0.1×), cache write 5-min \$6.25 (1.25×), cache write 1-hour \$10 (2×), output \$25.
The cache multiplier structure (read 0.1× / 5-min write 1.25× / 1-hour write 2×) is model-independent.
This analysis is about **structure and relative magnitude**; absolute dollar figures scale with unit price.

---

## Key conclusions

1. **Every agent action triggers a safety-classifier request that re-reads the entire conversation history and
   is charged against the user's token usage — a cost disclosed in no official documentation.**
2. **Prompt-cache hits are billed per request (read at 0.1×), not "built once, reused for free"; high-frequency
   small requests continuously amplify this read fee.**
3. **Several material cache/billing changes were communicated after the fact or silently**: the 1-hour cache
   default was quietly cut to 5 minutes; the March 2026 abnormal quota drain was only acknowledged informally on
   social media; cache-invalidation issues have lingered in the tracker.

---

## 1. The Auto Mode safety classifier: re-reads full history per action, billed to the user

### What it is

The capture shows a class of `max_tokens=64` requests producing only a very short verdict, whose system prompt
begins:

> *"You are a **security monitor for autonomous AI coding agents** … Your job is to evaluate whether the agent's
> latest action should be **blocked**."*

This is Claude Code's **Auto Mode** safety classifier, tracked publicly at
[Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts) and referenced
across the official [changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md). Its job: before
each agent action, feed the **entire transcript** plus ~40K characters of safety rules (threat model covering
prompt injection, scope creep, deletion/exfiltration) to the model and emit an ALLOW/BLOCK verdict.

One changelog entry maps directly onto the capture — *"Fixed auto mode incorrectly blocking actions …
**when the safety classifier ran out of output tokens** while reasoning"* — matching the `max_tokens=64`
short-verdict requests seen here; see also
[issue #39259](https://github.com/anthropics/claude-code/issues/39259) (classifier blocking read-only ops when
unavailable).

### Measured cost

| Metric | Value |
|--------|------|
| Safety-classifier requests | **41** |
| Cumulative `cache_read` | **2,101,257 tokens** |
| Cumulative `output` | **1,423 tokens** |
| read : output ratio | **≈ 1,476 : 1** |
| Share of all cache reads in the sample | **≈ 19%** |
| Cache-read cost of this class | ≈ \$1.05 |

In other words: **to produce ~1,400 verdict tokens, the system re-read 2.1M tokens of conversation history**,
accounting for nearly a fifth of all cache reads in the sample. Each such request carries the full safety-rules
prompt plus the entire conversation history (single reads routinely in the 120K-token range).

### The discrepancy

- **Anthropic describes Auto Mode as a safety capability** (see
  [Building safeguards for Claude](https://www.anthropic.com/news/building-safeguards-for-claude) and the
  changelog); its protective value is real — these are not pointless requests.
- **But no documentation states that this safety check runs on the user's token budget**, nor that it
  **re-reads the entire conversation history on every action**. For metered or quota-capped users this is a
  substantial, invisible fixed overhead.

---

## 2. Cache hits are billed per request; high-frequency small requests amplify it

The Messages API is stateless: every request must resend the full context. On a cache hit the repeated prefix is
billed as `cache_read` (0.1× base input), **and that fee is charged on every request, not "built once then free."**
The [official caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) define this, but
there is a gap with the intuitive expectation (a continuous session = server-retained state that should not be
re-billed):

- There is no server-side "session" as a billing entity; what feels like "the session never dropped" equals
  "the cache is still warm," priced at 0.1× per read.
- Consequently, **any high-frequency, low-output request that carries the full prefix (the safety classifier
  above, or each tool-call hop) repeatedly triggers a read fee on 100K+ tokens.** The more requests, the higher
  the cumulative read fee.

Across the sample, cache reads total **11.12M tokens**, ~a quarter of total cost, of which ~19% comes from the
safety-classifier class of background requests.

---

## 3. Cache misses: cold starts and parallel branches trigger 1.25× full rewrites

On a miss the whole prefix is rewritten as `cache_creation` (5-min write 1.25×), priced above base input. The
large rewrites in the sample are all explicable as **cold starts** (first build of that prefix) or **parallel
branches** (concurrent subtasks each building their own cache), not eviction of a warm cache:

| seq | gap | cache_read | cache_creation (1.25×) | output | verdict |
|----:|----:|---------:|---------------------:|------:|---------|
| #3 | — | 0 | 393,338 | 1,279 | cold start, first build of the prefix |
| #5 | +26s | 27,127 | 367,325 | 2,828 | parallel branch, only 26s after #3 (far under 5-min TTL) |
| #9 | +46s | 394,452 | 3,266 | 7,927 | hit, 400K at 0.1× |
| #16 | +82s | 397,718 | 7,979 | 131 | hit |

The community reports a worse variant: hits work, yet content that should read at 0.1× is **continuously
rewritten in steady state** —
[claude-agent-sdk #311](https://github.com/anthropics/claude-agent-sdk-typescript/issues/311)
(null-content turns still report ~65K `cache_creation`) and
[claude-code #46917](https://github.com/anthropics/claude-code/issues/46917)
(v2.1.100+ bills ~20K more `cache_creation` per request server-side, despite fewer payload bytes).

---

## 4. Description vs. reality: cache TTL and billing communication

### The 1-hour cache default was silently cut to 5 minutes

From early April 2026, Anthropic reduced Claude Code's default prompt-cache TTL from 1 hour to 5 minutes **with no
announcement**; the docs were quietly updated to state "5 minutes is the default." The change reached different
users on different days. A shorter TTL means a brief idle expires the cache, forcing a 1.25× rebuild of the whole
prefix on continuation.
([XDA](https://www.xda-developers.com/anthropic-quietly-nerfed-claude-code-hour-cache-token-budget/))

### Cache-invalidating injection: the doc premise vs. actual behavior

The official caching docs premise hits on "byte-stable prefix"; community investigation finds Claude Code injects
**per-request varying content inside the cacheable prefix**, causing prefix-hash mismatches, lower hit rates, and
higher token consumption:

| Injection surface | Mechanism | Confidence |
|--------|------|----------|
| attestation anti-abuse data | differs per request, sits in the cacheable prefix → hash changes → old cache discarded | community analysis |
| `<system-reminder>` block | shifts position on resume, changing `messages[0]` structure and invalidating the following prefix | **official issue** |
| microcompact sentinel with timestamp | a second compaction writes different bytes at the same position | community analysis |
| billing-sentinel string replacement | when history contains certain terms, replacement hits the wrong position, forcing an uncached rebuild | source reverse-engineering (single source) |
| `ANTI_DISTILLATION_CC` fake-tool injection | injects anti-distillation content into the system prompt | source leak, unconfirmed by Anthropic |

> Confidence varies: `<system-reminder>` drift and resume cache invalidation have **official issues**;
> `ANTI_DISTILLATION_CC` and the billing sentinel come from a **single reverse-engineering/leak source** and are
> unconfirmed by Anthropic.
>
> Related issues: [#43657](https://github.com/anthropics/claude-code/issues/43657),
> [#42338](https://github.com/anthropics/claude-code/issues/42338),
> [#40524](https://github.com/anthropics/claude-code/issues/40524),
> [#27048](https://github.com/anthropics/claude-code/issues/27048);
> combined analysis (incl. reverse-engineering, treat with care):
> [SmartScope](https://smartscope.blog/en/blog/claude-code-token-consumption-cache-bug/),
> [claude-code-cache-fix](https://github.com/cnighswonger/claude-code-cache-fix).

### Abnormal quota drain: only informally acknowledged

From 2026-03-23, users across paid tiers reported abnormally fast quota exhaustion. Anthropic acknowledged it
informally on social media on 03-31 ("limits consumed far faster than expected… top priority"), but with **no
blog post, email, or status-page notice** at the time — a point criticized in
[issue #41930](https://github.com/anthropics/claude-code/issues/41930). Compensation followed on 05-06 via higher
limits.
([The Register](https://www.theregister.com/2026/03/31/anthropic_claude_code_limits/),
[DevClass](https://www.devclass.com/ai-ml/2026/04/01/anthropic-admits-claude-code-users-hitting-usage-limits-way-faster-than-expected/5213575))

---

## 5. A counter-example: extended thinking is clearly documented

For balance, not every non-obvious behavior is undisclosed. The 1,155 thinking blocks fed back in the capture are
**all empty-text plus an encrypted signature only**, consistent with the docs:
[Building with extended thinking](https://docs.claude.com/en/docs/build-with-claude/extended-thinking) states that
under `thinking.display: "omitted"` the `thinking` field is returned empty, with the full reasoning encrypted in
the `signature` field ("full thinking content is encrypted and returned in the signature field").

Implication: reasoning plaintext is not visible to the client (an anti-distillation/safety design), and subsequent
turns only resend the ~1.6KB signature, so **the reasoning text itself is not repeatedly billed as input** — the
bulk of cache reads is conversation and tool content, not thinking text. This behavior is clearly documented and
serves as a positive, well-disclosed counter-example.

---

## 6. Findings ↔ community issues

| Finding | Issue |
|------|------|
| Background auto-requests carry full prefix, consume large token counts | [#12243](https://github.com/anthropics/claude-code/issues/12243), [#52502](https://github.com/anthropics/claude-code/issues/52502) |
| Fixed prefix (CLAUDE.md/system) re-read at 0.1× per request | [#48896](https://github.com/anthropics/claude-code/issues/48896) |
| Hits work yet content is rewritten at 1.25× in steady state | [#311](https://github.com/anthropics/claude-agent-sdk-typescript/issues/311), [#46917](https://github.com/anthropics/claude-code/issues/46917) |
| Injected content breaks the cache prefix | [#43657](https://github.com/anthropics/claude-code/issues/43657), [#42338](https://github.com/anthropics/claude-code/issues/42338), [#40524](https://github.com/anthropics/claude-code/issues/40524), [#27048](https://github.com/anthropics/claude-code/issues/27048) |
| usage data not fed back to the model | [#26340](https://github.com/anthropics/claude-code/issues/26340) |
| Abnormal quota drain (umbrella) | [#41930](https://github.com/anthropics/claude-code/issues/41930) |

---

## 7. Sample billing totals (133 requests)

| Item | tokens | cost (USD) |
|----|------:|--------:|
| input | 646,967 | \$3.23 |
| cache read (0.1×) | 11,123,064 | \$5.56 |
| cache write (1.25×) | 1,312,097 | \$8.20 |
| output | 192,313 | \$4.81 |
| **total** | — | **≈\$21.80** |

---

## 8. Mitigations

- Reconsider keeping Auto Mode always on: its safety classifier re-reads the full history and bills **every action**.
- Shrink the resident prefix (CLAUDE.md, system, tool definitions): it is the base for every re-read/rewrite.
- Avoid manufacturing cache misses: don't run concurrent sessions contending for the same context; don't leave a
  session idle beyond ~5 minutes (the current default TTL).

---

## Appendix: data & tools (sanitized)

| File | Contents |
|------|------|
| [`data/capture_sanitized.jsonl`](./data/capture_sanitized.jsonl) | per-request structure + billing fields (bodies, headers, upstream removed), 133 rows |
| [`data/capture_summary.csv`](./data/capture_summary.csv) | billing-field summary |
| [`tools/`](./tools/) | capture and summarization scripts; the proxy only forwards requests and records no auth material |

The data contains only token counts and timestamps — no request bodies, credentials, or upstream addresses.
