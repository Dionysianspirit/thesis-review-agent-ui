# 真实模型 eval 协议

默认 `pytest` 仍用预定工具路径（faux），只证明链路。本协议覆盖两条互补的评测线：

1. **金标链路评测**（V0.5 引入）：用本机密钥跑真实 Pi，看模型会不会自己取证；
2. **真实评测闭环**（V0.8 引入）：让真实老师 + 真实论文的每一次审稿沉淀为可复查的数据，回答「AI 第一遍到底审得好不好」。

密钥和授权真稿不进仓库，也不要贴进对话。

---

## 一、真实评测闭环（V0.8）

### 操作流程

1. 正常走完一次审稿：AI 初审 → 老师逐条决定（确认 / 编辑后确认 / 驳回）。
2. 驳回时（或之后在「已驳回」标签里）**可选**补填驳回原因与备注；不填不影响任何流程。
3. 把老师额外发现、但 AI 没报的重要问题在「AI 漏检补录」卡片登记（仅评测数据，不进入正式 Word）。
4. 在「导出」阶段的「本次审稿评测」面板核对计数，点「导出评测数据」，得到三个文件（写入结果文件夹）：
   - `review-eval.json` — 全量结构化数据；
   - `review-eval-findings.csv` — 每行一条记录（AI 候选 + `teacher_missed_issue` 行）；
   - `review-eval-summary.csv` — 汇总指标纵表。
5. 累积多篇后按 `model_snapshot.reasoning`、`chapter_observations` 等字段做横向比较。

### 指标定义（代码与 UI 统一口径）

| 指标 | 定义 |
| --- | --- |
| 已处理意见（processed） | `accepted + edited_accepted + rejected` |
| 采用（adopted） | `accepted + edited_accepted` |
| 已处理意见采用率 | `adopted / processed` |
| 直接 / 编辑后采用率 | `accepted / processed`、`edited_accepted / processed` |
| 驳回率 | `rejected / processed` |

- **`pending` 不进入任何比率的分母**：未决定的意见不构成质量信号。
- 没有已处理意见时所有比率为 `null`（未知），不是 0%。
- 分类沿用现有 `kind` + `subtype` 体系（格式 / 语言 / 内容（论证、数据一致性、方法、实验、结构、术语、引用）/ 历史复查 / 外部核验）。
- **采用率是老师判断下的数字，不是模型准确率。** 没有独立金标前，本系统不产出 precision / recall，也不要在汇报里这样称呼。
- 漏检补录只度量「老师发现的、AI 没报的」，是 recall 的近似下界，不等于完整 recall。

### review-eval.json 结构

```json
{
  "schema_version": "0.8",
  "session_id": "…",
  "generated_at": "…",
  "document": {"draft_id": "…", "file_name": "…"},
  "participants": {"teacher_id": "…", "student_id": "…"},
  "model_snapshot": {"provider": "…", "model": "…", "reasoning": "…", "api_key_set": true},
  "timing": {"review_started_at": "…", "review_completed_at": "…", "duration_s": 522.4},
  "token_usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0} | null,
  "chapter_coverage": {"read": 0, "probed": 0, "unread": 0, "total": 0},
  "chapter_observations": [{"chapter": "…", "coverage_status": "read|probed|unread", "ai_candidates": 0, "accepted": 0, "edited_accepted": 0, "rejected": 0, "teacher_missed_issues": 0}],
  "metrics": {"candidate_count": 0, "accepted_count": 0, "edited_accepted_count": 0, "rejected_count": 0, "pending_count": 0, "processed_count": 0, "adopted_count": 0, "acceptance_rate": null, "direct_acceptance_rate": null, "edited_acceptance_rate": null, "rejection_rate": null, "missed_issue_count": 0},
  "category_metrics": [{"category": "…", "subtype": "…", "candidate_count": 0, "accepted": 0, "edited_accepted": 0, "rejected": 0, "pending": 0, "acceptance_rate": null}],
  "rejection_reasons": {"false_positive": 0, "…": 0, "unclassified": 0},
  "findings": [{"id": "…", "kind": "…", "subtype": "…", "problem": "…", "quote": "…", "teacher_decision": "…", "rejection_reason": "…", "teacher_edited": false, "…": "…"}],
  "teacher_missed_issues": [{"id": "…", "section": "…", "category": "…", "problem": "…", "note": "…", "source": "teacher_missed_issue"}]
}
```

