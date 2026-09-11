import { spawn } from "node:child_process";
import { appendFileSync, existsSync, readFileSync, statSync, unlinkSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

import { Agent } from "@earendil-works/pi-agent-core";
import {
  Type,
  createModels,
  createProvider,
  envApiKeyAuth,
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
} from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";
import { anthropicProvider } from "@earendil-works/pi-ai/providers/anthropic";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";

const here = path.dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const out = { selftest: false, faux: false, request: null };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--selftest") out.selftest = true;
    else if (argv[i] === "--faux") out.faux = true;
    else if (argv[i] === "--request") out.request = argv[++i];
  }
  return out;
}

// Match the Python applog stamp (local time with numeric offset) so lines
// from both sides interleave in one chronological order.
function localStamp(date = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
  );
}

function appendAgentLog(name, line) {
  const dir = process.env.THESIS_LOG_DIR;
  if (!dir) return;
  try {
    const stamp = localStamp();
    appendFileSync(path.join(dir, name), `${stamp} ${line}\n`, { encoding: "utf8" });
  } catch {
    // logging must not break review
  }
}

function removeIfExists(file) {
  try {
    unlinkSync(file);
  } catch (error) {
    if (error && error.code !== "ENOENT") throw error;
  }
}

function waitForPortfile(file, timeoutMs = 20000, minMtimeMs = 0) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      try {
        if (existsSync(file)) {
          const stamp = statSync(file).mtimeMs;
          if (stamp >= minMtimeMs) {
            const port = Number(readFileSync(file, "utf8").trim());
            if (Number.isInteger(port) && port > 0 && port <= 65535) {
              clearInterval(timer);
              resolve(port);
              return;
            }
          }
        }
      } catch {
        // portfile can appear while we read it
      }
      if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        reject(new Error("worker portfile timeout"));
      }
    }, 50);
  });
}

function connectWorker(port, timeoutMs = 1000) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: "127.0.0.1", port });
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error(`worker connect timeout ${port}`));
    }, timeoutMs);
    const onError = (error) => {
      clearTimeout(timer);
      socket.destroy();
      reject(error);
    };
    socket.once("connect", () => {
      clearTimeout(timer);
      socket.off("error", onError);
      resolve(socket);
    });
    socket.once("error", onError);
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function startWorker(cfg) {
  const portfile = path.join(cfg.output_dir, "worker.port");
  removeIfExists(portfile);
  const spawnedAt = Date.now();
  const args = [
    ...(cfg.worker_args || [
      "-m",
      "thesis_review.worker",
      "--home",
      cfg.home,
      "--teacher",
      cfg.teacher_id,
      "--student",
      cfg.student_id,
      "--major",
      cfg.major || "人工智能",
    ]),
    "--portfile",
    portfile,
  ];
  const child = spawn(cfg.python, args, {
    env: {
      ...process.env,
      PYTHONPATH: cfg.pythonpath,
      PYTHONIOENCODING: "utf-8",
      THESIS_REVIEW_ROOT: process.env.THESIS_REVIEW_ROOT || "",
      THESIS_LOG_DIR: process.env.THESIS_LOG_DIR || path.join(cfg.home || "", "logs"),
    },
    stdio: ["ignore", "ignore", "pipe"],
    windowsHide: true,
  });
  appendAgentLog("run.log", `[run] worker spawn python=${cfg.python} portfile=${portfile}`);
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  const exitError = new Promise((_, reject) => {
    child.once("exit", (code, signal) => {
      reject(new Error(`worker exited ${code ?? signal}: ${stderr.trim() || "no stderr"}`));
    });
  });
  exitError.catch(() => {});
  let port;
  try {
    port = await Promise.race([waitForPortfile(portfile, 20000, spawnedAt - 100), exitError]);
  } catch (error) {
    try {
      child.kill();
    } catch {
      // already gone
    }
    throw error;
  }
  let socket;
  let lastError;
  for (let attempt = 0; attempt < 25; attempt += 1) {
    if (child.exitCode != null || child.signalCode) {
      throw new Error(`worker exited ${child.exitCode ?? child.signalCode}: ${stderr.trim() || "no stderr"}`);
    }
    try {
      socket = await connectWorker(port);
      lastError = null;
      appendAgentLog("run.log", `[run] worker connected port=${port}`);
      break;
    } catch (error) {
      lastError = error;
      await sleep(50);
    }
  }
  if (!socket) {
    try {
      child.kill();
    } catch {
      // already gone
    }
    throw lastError || new Error("worker connect failed");
  }
  const pending = new Map();
  const rl = readline.createInterface({ input: socket });
  rl.on("error", () => {});
  socket.on("error", () => {});
  rl.on("line", (line) => {
    if (!line.trim()) return;
    const message = JSON.parse(line);
    const waiter = pending.get(String(message.id));
    if (waiter) {
      pending.delete(String(message.id));
      if (message.error) waiter.reject(new Error(message.error.message));
      else waiter.resolve(message.result);
    }
  });
  let nextId = 0;
  async function call(op, params = {}) {
    const id = String(++nextId);
    const result = new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
    });
    socket.write(`${JSON.stringify({ id, op, params })}\n`);
    return result;
  }
  return { call, child, socket, stderr: () => stderr };
}

