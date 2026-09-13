const $ = (id) => document.getElementById(id);

const hasBridge = () => Boolean(window.pywebview && window.pywebview.api);

async function api(name, ...args) {
  return window.pywebview.api[name](...args);
}

function log(message, cls) {
  const node = $("log");
  node.textContent = message;
  node.className = "live-message serif" + (cls ? " " + cls : "");
}

function setStatus(text, state) {
  $("status-text").textContent = text;
  $("status-dot").className = "status-dot " + (state || "idle");
}

function esc(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

const KIND_META = {
  format: { label: "格式", chip: "格式" },
  language: { label: "语言", chip: "语言" },
  content: { label: "内容", chip: "内容" },
  history: { label: "历史", chip: "历史复查" },
  external: { label: "外部核验", chip: "外部核验" },
};

const DECISION_LABELS = {
  pending: "待处理",
  accepted: "已确认",
  edited_accepted: "已修改确认",
  rejected: "已驳回",
};

// V0.8 rejection reasons: eval-only annotation, never shown to students.
const REJECTION_REASONS = [
  ["false_positive", "判断错误 / 不成立"],
  ["duplicate", "与已有意见重复"],
  ["not_important", "成立但不值得作为正式意见"],
  ["insufficient_evidence", "依据不足"],
  ["bad_suggestion", "问题可能存在，但修改建议不合适"],
  ["already_resolved", "论文当前已解决"],
  ["other", "其他"],
];
const REJECTION_LABELS = Object.fromEntries(REJECTION_REASONS);

const APPLY_LABELS = { comment: "拟写批注", revision: "拟写修订", both: "拟写批注 + 修订" };

const EVIDENCE_LABELS = {
  history: "历次批注",
  history_span: "旧稿原文",
  claim: "论文原文",
  evidence: "对照证据",
  counter: "反证",
  rule: "规则依据",
};

const STATUS_LABELS = { candidate: "待确认", confirmed: "已确认", disabled: "已停用" };

function kindOf(finding) {
  if (finding.kind) return finding.kind;
  if (finding.source === "history") return "history";
  if (finding.source === "external") return "external";
  if (finding.source === "argument") return "content";
  return finding.category === "C" ? "format" : finding.category === "B" ? "content" : "language";
}

function decisionOf(finding) {
  return finding.teacher_decision || "pending";
}

function renderIssues(issues) {
  const box = $("issues");
  box.innerHTML = "";
  if (!issues.length) {
    box.className = "issues empty";
    box.textContent = "还没有历史问题。需要时再导入历史稿。";
    return;
  }
  box.className = "issues";
  for (const item of issues) {
    const label = document.createElement("label");
    label.className = "issue";
    const checked = item.status === "confirmed" ? "checked" : "";
    const statusCls = item.status === "confirmed" ? "confirmed" : item.status === "disabled" ? "disabled" : "";
    const title = item.problem || item.original_text || "（无摘要）";
    const kind = item.issue_type || item.category || "";
    const scope = item.scope ? ` · ${item.scope}` : "";
    const span = item.original_span ? ` · 原文：「${item.original_span}」` : "";
    const intent = item.teacher_intent || item.original_text || "";
    label.innerHTML = `
      <input type="checkbox" data-id="${esc(item.id)}" ${checked}>
      <div class="issue-body">
        <span class="issue-title">${esc(title)}<span class="issue-status ${statusCls}">${esc(STATUS_LABELS[item.status] || item.status)}</span></span>
        <span class="issue-meta">${esc(kind)}${esc(scope)}${esc(span)}</span>
        <span class="issue-intent">${esc(intent)}</span>
      </div>`;
    box.appendChild(label);
  }
  box.querySelectorAll("input[type=checkbox]").forEach((input) => {
    input.addEventListener("change", async () => {
      if (!hasBridge()) return;
      await api("set_issue", input.dataset.id, input.checked);
      await refresh();
    });
  });
}

function renderDrafts(drafts) {
  const box = $("history-drafts");
  if (!drafts || !drafts.length) {
    box.className = "draft-list empty";
    box.textContent = "还没有已保存的历史稿记录。";
    return;
  }
  box.className = "draft-list";
  box.innerHTML = drafts.map((item) => {
    const access = item.accessible ? "文件可访问" : "原文件找不到，已提取问题仍保留";
    return `<div class="draft-item"><strong>${esc(item.draft_id)}</strong><span class="mono">${esc(item.path || "（无路径）")}</span><span>${esc(access)} · ${item.issue_count || 0} 条问题</span></div>`;
  }).join("");
}

function renderSessions(sessions) {
  const box = $("sessions");
  if (!sessions || !sessions.length) {
    box.className = "session-list empty";
    box.textContent = "还没有审稿会话。";
    return;
  }
  box.className = "session-list";
  box.innerHTML = sessions.map((item) => {
    const label = item.completed ? "已完成" : (item.status === "failed" ? "初审失败" : "未完成");
    return `<button class="session-item" type="button" data-id="${esc(item.id)}"><span>${esc(item.student_id)} · ${esc(item.draft_id)}</span><span class="mono">${esc(label)}</span></button>`;
  }).join("");
  box.querySelectorAll("button[data-id]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!hasBridge()) return;
      await api("resume_session", btn.dataset.id);
      await refresh();
    });
  });
}

