# 真实模型 eval 协议（V0.5）

默认 `pytest` 仍用预定工具路径（faux），只证明链路。本协议用**本机密钥**跑真实 Pi，看模型会不会自己取证。未跑过、或金标未通过时，不要把产品说成已经会审论证。

密钥和授权真稿不进仓库，也不要贴进对话。

## 金标（仓库内模拟稿）

先在窗口「模型设置」填写密钥，或把 `THESIS_API_KEY` / `OPENAI_API_KEY` 放进环境变量。然后：

```bash
python -m thesis_review.cli eval --out artifacts/eval
```

读取 `%APPDATA%\ThesisReviewAgent\settings.json`（可用 `--home` 覆盖）。sqlite 写在 `--out/workspace`，不改老师日常历史库。`pi-request.json` 不含密钥。每案超时约 10 分钟。

| 案名 | 稿件 | 通过条件 |
| --- | --- | --- |
| overclaim | 结论写「显著提升」，结果只有微弱准确率、无基线/无显著性 | 至少一条 `source=argument`；在 `record_argument_finding` 之前有 `list_outline` 以及 `read_section` / `read_paragraphs` / `find_text` 之一 |
| abandon | 主张有基线和显著性 | 无 argument；有 `commit_review`；提交前同样有导航 |
| history | 导入并确认演示历史后审仍含「非常非常有效」的新稿 | 至少一条 `source=history` |

stdout 只打案名、通过与否、工具名、来源计数。`artifacts/eval/summary.json` 同样不含密钥和原文。失败请看 `reasons`：Pi 崩溃、超时、无导航直接记录、overclaim 漏批、supported 误批、历史该批未批。

通过 = 当前配置的那个模型在这三道题上像 Agent。不过则先改 prompt、导航预算或停止条件，不要加新的 reviewer。

## V0.7 新增可测项（真实模型评测时顺带记录）

- **章节覆盖**：`<draft>-trace.json` 的 `coverage` 字段与 session `quality.chapter_coverage` 直接给出每章 read / probed / unread；对照金标案核对该读的章是否真的读了。
- **思考深度对比**：窗口「模型设置」里的思考深度（off/low/medium/high）写入 `pi-request.json` 与 session 快照，可对同一金标做多档 paired comparison（成本 vs 采用率）。
- **召回方法**：`semantic_history_candidates` 的候选带 `method`（ngram/hybrid）；history 案可分别统计两种方法在改写场景下的召回差异。仍然只有 `confirm_history_finding` 之后才算复犯候选。

## 授权真稿（可选，仅本机）

1. 把授权稿放到 `artifacts/real/`（目录已被 gitignore）。
2. 复制 [eval-real.example.json](eval-real.example.json) 为 `artifacts/real/cases.json`，改路径和期望。
3. 运行：

```bash
python -m thesis_review.cli eval --out artifacts/eval --manifest artifacts/real/cases.json
```

清单里 `expect_argument` / `expect_history` 只是粗标签。summary 只记案名、是否通过、工具序列、finding 条数和 source。不要把学生原文写进任何会提交的文件。