export const FIRST_PASS_PROMPT = [
  "你是本科毕业论文的第一轮初审 Agent，服务对象是老师，不是学生。",
  "目标：替老师完成第一遍格式、语言、内容、历史复查和必要时的外部核验。宁可少报，不要乱报。",
  "最终决定由老师做。你只产生候选审稿意见，不要把意见写成已经给学生的正式结论。",
  "流程边界：必须先 open_draft。用 report_intent 持续报告当前阅读位置、检查目标和结果状态，使用老师能看懂的中文短句，不要输出隐藏推理或思维链。",
  "先 list_outline，再按疑点自主选择章节 read_section / read_paragraphs / find_text。禁止为了省事列出全文。导航次数有限：每次导航结果都带 nav_left（剩余次数），请据此规划阅读顺序；额度不足时按大纲取舍重点章节，不要重复尝试已失败的操作。",
  "run_checks 只把格式和语言规则写入候选，不会写进学生 Word。",
  "内容至少覆盖：论证缺口、数据前后矛盾、方法/实验能否支持结论、摘要-正文-结论一致性、术语/结构明显断裂。对「显著」「明显」「有效」等用词，必须到实验或结果里核对，并主动找反证。",
  "内部能核对的问题不要联网。只有政策、统计公报、首次提出权、外部市场规模等无法在论文内核实的事实，才 web_search。检索失败不得编造来源或结论。",
  "学生历史：get_history_candidates 与 semantic_history_candidates 只是召回。语义相似不等于复犯。必须阅读新稿上下文，只有同类问题仍存在且能给出本稿真实原文时，才 confirm_history_finding。",
  "老师历史：get_teacher_feedback 只是软参考。不得生成老师人格画像，不得改写本提示，不得把一次采用升格为学校硬规则。被驳回的意见不能当成正向偏好。",
  "写候选时，quote / evidence_quote 必须是稿件中真实存在的子串。证据不足就放弃，不要调用记录工具。",
  "学校格式由规则检查，不要用模型自由判断格式。",
  "不要使用「再次」「屡次」。不要整段重写。完成初审后必须 commit_review。",
].join("");

const OVERCLAIM_CLAIM = "实验结果表明该方法显著提升了分类准确率。";
const OVERCLAIM_EVIDENCE = "准确率由 0.81 提高到 0.83。";
const DATA_LEFT = "准确率为 81%";
const DATA_RIGHT = "准确率为 85%";
const STATS_CLAIM = "根据国家统计局数据，2023 年相关产业规模已超过十万亿元。";

// The worker's commit_review result is the only source of truth for where
// findings were written; cfg paths can drift from it when tool params get
// mangled in transit.
let lastCommitResult = null;