### 隐私边界

- 导出只写入本机结果文件夹；不上传、无遥测、无后台同步。
- 不包含：API Key / 密钥、老师与学生真实姓名、论文全文、evidence 全量文本。
- finding 只保留问题描述、理由与最小必要原文引用；模型快照只含 provider / model / reasoning / 是否配置密钥。
- `token_usage` 只有真实 API 返回时才记录；faux 链路一律不记，服务商未返回时为 `null`——**unknown 优于假数字**。
- `chapter_observations` 只记录事实（各章候选数 / 采用数 / 驳回数 / 漏检数），不自动下因果结论；漏检补录的位置是自由文本，只有与章节标题明显重叠才归章，否则不强行匹配。

---

## 二、金标链路评测（V0.5 起）

### 金标（仓库内模拟稿）

先在窗口「模型设置」填写密钥，或把 `THESIS_API_KEY` / `OPENAI_API_KEY` 放进环境变量。然后：

```bash
python -m thesis_review.cli eval --out artifacts/eval
```

读取 `%APPDATA%\ThesisReviewAgent\settings.json`（可用 `--home` 覆盖）。sqlite 写在 `--out/workspace`，不改老师日常历史库。`pi-request.json` 不含密钥。每案超时约 10 分钟。| 案名 | 稿件 | 通过条件 |
| --- | --- | --- |
| overclaim | 结论写「显著提升」，结果只有微弱准确率、无基线/无显著性 | 至少一条 `source=argument`；在 `record_argument_finding` 之前有 `list_outline` 以及 `read_section` / `read_paragraphs` / `find_text` 之一 |
| abandon | 主张有基线和显著性 | 无 argument；有 `commit_review`；提交前同样有导航 |
| history | 导入并确认演示历史后审仍含「非常非常有效」的新稿 | 至少一条 `source=history` |

stdout 只打案名、通过与否、工具名、来源计数。`artifacts/eval/summary.json` 同样不含密钥和原文。失败请看 `reasons`：Pi 崩溃、超时、无导航直接记录、overclaim 漏批、supported 误批、历史该批未批。

通过 = 当前配置的那个模型在这三道题上像 Agent。不过则先改 prompt、导航预算或停止条件，不要加新的 reviewer。

### V0.7 新增可测项（真实模型评测时顺带记录）

- **章节覆盖**：`<draft>-trace.json` 的 `coverage` 字段与 session `quality.chapter_coverage` 直接给出每章 read / probed / unread；对照金标案核对该读的章是否真的读了。
- **思考深度对比**：窗口「模型设置」里的思考深度（off/low/medium/high）写入 `pi-request.json` 与 session 快照，可对同一金标做多档 paired comparison；V0.8 起 `review-eval.json` 的 `token_usage` 与 `timing` 让成本对比有了真实数字（faux 不记、拿不到为 null）。
- **召回方法**：`semantic_history_candidates` 的候选带 `method`（ngram/hybrid）；history 案可分别统计两种方法在改写场景下的召回差异。仍然只有 `confirm_history_finding` 之后才算复犯候选。

### 授权真稿（可选，仅本机）

1. 把授权稿放到 `artifacts/real/`（目录已被 gitignore）。
2. 复制 [eval-real.example.json](eval-real.example.json) 为 `artifacts/real/cases.json`，改路径和期望。
3. 运行：

```bash
python -m thesis_review.cli eval --out artifacts/eval --manifest artifacts/real/cases.json
```

清单里 `expect_argument` / `expect_history` 只是粗标签。summary 只记案名、是否通过、工具序列、finding 条数和 source。不要把学生原文写进任何会提交的文件。