let lastFindings = [];
let lastRecall = { confirmed: 0, recalled: 0, written: 0, skipped: [], absent: [] };
let lastStats = { ai_candidates: 0, accepted: 0, edited_accepted: 0, rejected: 0, pending: 0, formal: 0 };
let decisionTab = "pending";
let kindTab = "all";

function findingCard(finding) {
  const kind = kindOf(finding);
  const meta = KIND_META[kind] || KIND_META.content;
  const decision = decisionOf(finding);
  const loc = finding.section
    ? `${finding.section} · ${finding.anchor || ""}`
    : (finding.paragraph_index ? `第 ${finding.paragraph_index} 段 · ${finding.anchor || ""}` : (finding.anchor || ""));
  const quote = finding.quote ? `<blockquote class="quote">「${esc(finding.quote)}」</blockquote>` : "";
  const evidence = (finding.evidence || []).map((item) => {
    const evKind = EVIDENCE_LABELS[item.kind] || item.kind || "依据";
    const draft = item.draft_id ? ` · ${item.draft_id}` : "";
    return `<div class="ev-item"><span class="ev-kind">${esc(evKind)}${esc(draft)}</span><span>${esc(item.text)}</span></div>`;
  }).join("");
  const sources = (finding.external_sources || []).map((item) => {
    return `<div class="ev-item"><span class="ev-kind">外部来源</span><span>${esc(item.title || "")} ${esc(item.url || "")}</span></div>`;
  }).join("");
  const history = (finding.history_refs || []).length
    ? `<div class="ev-item"><span class="ev-kind">历史参考</span><span>${esc((finding.history_refs || []).join("，"))}</span></div>`
    : "";
  const original = finding.original_problem && finding.original_problem !== finding.problem
    ? `<p class="rationale">AI 原意见：${esc(finding.original_problem)}</p>`
    : "";
  const teacherText = finding.teacher_final_text
    ? `<p class="rationale">老师最终文本：${esc(finding.teacher_final_text)}</p>`
    : "";
  const teacherNew = finding.teacher_final_new
    ? `<p class="rationale">老师修订替换文本：${esc(finding.teacher_final_new)}</p>`
    : "";
  const suggest = finding.suggested_old && finding.suggested_new
    ? `<div class="suggest">建议将 <span class="old">「${esc(finding.suggested_old)}」</span> 改为 <span class="new">「${esc(finding.suggested_new)}」</span></div>`
    : (finding.suggested_action ? `<div class="suggest">${esc(finding.suggested_action)}</div>` : "");
  const subtype = finding.subtype ? ` · ${finding.subtype}` : "";
  const editValue = finding.teacher_final_text || finding.problem || "";
  const pending = decision === "pending";
  // V0.7: the comment body and the tracked-revision replacement are edited
  // separately; the revision box only appears when a replacement exists.
  const hasRevision = Boolean(finding.suggested_old && finding.suggested_new);
  const revisionValue = finding.teacher_final_new || finding.suggested_new || "";
  const actions = pending ? `
      <div class="finding-actions">
        <button class="btn btn-dark" data-act="accepted" type="button">确认</button>
        <button class="btn btn-ghost" data-act="rejected" type="button">驳回</button>
        <button class="btn btn-ghost" data-act="edited_accepted" type="button">编辑后确认</button>
      </div>
      <div class="edit-block">
        <span class="edit-label">老师最终批注意见</span>
        <textarea class="edit-box" placeholder="老师最终审稿意见">${esc(editValue)}</textarea>
        ${hasRevision ? `
        <span class="edit-label">修订替换文本（进入 Word 修订，留空使用建议替换）</span>
        <textarea class="edit-box edit-box-new" placeholder="老师最终替换文本">${esc(revisionValue)}</textarea>` : ""}
      </div>` : "";
  // V0.8: optional rejection reason on rejected cards — eval data only.
  const rejectionEditor = decision === "rejected" ? `
      <div class="rejection-editor">
        <span class="edit-label">驳回原因（可选，仅用于评测统计）</span>
        <div class="rejection-row">
          <select class="reject-reason">
            <option value="">未分类</option>
            ${REJECTION_REASONS.map(([value, label]) => `<option value="${esc(value)}" ${finding.rejection_reason === value ? "selected" : ""}>${esc(label)}</option>`).join("")}
          </select>
          <button class="btn btn-ghost" data-act="save-reason" type="button">保存原因</button>
        </div>
        <textarea class="edit-box reject-note" placeholder="补充说明（可选）">${esc(finding.rejection_note || "")}</textarea>
      </div>` : "";
  return `
    <article class="finding decision-${esc(decision)}" data-kind="${esc(kind)}" data-decision="${esc(decision)}" data-id="${esc(finding.id)}">
      <div class="finding-head">
        <span class="chip">${esc(meta.chip)}${esc(subtype)}</span>
        <span class="loc">${esc(loc)}</span>
        <span class="apply-badge">${esc(DECISION_LABELS[decision] || decision)}</span>
      </div>
      <h4>${esc(finding.problem)}</h4>
      <p class="rationale">${esc(finding.rationale)}</p>
      ${original}
      ${quote}
      ${evidence || sources || history ? `<div class="evidence">${evidence}${sources}${history}</div>` : ""}
      ${suggest}
      ${teacherText}
      ${teacherNew}
      <p class="source-line mono">来源：${esc(finding.source || "")} · ${esc(APPLY_LABELS[finding.apply] || "拟写批注")}</p>
      ${actions}
      ${rejectionEditor}
    </article>`;
}