function makeTool(call, name, label, description, parameters, extra = {}) {
  return {
    name,
    label,
    description,
    parameters,
    executionMode: extra.executionMode || "sequential",
    async execute(_toolCallId, params) {
      const result = await call(name, params);
      if (name === "commit_review") lastCommitResult = result;
      return {
        // Compact JSON: tool results dominate context growth and pretty-print
        // whitespace costs input tokens on every subsequent turn.
        content: [{ type: "text", text: JSON.stringify(result) }],
        details: result,
        terminate: Boolean(extra.terminate),
      };
    },
  };
}

function allTools(call) {
  return [
    makeTool(call, "open_draft", "打开稿件", "打开待审的 Word 稿件。", Type.Object({
      path: Type.Optional(Type.String()),
      bytes_b64: Type.Optional(Type.String()),
    })),
    makeTool(call, "report_intent", "报告检查意图", "向老师报告当前阅读位置、检查目标和结果状态。不要输出思维链。", Type.Object({
      message: Type.String(),
    })),
    makeTool(call, "run_checks", "规则检查", "运行格式和语言规则，结果进入候选区，不写学生 Word。", Type.Object({
      draft_id: Type.Optional(Type.String()),
    })),
    makeTool(call, "get_history_candidates", "历史字符串召回", "用字符串召回教师已确认的历史问题候选。只返回原文片段，不写意见，不能直接判复犯。", Type.Object({
      draft_id: Type.Optional(Type.String()),
    })),
    makeTool(call, "semantic_history_candidates", "学生历史语义召回", "语义召回学生历史问题。相似不等于复犯，必须再读新稿上下文。", Type.Object({
      draft_id: Type.Optional(Type.String()),
    })),
    makeTool(call, "get_teacher_feedback", "老师反馈软参考", "检索老师以往接受/驳回/改写，仅作软参考。驳回项不是正向规则。", Type.Object({
      limit: Type.Optional(Type.Number()),
    })),
    makeTool(call, "confirm_history_finding", "确认历史复犯候选", "判定同一问题仍未改正后，校验 new_quote 为本稿子串才进入候选。不要编造原文。", Type.Object({
      issue_id: Type.String(),
      new_quote: Type.String(),
      rationale: Type.Optional(Type.String()),
      draft_id: Type.Optional(Type.String()),
    })),
    makeTool(call, "list_outline", "列出大纲", "列出标题性段落的序号、锚点和短文本，供导航。不是全文。", Type.Object({})),
    makeTool(call, "read_section", "阅读章节", "从指定段落读到下一标题，最多 8 段或有限字数。", Type.Object({
      start_ordinal: Type.Number(),
      limit: Type.Optional(Type.Number()),
    })),
    makeTool(call, "read_paragraphs", "阅读段落", "从指定序号起读取有限段落，最多 8 段。", Type.Object({
      start_ordinal: Type.Number(),
      limit: Type.Optional(Type.Number()),
    })),
    makeTool(call, "find_text", "检索原文", "在正文中查找短词，返回少量命中上下文。", Type.Object({
      needle: Type.String(),
      max_hits: Type.Optional(Type.Number()),
    })),
    makeTool(call, "web_search", "外部检索", "仅在论文内部无法核验的事实时使用。失败不得编造。", Type.Object({
      query: Type.String(),
      limit: Type.Optional(Type.Number()),
    })),
    makeTool(call, "web_fetch", "读取外部页面", "读取已检索到的来源页面。失败不得编造。", Type.Object({
      url: Type.String(),
    })),
    makeTool(call, "record_argument_finding", "记录论证缺口", "主张原文和证据原文都必须是稿件中的真实子串。", Type.Object({
      claim_quote: Type.String(),
      evidence_quote: Type.String(),
      problem: Type.String(),
      rationale: Type.String(),
      draft_id: Type.Optional(Type.String()),
    })),
    makeTool(call, "record_content_finding", "记录内容问题", "记录论证、数据、方法、实验或结构问题。原文必须真实存在。kind 只能填 content/language/format；subtype 从 argument/data_consistency/method/experiment/structure/terminology/citation 中选。", Type.Object({
      kind: Type.Optional(Type.String()),
      subtype: Type.String(),
      quote: Type.String(),
      evidence_quote: Type.Optional(Type.String()),
      problem: Type.String(),
      rationale: Type.String(),
      suggested_action: Type.Optional(Type.String()),
      section: Type.Optional(Type.String()),
      draft_id: Type.Optional(Type.String()),
    })),
    makeTool(call, "record_external_finding", "记录外部核验", "必须带来源 title 与 URL。外部结果不是绝对真理。", Type.Object({
      quote: Type.String(),
      problem: Type.String(),
      rationale: Type.String(),
      external_sources: Type.Array(Type.Object({
        title: Type.String(),
        url: Type.String(),
        source_type: Type.Optional(Type.String()),
        query: Type.Optional(Type.String()),
        checked_time: Type.Optional(Type.String()),
      })),
      draft_id: Type.Optional(Type.String()),
    })),
    makeTool(call, "commit_review", "结束初审", "保存候选意见。不会生成给学生的正式 Word。无需参数，直接调用即可。", Type.Object({
      draft_id: Type.Optional(Type.String()),
      output_dir: Type.Optional(Type.String()),
    }), { terminate: true }),
  ];
}