function renderResults(findings, recall, usedModel, warning, stats) {
  lastFindings = findings || [];
  lastRecall = recall || lastRecall;
  lastStats = stats || countsFrom(lastFindings);
  const has = lastFindings.length || (lastRecall.skipped || []).length || (lastRecall.absent || []).length;
  $("results-empty").hidden = Boolean(has);
  $("results").hidden = !has;
  $("count-pending").textContent = lastStats.pending || 0;
  $("count-accepted").textContent = lastStats.accepted || 0;
  $("count-edited").textContent = lastStats.edited_accepted || 0;
  $("count-rejected").textContent = lastStats.rejected || 0;
  $("count-all").textContent = lastStats.ai_candidates || lastFindings.length;
  const badge = $("mode-badge");
  badge.textContent = usedModel ? "模型辅助" : "离线规则";
  badge.className = "mode-badge mono" + (usedModel ? " model" : "");
  $("recall-line").textContent =
    `已确认历史问题 ${lastRecall.confirmed || 0} · 召回 ${lastRecall.recalled || 0} · 写成候选 ${lastRecall.written || 0}`;
  const banner = $("warning-banner");
  if (warning) {
    banner.textContent = warning;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
  renderExportStats(lastStats);
  if (!has) return;
  renderTab();
}

function countsFrom(findings) {
  const stats = { ai_candidates: findings.length, accepted: 0, edited_accepted: 0, rejected: 0, pending: 0, formal: 0 };
  for (const item of findings) {
    const decision = decisionOf(item);
    stats[decision] = (stats[decision] || 0) + 1;
  }
  stats.formal = (stats.accepted || 0) + (stats.edited_accepted || 0);
  return stats;
}

function renderExportStats(stats) {
  const current = stats || lastStats;
  $("export-stats").textContent =
    `AI 候选：${current.ai_candidates || 0}　老师确认：${current.accepted || 0}　编辑后确认：${current.edited_accepted || 0}　驳回：${current.rejected || 0}　未处理：${current.pending || 0}　正式采用：${current.formal || 0}`;
  const formal = (current.accepted || 0) + (current.edited_accepted || 0);
  $("btn-export").disabled = formal === 0 || (current.pending || 0) > 0;
  $("btn-export-confirmed").disabled = formal === 0;
}

function renderTab() {
  const box = $("findings");
  const selected = lastFindings.filter((item) => {
    const decision = decisionOf(item);
    const kind = kindOf(item);
    if (decision !== decisionTab) return false;
    if (kindTab !== "all" && kind !== kindTab) return false;
    return true;
  });
  if (!selected.length) {
    box.innerHTML = `<div class="card placeholder">该筛选下没有候选意见。</div>`;
    return;
  }
  box.innerHTML = selected.map(findingCard).join("");
  box.querySelectorAll(".finding").forEach((card) => {
    card.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.act === "save-reason") {
          const reasonBox = card.querySelector(".reject-reason");
          const noteBox = card.querySelector(".reject-note");
          onSaveRejection(
            card.dataset.id,
            reasonBox ? reasonBox.value : "",
            noteBox ? noteBox.value : "",
          );
          return;
        }
        const commentBox = card.querySelector(".edit-box");
        const revisionBox = card.querySelector(".edit-box-new");
        onDecide(
          card.dataset.id,
          btn.dataset.act,
          commentBox ? commentBox.value : "",
          revisionBox ? revisionBox.value : "",
        );
      });
    });
  });
}

async function onSaveRejection(id, reason, note) {
  if (!hasBridge()) {
    const item = lastFindings.find((finding) => finding.id === id);
    if (item) {
      item.rejection_reason = reason;
      item.rejection_note = note;
    }
    log("浏览器预览：驳回原因仅在前端标记。", "ok");
    return;
  }
  const result = await api("set_rejection_reason", id, reason || "", note || "");
  if (result && result.ok) {
    log("已保存驳回原因。", "ok");
  } else {
    log((result && result.message) || "未能保存驳回原因。", "err");
  }
}

function resolveDecision(id, decision, editedText, editedNewText) {
  const item = lastFindings.find((finding) => finding.id === id);
  const text = String(editedText == null ? "" : editedText);
  const newText = String(editedNewText == null ? "" : editedNewText);
  if (decision !== "edited_accepted") {
    return { decision, editedText: text, editedNewText: "", error: "" };
  }
  if (!text.trim()) {
    return { decision, editedText: text, editedNewText: "", error: "编辑后确认需要填写老师最终意见。" };
  }
  const original = String((item && (item.teacher_final_text || item.problem)) || "").trim();
  const suggestedNew = String((item && item.suggested_new) || "").trim();
  if (text.trim() === original) {
    return { decision: "accepted", editedText: "", editedNewText: "", error: "" };
  }
  // The revision replacement counts as edited only when the teacher changed
  // it away from the suggestion; otherwise it stays the suggested text.
  const revisedReplacement = newText.trim() && newText.trim() !== suggestedNew ? newText : "";
  return { decision: "edited_accepted", editedText: text, editedNewText: revisedReplacement, error: "" };
}