function compatibleProvider(cfg) {
  const modelId = cfg.model || "gpt-4o-mini";
  const baseUrl = (cfg.base_url || "https://api.openai.com/v1").replace(/\/$/, "");
  const model = {
    id: modelId,
    name: modelId,
    api: "openai-completions",
    provider: "openai-compatible",
    baseUrl,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 8192,
  };
  return {
    provider: createProvider({
      id: "openai-compatible",
      name: "OpenAI Compatible",
      baseUrl,
      auth: { apiKey: envApiKeyAuth("API key", ["THESIS_API_KEY", "OPENAI_API_KEY"]) },
      models: [model],
      api: openAICompletionsApi(),
    }),
    model,
  };
}

function convertToLlm(messages) {
  return messages.filter((message) => message.role === "user" || message.role === "assistant" || message.role === "toolResult");
}

function parseToolJson(message) {
  if (!message || message.role !== "toolResult") return null;
  const text = (message.content || []).map((part) => part.text || "").join("");
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function lastToolJson(context) {
  const messages = context.messages || [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const parsed = parseToolJson(messages[i]);
    if (parsed) return parsed;
  }
  return {};
}

function findOutlineJson(context) {
  const messages = context.messages || [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const parsed = parseToolJson(messages[i]);
    if (parsed && Array.isArray(parsed.outline)) return parsed;
  }
  return lastToolJson(context);
}

function headingOrdinal(outline, keyword) {
  const items = outline?.outline || [];
  const hit = items.find((item) => String(item.text || "").includes(keyword));
  return hit ? hit.ordinal : 1;
}

function findCandidatesJson(context) {
  const messages = context.messages || [];
  let fallback = { candidates: [] };
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const parsed = parseToolJson(messages[i]);
    if (parsed && Array.isArray(parsed.candidates) && parsed.candidates.length) {
      if (parsed.candidates.some((item) => item.issue_id && item.new_quote)) return parsed;
      if (!fallback.candidates.length) fallback = parsed;
    }
  }
  return fallback;
}

function repeatCandidate(data) {
  const candidates = data.candidates || [];
  return candidates.find((item) =>
    String(item.original_text || item.problem || "").includes("主观评价")
    || String(item.new_quote || "").includes("非常非常有效"),
  );
}

async function runLive(cfg, call) {
  const models = createModels();
  let model;
  if (cfg.provider === "anthropic") {
    models.setProvider(anthropicProvider());
    model = models.getModel("anthropic", cfg.model);
  } else if (cfg.provider === "openai") {
    models.setProvider(openaiProvider());
    model = models.getModel("openai", cfg.model);
  } else {
    const created = compatibleProvider(cfg);
    models.setProvider(created.provider);
    model = created.model;
  }
  if (!model) {
    throw new Error(`找不到模型 ${cfg.provider}/${cfg.model}`);
  }
  const agent = new Agent({
    initialState: {
      systemPrompt: FIRST_PASS_PROMPT,
      model,
      tools: allTools(call),
      thinkingLevel: "off",
    },
    convertToLlm,
    streamFn: models.streamSimple.bind(models),
    getApiKey: () => process.env.THESIS_API_KEY || process.env.OPENAI_API_KEY || process.env.ANTHROPIC_API_KEY,
    toolExecution: "sequential",
  });
  await agent.prompt(
    `打开稿件 path=${cfg.draft_path}，draft_id=${cfg.draft_id}，output_dir=${cfg.output_dir}。自主完成第一轮初审，完成后调用 commit_review。`,
  );
}