async function onDecide(id, decision, editedText, editedNewText) {
  const resolved = resolveDecision(id, decision, editedText, editedNewText);
  if (resolved.error) {
    log(resolved.error, "err");
    return;
  }
  decision = resolved.decision;
  editedText = resolved.editedText;
  editedNewText = resolved.editedNewText;
  if (!hasBridge()) {
    const item = lastFindings.find((finding) => finding.id === id);
    if (item) {
      item.teacher_decision = decision;
      if (decision === "edited_accepted") {
        item.teacher_final_text = editedText;
        if (editedNewText) item.teacher_final_new = editedNewText;
      }
      lastStats = countsFrom(lastFindings);
      renderResults(lastFindings, lastRecall, false, "", lastStats);
    }
    return;
  }
  const result = await api("decide_finding", id, decision, editedText || "", editedNewText || "");
  if (result && result.ok) {
    await refresh();
  } else {
    log((result && result.message) || "未能保存老师决定。", "err");
  }
}

$("decision-tabs").addEventListener("click", (event) => {
  const btn = event.target.closest(".tab");
  if (!btn) return;
  decisionTab = btn.dataset.decision;
  document.querySelectorAll("#decision-tabs .tab").forEach((tab) => tab.classList.toggle("active", tab === btn));
  renderTab();
});

$("type-tabs").addEventListener("click", (event) => {
  const btn = event.target.closest(".tab");
  if (!btn) return;
  kindTab = btn.dataset.kind;
  document.querySelectorAll("#type-tabs .tab").forEach((tab) => tab.classList.toggle("active", tab === btn));
  renderTab();
});

function setStage(stage) {
  document.body.classList.remove("stage-prepare", "stage-reviewing", "stage-decide", "stage-export");
  document.body.classList.add("stage-" + (stage || "prepare"));
  document.querySelectorAll("#stage-nav .stage-nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.stage === stage);
    if (btn.dataset.stage === "reviewing") {
      btn.disabled = stage === "prepare" && !lastFindings.length;
    }
  });
  if (stage === "reviewing") {
    $("live-title").textContent = lastFindings.length ? "初审候选已形成" : "AI 正在初审";
    $("live-kicker").textContent = lastFindings.length ? "REVIEW" : "LIVE";
  } else if (stage === "decide" || stage === "export") {
    $("live-title").textContent = "初审候选已形成";
    $("live-kicker").textContent = "REVIEW";
  } else {
    $("live-title").textContent = "等待开始。";
    $("live-kicker").textContent = "IDLE";
  }
}

function applyState(state) {
  $("teacher-name").value = state.teacher_name || "";
  $("student-id").value = state.student_id || "";
  $("major").value = state.major || "";
  $("provider").value = state.provider || "";
  $("model").value = state.model || "";
  $("base-url").value = state.base_url || "";
  $("reasoning").value = state.reasoning || "off";
  $("api-key").value = "";
  $("api-key").placeholder = state.api_key_set ? "已保存，留空则保持不变" : "未保存，离线初审";
  renderIssues(state.issues || []);
  renderDrafts(state.history_drafts || []);
  renderSessions(state.sessions || []);
  $("paper-path").textContent = state.paper_path || "尚未选择当前新稿";
  $("btn-open-doc").disabled = !state.reviewed_path;
  $("btn-open-folder").disabled = !state.output_dir;
  $("reviewed-path").textContent = state.reviewed_path || "";
  renderResults(state.findings || [], state.recall, state.used_model, state.warning, state.stats);
  renderMissedIssues((state.session && state.session.missed_issues) || []);
  renderEvalSummary(state.eval_summary || null);
  setStage(state.stage || "prepare");
  if (state.status) log(state.status);
}

async function refresh() {
  if (!hasBridge()) return;
  applyState(await api("state"));
}

$("btn-save-identity").addEventListener("click", async () => {
  if (!hasBridge()) return;
  await api("save_identity", {
    teacher_name: $("teacher-name").value,
    student_id: $("student-id").value,
    major: $("major").value,
  });
  log("已保存老师与学生身份。", "ok");
  setStatus("身份已保存", "done");
});

$("btn-ingest").addEventListener("click", async () => {
  if (!hasBridge()) return;
  setStatus("正在导入历史稿…", "busy");
  const result = await api("ingest_files");
  log(result.message, result.ok ? "ok" : "err");
  setStatus(result.ok ? "历史稿已导入" : "未导入", result.ok ? "done" : "err");
  await refresh();
});

$("btn-demo").addEventListener("click", async () => {
  if (!hasBridge()) {
    $("teacher-name").value = "老师甲";
    $("student-id").value = "zhou";
    $("major").value = "人工智能";
    $("paper-path").textContent = "演示稿 / thesis.docx";
    log("已载入演示身份。点击「开始 AI 初审」查看候选意见。", "ok");
    setStage("prepare");
    return;
  }
  setStatus("正在载入演示稿…", "busy");
  const result = await api("load_demo");
  log(result.message, result.ok ? "ok" : "err");
  setStatus("演示稿已载入", result.ok ? "done" : "err");
  await refresh();
});

$("btn-choose-paper").addEventListener("click", async () => {
  if (!hasBridge()) return;
  const result = await api("choose_paper");
  if (result && result.ok) {
    $("paper-path").textContent = result.paper_path || "";
    log(result.message, "ok");
  }
});

$("btn-review").addEventListener("click", async () => {
  const btn = $("btn-review");
  if (!hasBridge()) {
    btn.disabled = true;
    setStage("reviewing");
    setStatus("AI 正在初审…", "busy");
    log("AI 正在初审（浏览器演示）");
    window.setTimeout(() => {
      applyState(demoDecideState());
      log("AI 初审完成，4 条候选待老师处理。", "ok");
      setStatus("请处理候选意见", "done");
      btn.disabled = false;
    }, 700);
    return;
  }
  btn.disabled = true;
  setStage("reviewing");
  setStatus("AI 正在初审…", "busy");
  log("AI 正在初审");
  const result = await api("review_file");
  if (!result || !result.started) {
    const message = result && result.message ? result.message : "未开始初审。";
    const cancelled = message.includes("未选择") || message.includes("请稍候");
    log(message, cancelled ? "" : "err");
    setStatus(cancelled ? "准备就绪" : "初审未完成", cancelled ? "idle" : "err");
    setStage("prepare");
    btn.disabled = false;
    return;
  }
  pollReview();
});

function renderTechLog(entries) {
  const body = $("tech-log-body");
  if (!body) return;
  if (!entries || !entries.length) {
    body.textContent = "暂无。";
    renderCoverage([]);
    return;
  }
  body.textContent = entries.map((item) => {
    const flag = item.ok === false ? "未完成" : "完成";
    const name = item.label || item.op;
    const heading = item.heading ? `  ${item.heading}` : "";
    const why = item.ok === false && item.reason ? `  ${item.reason}` : "";
    return `${flag}  ${name}${heading}${why}`;
  }).join("\n");
  renderCoverage(entries);
}

function renderCoverage(entries) {
  const line = $("coverage-line");
  if (!line) return;
  const events = (entries || []).filter((item) => item.op === "coverage" && item.intent);
  if (!events.length) {
    line.hidden = true;
    return;
  }
  const text = String(events[events.length - 1].intent || "");
  const covered = text.match(/章节覆盖\s*(\d+)\s*\/\s*(\d+)/);
  const uncovered = text.match(/未检查：(.+)$/);
  $("coverage-text").textContent = covered ? `${covered[1]} / ${covered[2]} 章已实际阅读` : text;
  $("coverage-uncovered").textContent = uncovered ? `未检查：${uncovered[1]}` : "全部章节均已覆盖";
  line.hidden = false;
}

function applyProgress(prog) {
  if (!prog) return;
  const message = prog.message || prog.status || "";
  if (prog.error) {
    log(prog.error || message || "审查失败。", "err");
    setStatus("初审未完成", "err");
    setStage("prepare");
  } else if (prog.done) {
    log(prog.status || message, "ok");
    setStatus("请处理候选意见", "done");
    setStage(prog.stage || "decide");
  } else if (message) {
    log(message);
    setStatus(message, "busy");
    setStage("reviewing");
  }
  renderResults(prog.findings || [], prog.recall, prog.used_model, prog.warning, prog.stats);
  renderTechLog(prog.tech_log || []);
  renderEvalSummary(prog.eval_summary || null);
  $("btn-open-doc").disabled = !prog.reviewed_path;
  $("btn-open-folder").disabled = !prog.output_dir;
  if (prog.reviewed_path) $("reviewed-path").textContent = prog.reviewed_path;
  if (prog.paper_path) $("paper-path").textContent = prog.paper_path;
}

function pollReview() {
  const btn = $("btn-review");
  const tick = async () => {
    if (!hasBridge()) {
      btn.disabled = false;
      return;
    }
    const prog = await api("progress");
    applyProgress(prog);
    if (prog.done || prog.error) {
      btn.disabled = false;
      if (prog.done && !prog.error) {
        $("sec-decide").scrollIntoView({ behavior: "smooth", block: "start" });
      }
      return;
    }
    window.setTimeout(tick, 300);
  };
  tick();
}

function renderMissedIssues(missed) {
  const box = $("missed-list");
  if (!box) return;
  if (!missed || !missed.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.className = "missed-list";
  box.innerHTML = missed.map((item) => `
    <div class="missed-item" data-id="${esc(item.id)}">
      <div class="missed-item-body">
        <span class="missed-item-problem">${esc(item.problem)}</span>
        <span class="missed-item-meta">${esc(item.section || "未定位")}${item.category ? " · " + esc(KIND_META[item.category] ? KIND_META[item.category].label : item.category) : ""}${item.note ? " · " + esc(item.note) : ""}</span>
      </div>
      <button class="btn btn-ghost missed-remove" type="button" data-id="${esc(item.id)}">删除</button>
    </div>`).join("");
  box.querySelectorAll(".missed-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!hasBridge()) return;
      await api("remove_missed_issue", btn.dataset.id);
      await refresh();
    });
  });
}