function fauxFirstPass(cfg) {
  const scenario = cfg.faux_scenario || "housekeeping";
  const commit = fauxAssistantMessage([
    fauxToolCall("commit_review", { draft_id: cfg.draft_id, output_dir: cfg.output_dir }),
  ]);
  const prelude = [
    fauxAssistantMessage([fauxToolCall("report_intent", { message: "正在读取论文结构" })]),
    fauxAssistantMessage([fauxToolCall("open_draft", { path: cfg.draft_path })]),
    fauxAssistantMessage([fauxToolCall("run_checks", { draft_id: cfg.draft_id })]),
    fauxAssistantMessage([fauxToolCall("get_teacher_feedback", { limit: 8 })]),
    fauxAssistantMessage([fauxToolCall("semantic_history_candidates", { draft_id: cfg.draft_id })]),
    fauxAssistantMessage([fauxToolCall("get_history_candidates", { draft_id: cfg.draft_id })]),
  ];
  if (scenario === "skip_history") {
    return [
      ...prelude,
      fauxAssistantMessage([fauxToolCall("list_outline", {})]),
      commit,
    ];
  }
  if (scenario === "overclaim") {
    return [
      ...prelude,
      fauxAssistantMessage([fauxToolCall("report_intent", { message: "正在检查摘要中的核心结论" })]),
      fauxAssistantMessage([fauxToolCall("list_outline", {})]),
      (context) => fauxAssistantMessage([
        fauxToolCall("read_section", { start_ordinal: headingOrdinal(lastToolJson(context), "4 结论") }),
      ]),
      (context) => fauxAssistantMessage([
        fauxToolCall("read_section", { start_ordinal: headingOrdinal(findOutlineJson(context), "3 实验结果") }),
      ]),
      fauxAssistantMessage([
        fauxToolCall("record_argument_finding", {
          claim_quote: OVERCLAIM_CLAIM,
          evidence_quote: OVERCLAIM_EVIDENCE,
          problem: "结论用词过满，实验结果仅有微弱数值变化。",
          rationale: "未见显著性检验，提升幅度与「显著提升」不符。",
          draft_id: cfg.draft_id,
        }),
      ]),
      commit,
    ];
  }
  if (scenario === "abandon") {
    return [
      ...prelude,
      fauxAssistantMessage([fauxToolCall("list_outline", {})]),
      (context) => fauxAssistantMessage([
        fauxToolCall("read_section", { start_ordinal: headingOrdinal(lastToolJson(context), "4 结论") }),
      ]),
      (context) => fauxAssistantMessage([
        fauxToolCall("read_section", { start_ordinal: headingOrdinal(findOutlineJson(context), "3 实验结果") }),
      ]),
      commit,
    ];
  }
  if (scenario === "data_mismatch") {
    return [
      ...prelude,
      fauxAssistantMessage([fauxToolCall("report_intent", { message: "正在核对第 3 章与第 5 章中的准确率数据" })]),
      fauxAssistantMessage([fauxToolCall("list_outline", {})]),
      fauxAssistantMessage([fauxToolCall("find_text", { needle: "81%" })]),
      fauxAssistantMessage([
        fauxToolCall("record_content_finding", {
          kind: "content",
          subtype: "data_consistency",
          quote: DATA_LEFT,
          evidence_quote: DATA_RIGHT,
          problem: "摘要与结论中的准确率不一致。",
          rationale: "同一指标在不同章节给出了 81% 与 85%。",
          suggested_action: "统一准确率并核对表格。",
          draft_id: cfg.draft_id,
        }),
      ]),
      commit,
    ];
  }
  if (scenario === "web_needed") {
    return [
      ...prelude,
      fauxAssistantMessage([fauxToolCall("report_intent", { message: "正在核对外部数据来源" })]),
      fauxAssistantMessage([fauxToolCall("list_outline", {})]),
      fauxAssistantMessage([fauxToolCall("web_search", { query: "国家统计局 2023 产业规模" })]),
      (context) => {
        const data = lastToolJson(context);
        const hit = (data.hits || [])[0] || { title: "", url: "" };
        if (!data.ok || !hit.url) {
          return commit;
        }
        return fauxAssistantMessage([
          fauxToolCall("record_external_finding", {
            quote: STATS_CLAIM,
            problem: "对外引用的宏观数据需要老师核对来源。",
            rationale: "论文给出国家统计局口径的市场规模，已检索到候选来源，不能当作绝对真理。",
            external_sources: [{
              title: hit.title || "检索结果",
              url: hit.url,
              source_type: hit.source_type || "web",
              query: "国家统计局 2023 产业规模",
              checked_time: hit.checked_time || "",
            }],
            draft_id: cfg.draft_id,
          }),
        ]);
      },
      commit,
    ];
  }
  if (scenario === "web_skip") {
    return [
      ...prelude,
      fauxAssistantMessage([fauxToolCall("report_intent", { message: "正在核对摘要和结论中的准确率数据" })]),
      fauxAssistantMessage([fauxToolCall("list_outline", {})]),
      fauxAssistantMessage([fauxToolCall("find_text", { needle: "81%" })]),
      commit,
    ];
  }
  if (scenario === "web_fail") {
    return [
      ...prelude,
      fauxAssistantMessage([fauxToolCall("web_search", { query: "国家统计局 不存在的检索" })]),
      commit,
    ];
  }
  return [
    ...prelude,
    (context) => {
      const hit = repeatCandidate(findCandidatesJson(context));
      if (!hit) {
        return fauxAssistantMessage([fauxToolCall("list_outline", {})]);
      }
      return fauxAssistantMessage([
        fauxToolCall("confirm_history_finding", {
          issue_id: hit.issue_id,
          new_quote: hit.new_quote,
          rationale: "仍是无依据的主观评价。",
          draft_id: cfg.draft_id,
        }),
      ]);
    },
    fauxAssistantMessage([fauxToolCall("list_outline", {})]),
    commit,
  ];
}