function pct(rate) {
  return rate == null ? "—" : `${(rate * 100).toFixed(1)}%`;
}

function durationText(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "—";
  const mins = Math.floor(total / 60);
  const secs = Math.round(total % 60);
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

function renderEvalSummary(summary) {
  const body = $("eval-body");
  if (!body) return;
  if (!summary || !summary.candidate_count) {
    body.innerHTML = `<p class="eval-empty">开始审稿并处理候选意见后，这里会给出本次的评测总结。</p>`;
    return;
  }
  const model = summary.model_snapshot || {};
  const coverage = summary.coverage_summary || {};
  const usage = summary.token_usage;
  const timing = summary.timing || {};
  const modelLine = [model.model || "未知模型", `思考深度 ${model.reasoning || "off"}`]
    .concat(usage ? [`token ${usage.input || 0}入/${usage.output || 0}出`] : [])
    .join(" · ");
  const categoryRows = (summary.category_metrics || []).map((row) => {
    const meta = KIND_META[row.category];
    const label = meta ? meta.label : row.category;
    const name = row.subtype ? `${label} · ${row.subtype}` : label;
    return `<tr><td>${esc(name)}</td><td>${row.candidate_count}</td><td>${row.accepted}</td><td>${row.edited_accepted}</td><td>${row.rejected}</td><td>${pct(row.acceptance_rate)}</td></tr>`;
  }).join("");
  const reasonRows = Object.entries(summary.rejection_reasons || {})
    .map(([reason, count]) => `<tr><td>${esc(REJECTION_LABELS[reason] || (reason === "unclassified" ? "未分类" : reason))}</td><td>${count}</td></tr>`)
    .join("");
  const chapterRows = (summary.chapter_observations || []).map((row) => `
    <tr><td>${esc(row.chapter)}</td><td>${esc(COVERAGE_LABELS[row.coverage_status] || row.coverage_status)}</td><td>${row.ai_candidates}</td><td>${row.accepted + row.edited_accepted}</td><td>${row.rejected}</td><td>${row.teacher_missed_issues}</td></tr>`).join("");
  body.innerHTML = `
    <div class="eval-head">
      <div class="eval-col">
        <h4>本次 AI 初审</h4>
        <p>候选意见：<strong>${summary.candidate_count}</strong></p>
        <p>直接接受：<strong>${summary.accepted_count}</strong></p>
        <p>编辑后接受：<strong>${summary.edited_accepted_count}</strong></p>
        <p>驳回：<strong>${summary.rejected_count}</strong></p>
        <p>待处理：<strong>${summary.pending_count}</strong></p>
      </div>
      <div class="eval-col">
        <h4>章节</h4>
        <p>${coverage.read ?? 0} / ${coverage.total ?? 0} 章已实际读取</p>
        <p>${coverage.probed ?? 0} 章 probed · ${coverage.unread ?? 0} 章 unread</p>
        <h4 class="eval-gap">老师补录 AI 漏检</h4>
        <p><strong>${summary.missed_issue_count}</strong> 条</p>
      </div>
      <div class="eval-col">
        <h4>质量与运行</h4>
        <p>已处理意见采用率：<strong>${pct(summary.acceptance_rate)}</strong></p>
        <p>耗时：${durationText(timing.duration_s)}</p>
        <p>模型：${esc(modelLine)}</p>
      </div>
    </div>
    ${categoryRows ? `<table class="eval-table"><thead><tr><th>类型</th><th>候选</th><th>接受</th><th>改后</th><th>驳回</th><th>采用率</th></tr></thead><tbody>${categoryRows}</tbody></table>` : ""}
    ${reasonRows ? `<table class="eval-table"><thead><tr><th>驳回原因</th><th>条数</th></tr></thead><tbody>${reasonRows}</tbody></table>` : ""}
    ${chapterRows ? `<table class="eval-table"><thead><tr><th>章节</th><th>覆盖</th><th>AI 候选</th><th>采用</th><th>驳回</th><th>漏检补录</th></tr></thead><tbody>${chapterRows}</tbody></table>` : ""}
  `;
}

const COVERAGE_LABELS = { read: "已读", probed: "probed", unread: "unread" };

$("btn-add-missed").addEventListener("click", async () => {
  const problem = $("missed-problem").value.trim();
  if (!problem) {
    log("补录漏检需要先填写问题描述。", "err");
    return;
  }
  if (!hasBridge()) {
    log("浏览器预览：漏检补录仅在真实窗口中保存。", "err");
    return;
  }
  const result = await api(
    "add_missed_issue",
    problem,
    $("missed-section").value.trim(),
    $("missed-category").value,
    $("missed-note").value.trim(),
  );
  if (result && result.ok) {
    $("missed-problem").value = "";
    $("missed-section").value = "";
    $("missed-category").value = "";
    $("missed-note").value = "";
    log("已补录一条 AI 漏检问题。仅作为评测数据，不进入正式 Word。", "ok");
    await refresh();
  } else {
    log((result && result.message) || "未能补录漏检问题。", "err");
  }
});

$("btn-export-eval").addEventListener("click", async () => {
  if (!hasBridge()) {
    log("浏览器预览无法导出评测文件。", "err");
    return;
  }
  const result = await api("export_eval");
  if (result && result.ok && result.files) {
    $("eval-export-path").textContent = result.files.json || "";
    log(`已导出评测数据：${result.files.json}`, "ok");
  } else {
    log((result && result.message) || "未能导出评测数据。", "err");
  }
});

$("btn-accept-format").addEventListener("click", async () => {  if (!hasBridge()) {
    lastFindings.forEach((item) => {
      if (kindOf(item) === "format" && decisionOf(item) === "pending") item.teacher_decision = "accepted";
    });
    lastStats = countsFrom(lastFindings);
    renderResults(lastFindings, lastRecall, false, "", lastStats);
    return;
  }
  await api("accept_format_batch");
  await refresh();
});

async function doExport(allowPending) {
  if (!hasBridge()) {
    if (!allowPending && (lastStats.pending || 0) > 0) {
      log(`仍有 ${lastStats.pending} 条意见未处理。可继续处理，或仅使用当前已确认意见生成。`, "err");
      setStatus("仍有未处理意见", "err");
      return;
    }
    const formal = (lastStats.accepted || 0) + (lastStats.edited_accepted || 0);
    if (!formal) {
      log("还没有老师确认的意见，无法生成正式稿。", "err");
      return;
    }
    $("reviewed-path").textContent = "演示输出 / thesis-reviewed.docx";
    $("btn-open-doc").disabled = false;
    $("btn-open-folder").disabled = false;
    lastStats.formal = formal;
    renderExportStats(lastStats);
    setStage("export");
    log(`演示：已生成正式审稿稿件，共写入 ${formal} 条老师认可意见。浏览器预览不写真实 Word。`, "ok");
    setStatus("正式稿已生成", "done");
    return;
  }
  const result = await api("export_final", allowPending);
  if (!result) return;
  if (!result.ok && result.needs_confirm) {
    log(result.message, "err");
    setStatus("仍有未处理意见", "err");
    return;
  }
  if (result.ok) {
    await refresh();
    const n = result.n_exported || 0;
    const extra = result.warning || "";
    log(`已生成正式审稿稿件，共写入 ${n} 条老师认可意见。${extra}`.trim(), "ok");
    setStatus("正式稿已生成", "done");
    $("btn-open-doc").disabled = !result.reviewed_path;
    $("reviewed-path").textContent = result.reviewed_path || "";
    if (result.stats) renderExportStats(result.stats);
    setStage("export");
  } else {
    log(result.message || "未能生成正式稿。", "err");
  }
}

$("btn-export").addEventListener("click", () => doExport(false));
$("btn-export-confirmed").addEventListener("click", () => doExport(true));
$("btn-open-doc").addEventListener("click", () => {
  if (!hasBridge()) {
    log("浏览器预览无法调用 Microsoft Word。请在 Windows 本机打开正式稿。", "err");
    return;
  }
  api("open_reviewed");
});
$("btn-open-folder").addEventListener("click", () => hasBridge() && api("open_folder"));
$("btn-logs").addEventListener("click", () => hasBridge() && api("open_logs"));
$("btn-settings").addEventListener("click", () => $("settings-modal").classList.remove("hidden"));
$("btn-close-settings").addEventListener("click", () => $("settings-modal").classList.add("hidden"));
$("settings-modal").addEventListener("click", (event) => {
  if (event.target === $("settings-modal")) $("settings-modal").classList.add("hidden");
});
$("btn-save-settings").addEventListener("click", async () => {
  if (!hasBridge()) return;
  const result = await api("save_model", {
    provider: $("provider").value,
    model: $("model").value,
    base_url: $("base-url").value,
    api_key: $("api-key").value,
    reasoning: $("reasoning").value,
  });
  $("settings-modal").classList.add("hidden");
  $("api-key").value = "";
  log(result.api_key_set ? "已保存模型设置。密钥只留在本机，重启后不用重填。" : "已保存模型设置。未填写密钥，将走离线初审。", "ok");
  await refresh();
});

const DEMO_STATE = {
  teacher_name: "老师甲",
  student_id: "zhou",
  major: "人工智能",
  provider: "openai-compatible",
  model: "gpt-4o-mini",
  base_url: "",
  api_key: "",
  api_key_set: false,
  paper_path: "C:/Users/demo/Documents/new.docx",
  reviewed_path: "",
  output_dir: "C:/Users/demo/Documents/论文审改结果",
  status: "演示数据：AI 初审完成，4 条候选待老师处理。",
  used_model: false,
  warning: "",
  stage: "decide",
  stats: { ai_candidates: 4, accepted: 0, edited_accepted: 0, rejected: 0, pending: 4, formal: 0 },
  issues: [
    {
      id: "i-001",
      status: "confirmed",
      category: "A",
      issue_type: "语言表达",
      scope: "全文",
      problem: "叠词加强语气，属无依据的主观评价",
      original_text: "不要用「非常非常有效」这类叠词主观评价，要有数据支撑。",
      original_span: "非常非常有效",
      teacher_intent: "不要用「非常非常有效」这类叠词主观评价，要有数据支撑。",
    },
  ],
  history_drafts: [
    { draft_id: "v1", path: "C:/Users/demo/v1.docx", accessible: true, issue_count: 1 },
  ],
  sessions: [
    { id: "s-1", student_id: "zhou", draft_id: "new", completed: false },
  ],
  findings: [
    {
      id: "rule-A-1",
      source: "rule",
      kind: "language",
      category: "A",
      teacher_decision: "pending",
      problem: "叠词加强语气，属无依据的主观评价。",
      rationale: "「非常非常」是口语化叠词，学术论文应给出可核验的数据。",
      quote: "该方法非常非常有效。",
      anchor: "P0012",
      paragraph_index: 12,
      apply: "both",
      suggested_old: "非常非常有效",
      suggested_new: "有效",
      evidence: [],
    },
    {
      id: "rule-C-1",
      source: "rule",
      kind: "format",
      category: "C",
      teacher_decision: "pending",
      problem: "表格缺少题注。",
      rationale: "学校手册要求每个表格上方有「表 X 标题」格式的题注。",
      quote: "",
      anchor: "T0002",
      paragraph_index: 0,
      apply: "comment",
      evidence: [],
    },
    {
      id: "history-i-001",
      source: "history",
      kind: "history",
      category: "A",
      teacher_decision: "pending",
      problem: "历史召回：避免主观评价。新稿出现相似原文，待老师判断是否复犯。",
      rationale: "仅召回相似原文，未经模型确认，不能直接写成复犯。旧稿批注：不要用「非常非常有效」这类叠词主观评价，要有数据支撑。",
      quote: "非常非常有效",
      anchor: "P0012",
      paragraph_index: 12,
      apply: "comment",
      issue_id: "i-001",
      history_refs: ["i-001"],
      evidence: [
        { kind: "history", draft_id: "draft-2", text: "不要用「非常非常有效」这类叠词主观评价，要有数据支撑。" },
        { kind: "history_span", draft_id: "draft-2", text: "非常非常有效" },
      ],
    },
    {
      id: "argument-1",
      source: "argument",
      kind: "content",
      subtype: "argument",
      category: "B",
      teacher_decision: "pending",
      problem: "结论用词过满，实验结果仅有微弱数值变化。",
      rationale: "未见显著性检验，提升幅度与「显著提升」不符。",
      quote: "实验结果表明该方法显著提升了分类准确率。",
      anchor: "P0041",
      paragraph_index: 41,
      apply: "comment",
      evidence: [
        { kind: "claim", draft_id: "new", text: "实验结果表明该方法显著提升了分类准确率。" },
        { kind: "evidence", draft_id: "new", text: "准确率由 0.81 提高到 0.83。" },
      ],
    },
  ],
  recall: { confirmed: 1, recalled: 1, written: 1, skipped: [], absent: [] },
};

function demoPrepareState() {
  return Object.assign({}, DEMO_STATE, {
    findings: [],
    stage: "prepare",
    reviewed_path: "",
    status: "浏览器预览。选择当前新稿或直接开始 AI 初审，查看四阶段教师工作流。",
    stats: { ai_candidates: 0, accepted: 0, edited_accepted: 0, rejected: 0, pending: 0, formal: 0 },
    recall: { confirmed: 1, recalled: 0, written: 0, skipped: [], absent: [] },
  });
}

function demoDecideState() {
  return Object.assign({}, DEMO_STATE, {
    stage: "decide",
    reviewed_path: "",
    status: "演示数据：AI 初审完成，4 条候选待老师处理。",
    stats: { ai_candidates: 4, accepted: 0, edited_accepted: 0, rejected: 0, pending: 4, formal: 0 },
  });
}

$("stage-nav").addEventListener("click", (event) => {
  const btn = event.target.closest("[data-stage]");
  if (!btn || btn.disabled) return;
  const target = btn.dataset.stage;
  if (target === "prepare") {
    setStage("prepare");
    return;
  }
  if (target === "reviewing" && (lastFindings.length || document.body.classList.contains("stage-reviewing"))) {
    setStage("reviewing");
    $("sec-live").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (target === "decide" && lastFindings.length) {
    setStage("decide");
    $("sec-decide").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (target === "export" && ($("reviewed-path").textContent || lastStats.formal)) {
    setStage("export");
    $("sec-export").scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

function bootLive() {
  setStatus("准备就绪", "idle");
  refresh();
}

window.addEventListener("pywebviewready", bootLive);
if (window.pywebview && window.pywebview.api) {
  bootLive();
}
window.addEventListener("DOMContentLoaded", () => {
  window.setTimeout(() => {
    if (!hasBridge()) {
      applyState(demoPrepareState());
      setStatus("浏览器预览 · 演示数据", "idle");
    }
  }, 50);
});