async function runFaux(cfg, call) {
  const faux = fauxProvider({ models: [{ id: "faux-review" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses(fauxFirstPass(cfg));
  const agent = new Agent({
    initialState: {
      systemPrompt: FIRST_PASS_PROMPT,
      model: faux.getModel(),
      tools: allTools(call),
      thinkingLevel: "off",
    },
    convertToLlm,
    streamFn: models.streamSimple.bind(models),
    toolExecution: "sequential",
  });
  await agent.prompt("开始第一轮初审。完成后调用 commit_review。");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.selftest) {
    if (!Agent || !createModels || !Type) throw new Error("pi imports missing");
    process.stdout.write("pi-ok\n");
    return;
  }
  if (!args.request) throw new Error("missing --request");
  const cfg = JSON.parse(readFileSync(args.request, "utf8"));
  const worker = await startWorker(cfg);
  try {
    if (args.faux) await runFaux(cfg, worker.call);
    else await runLive(cfg, worker.call);
    if (!lastCommitResult || !lastCommitResult.findings_path) {
      throw new Error("agent finished without a successful commit_review");
    }
    const committed = {
      ok: true,
      reviewed_path: lastCommitResult.reviewed_path,
      findings_path: lastCommitResult.findings_path,
      n_findings: lastCommitResult.n_findings,
    };
    process.stdout.write(`${JSON.stringify(committed)}\n`);
  } catch (error) {
    process.stderr.write(`${worker.stderr()}\n`);
    appendAgentLog("error.log", `[error] agent ${error.stack || error.message}`);
    throw error;
  } finally {
    worker.socket.end();
    worker.child.kill();
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  appendAgentLog("error.log", `[error] ${error.stack || error.message}`);
  process.exit(1);
});
