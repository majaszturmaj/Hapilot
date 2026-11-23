/**
 * Hapilot v3 agent runner: one task, one Cline SDK session (stage 0 of the v3 plan).
 *
 * The session is set up like the Cline CLI 3.0.74 in headless yolo mode (`cline -y --json -c <dir> -t <s>`), the
 * configuration that was measured on an office-task benchmark. The CLI 3.0.74 binary embeds the same @cline/* 0.2.0 source this
 * runner installs (release commits dab4823 and 071e1b2 of github.com/cline/cline: no source difference), and
 * package-lock.json is derived from the CLI build's bun.lock (bun2npm.py: same versions and integrity hashes for the
 * SDK's whole dependency tree). Same as the CLI: yolo tool preset (read_files,
 * run_commands, editor, submit_and_exit), Cline's own yolo system prompt with workspace metadata, compaction on,
 * stop after 3 consecutive mistakes, loop detection 3/5, every tool auto-approved, no hooks, no subagents or teams.
 * The CLI's prompt preprocessing (a slash command at the start, @/path file mentions) is not done; the trace records
 * for every prompt whether it would have applied.
 * Different from the CLI: no telemetry or feature-flag service is created (the CLI creates PostHog/OTel ones), no git
 * checkpoints (they would write into the documents tree), no hub, the timeout covers the whole run, and every agent
 * event goes to a JSONL trace (tool name, input, output, error, duration: the basis for Tool Call Accuracy).
 *
 * Stage 2 adds Hapilot's domain tools (optional; without `mcp` in the spec the session is the stage 0 one): the MCP
 * server agent/mcp/hapilot_mcp.py is started over stdio with the SDK's MCP client, and its tools join the session
 * under their own names. A tool named in `approval.ask` (e-mail draft) is not auto-approved: the runner asks the host
 * bridge (`approval.bridge`, a Unix socket), which asks the user or applies the run's policy; no bridge = refused.
 *
 * Usage: node --import ./egress_guard.mjs run_task.mjs <spec.json>
 * spec.json: {prompt, cwd, providerId, modelId, apiKey, baseUrl, timeoutSeconds, tracePath, resultPath,
 *             mcp?: {command, args, env, timeoutSeconds}, approval?: {ask: [tool names], bridge: socket path},
 *             rules?: [{name, instructions}],
 *             mode?: "yolo" | "act", events?: "stdout", ask?: {bridge: socket path},
 *             initialMessagesPath?, messagesPath?, answer?: {bridge: socket path, file, corrections},
 *             modelImages?: true when the model takes image input (default: it does not)}
 * Without `modelImages` image parts of model requests become a short text note (no_images.mjs): the SDK's
 * read_files returns a PNG/JPG as an image, and a server without image input (llama-server without mmproj) failed
 * the turn. Each replacement is traced (`images_removed`).
 * Stage 3: `rules` go into the system prompt through the SDK's rule formatter, as the CLI adds a .clinerules file
 * (a "# Rules" section, one "## <name>" each); Cline's own prompt is otherwise unchanged. Without rules: as before.
 * Stage 4 (chat in the UI): `mode: "act"` is Cline's interactive mode (the CLI without -y): its own act prompt, the
 * act tool preset with ask_question and without submit_and_exit, the final answer as plain text; web fetch and skills
 * are switched off, subagents and teams stay off. ask_question goes to the host bridge (`ask.bridge`), which asks the
 * user in the UI. `initialMessagesPath` gives the chat's earlier messages, `messagesPath` receives the conversation
 * after the turn (also after an abort). `events: "stdout"` streams the agent events as JSON lines on stdout (text
 * deltas, tool calls) for the UI. SIGTERM aborts the turn (the UI's stop button). Without these keys: as before.
 * Stage 5 (evaluation): `answer` checks the final answer at submit_and_exit (yolo mode): the answer file (else the
 * summary) goes to the bridge, an invalid one is refused `corrections` times (default 1) with the reason so the agent
 * can fix it (answer.mjs); `result.answer` records the attempts and the last verdict. Without the key: as before.
 * Exit code 0 when the agent completed, 1 otherwise; the result file is written in every case.
 */
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import net from "node:net";
import { basename } from "node:path";
import { answerText, verdictAction } from "./answer.mjs";
import { installNoImages } from "./no_images.mjs";
import {
	buildWorkspaceMetadata,
	ClineCore,
	createDefaultMcpServerClientFactory,
	createTool,
	formatRulesForSystemPrompt,
	InMemoryMcpManager,
	// @cline/shared's buildClineSystemPrompt (the function the CLI imports), re-exported by core under this name
	getClineDefaultSystemPrompt as buildClineSystemPrompt,
	mergeRulesForSystemPrompt,
	prewarmFileIndex,
	SessionSource,
} from "@cline/sdk";

const MAX_CONSECUTIVE_MISTAKES = 3; // CLI_DEFAULT_MAX_CONSECUTIVE_MISTAKES
const LOOP_DETECTION = { softThreshold: 3, hardThreshold: 5 }; // CLI_DEFAULT_LOOP_DETECTION
const MAX_TRACE_TEXT = 200_000;
// The CLI passes the prompt through buildUserInputMessage: a slash command at the start, @/path file mentions.
const CLI_FILE_MENTION = /@(?:"(?:\/|~\/|\.{1,2}\/)[^"\r\n]+"|(?:\/|~\/|\.{1,2}\/)\S+)/i;

// Reaching this line means every named import above exists (the image build runs this).
if (process.argv[2] === "--self-check") {
	console.log("run_task.mjs: imports ok");
	process.exit(0);
}

const spec = JSON.parse(readFileSync(process.argv[2], "utf8"));
const MODE = spec.mode === "act" ? "act" : "yolo";
// act mode: tools of the interactive preset that have no place in Hapilot (the container has no network anyway)
const ACT_DISABLED = ["fetch_web_content", "skills"];
const STREAM_TEXT = 4000;
const startedAt = Date.now();
const sessionId = `${startedAt}_${Math.random().toString(36).slice(2, 7).padEnd(5, "0")}`;

function clip(value) {
	if (typeof value === "string") return value.length > MAX_TRACE_TEXT ? `${value.slice(0, MAX_TRACE_TEXT)}…[cut]` : value;
	if (value === undefined) return undefined;
	try {
		const text = JSON.stringify(value);
		return text.length > MAX_TRACE_TEXT ? `${text.slice(0, MAX_TRACE_TEXT)}…[cut]` : value;
	} catch {
		return String(value);
	}
}

function trace(entry) {
	if (!spec.tracePath) return;
	appendFileSync(spec.tracePath, `${JSON.stringify({ t: Date.now() - startedAt, ...entry })}\n`);
}

if (spec.modelImages !== true) installNoImages((count) => trace({ type: "images_removed", count }));

function cut(value, limit = STREAM_TEXT) {
	const text = typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value);
	return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/** One JSON line on stdout for the UI (only with `events: "stdout"`; nothing else is printed there). */
function emit(entry) {
	if (spec.events !== "stdout") return;
	process.stdout.write(`${JSON.stringify(entry)}\n`);
}

const logger = {
	debug: () => {},
	log: () => {},
	error: (message, metadata) => {
		trace({ type: "sdk_error_log", message, error: String(metadata?.error ?? "") });
	},
};

/** One trace line per agent event; tool calls keep name, input, output, error and duration. */
function onEvent(event) {
	if (event?.type !== "agent_event") return;
	const e = event.payload?.event;
	if (!e) return;
	switch (e.type) {
		case "content_start":
			if (e.contentType === "tool") {
				trace({ type: "tool_start", tool: e.toolName, id: e.toolCallId, input: clip(e.input) });
				emit({ ev: "tool_start", tool: e.toolName, id: e.toolCallId, input: cut(e.input) });
			} else if (e.contentType === "text") {
				emit({ ev: "text_delta", text: e.text ?? "" });
			} else if (e.contentType === "reasoning") {
				emit({ ev: "thinking" });
			}
			break;
		case "content_end":
			if (e.contentType === "tool") {
				trace({ type: "tool_end", tool: e.toolName, id: e.toolCallId, output: clip(e.output), error: e.error ?? null, ms: e.durationMs ?? null });
				emit({ ev: "tool_end", tool: e.toolName, id: e.toolCallId, output: cut(e.output), error: e.error ?? null, ms: e.durationMs ?? null });
			} else if (e.contentType === "text") {
				trace({ type: "text", text: clip(e.text ?? "") });
				emit({ ev: "text_end", text: e.text ?? "" });
			} else if (e.contentType === "reasoning") {
				trace({ type: "reasoning", chars: (e.reasoning ?? "").length, text: clip(e.reasoning ?? "") });
			}
			break;
		case "iteration_end":
			trace({ type: "iteration", n: e.iteration, tool_calls: e.toolCallCount });
			break;
		case "usage":
			trace({ type: "usage", in: e.inputTokens, out: e.outputTokens, reasoning: e.reasoningTokenCount ?? null, total_in: e.totalInputTokens, total_out: e.totalOutputTokens });
			break;
		case "notice":
			trace({ type: "notice", notice: e.noticeType, reason: e.reason ?? null, message: clip(e.message) });
			emit({ ev: "notice", reason: e.reason ?? null, message: cut(e.message, 500) });
			break;
		case "error":
			trace({ type: "error", message: String(e.error?.message ?? e.error), recoverable: e.recoverable, iteration: e.iteration });
			emit({ ev: "error", message: cut(String(e.error?.message ?? e.error), 500), recoverable: e.recoverable });
			break;
		case "done":
			trace({ type: "done", reason: e.reason, iterations: e.iterations });
			break;
		default:
	}
}

const MCP_SERVER = "hapilot";

/** Hapilot's rules as the SDK formats rule records (undefined without rules: the stage 0 prompt). */
function hapilotRules(rules) {
	if (!Array.isArray(rules) || rules.length === 0) return undefined;
	return formatRulesForSystemPrompt(rules.map((r) => ({ name: String(r.name), instructions: String(r.instructions), frontmatter: {} })));
}

/** Text of an MCP tool result for the model; an MCP error result becomes a tool error. */
function mcpText(reply) {
	const text = (reply?.content ?? [])
		.filter((c) => c?.type === "text")
		.map((c) => c.text)
		.join("\n");
	if (reply?.isError) throw new Error(text || "tool error");
	return text;
}

/** Hapilot's MCP tools as session tools: the SDK's MCP client and manager, results passed on as plain text. */
async function hapilotTools(cfg) {
	const manager = new InMemoryMcpManager({
		clientFactory: createDefaultMcpServerClientFactory({ clientName: "hapilot-agent", clientVersion: "0.5.0" }),
	});
	const timeoutSeconds = Number(cfg.timeoutSeconds || 300);
	await manager.registerServer({
		name: MCP_SERVER,
		transport: { type: "stdio", command: cfg.command, args: cfg.args ?? [], cwd: cfg.cwd, env: cfg.env ?? {} },
		timeoutSeconds,
	});
	const descriptors = await manager.listTools(MCP_SERVER);
	const tools = descriptors.map((d) =>
		createTool({
			name: d.name,
			description: d.description ?? "",
			inputSchema: d.inputSchema ?? { type: "object", properties: {} },
			timeoutMs: timeoutSeconds * 1000,
			retryable: false,
			execute: async (input, context) =>
				mcpText(await manager.callTool({ serverName: MCP_SERVER, toolName: d.name, arguments: input ?? {}, context })),
		}),
	);
	return { manager, tools };
}

/** One request to the host bridge (a JSON line each way). */
function bridgeRequest(path, method, params, timeoutMs = 600_000) {
	return new Promise((resolve, reject) => {
		let data = "";
		const socket = net.createConnection({ path });
		socket.setTimeout(timeoutMs, () => socket.destroy(new Error("bridge timeout")));
		socket.on("connect", () => socket.write(`${JSON.stringify({ method, params })}\n`));
		socket.on("data", (chunk) => {
			data += chunk;
		});
		socket.on("error", reject);
		socket.on("close", () => {
			try {
				resolve(JSON.parse(data));
			} catch {
				reject(new Error("bridge: no valid reply"));
			}
		});
	});
}

/** The user's answer to ask_question, through the host bridge; without a bridge the CLI's headless answer. */
async function askUser(question, options) {
	if (!spec.ask?.bridge) return options[0] ?? "";
	let answer = "";
	try {
		const reply = await bridgeRequest(spec.ask.bridge, "ask", { question: String(question ?? ""), options: (options ?? []).map(String) }, 1_000_000);
		answer = reply?.ok ? String(reply.result?.answer ?? "") : `(pytanie nie dotarło do użytkownika: ${reply?.error ?? "błąd"})`;
	} catch (error) {
		answer = `(pytanie nie dotarło do użytkownika: ${error?.message ?? error})`;
	}
	trace({ type: "ask", options: (options ?? []).length, answered: answer.length > 0 });
	return answer || "(brak odpowiedzi użytkownika)";
}

/** Stage 5: the answer check at submit; throws (the submit fails, the run goes on) to ask for a correction. */
async function checkAnswer(summary) {
	answerAttempts += 1;
	let fileText = null;
	try {
		if (spec.answer.file && existsSync(spec.answer.file)) fileText = readFileSync(spec.answer.file, "utf8").slice(0, 200_000);
	} catch {
		fileText = null;
	}
	let verdict = null;
	try {
		const reply = await bridgeRequest(spec.answer.bridge, "answer", { text: answerText(fileText, summary) });
		verdict = reply?.ok ? reply.result : null;
	} catch {
		verdict = null;
	}
	const action = verdictAction(verdict, answerAttempts, Number(spec.answer.corrections ?? 1));
	const valid = verdict ? Boolean(verdict.valid) : null;
	trace({ type: "answer_check", attempt: answerAttempts, file: fileText !== null, valid, error: verdict?.error ?? null, accepted: action.accept });
	result.answer = { attempts: answerAttempts, valid, error: verdict?.error ?? null };
	if (!action.accept) throw new Error(action.message);
}

/** The earlier messages of the chat (stage 4), or undefined: a new conversation. */
function initialMessages() {
	if (!spec.initialMessagesPath || !existsSync(spec.initialMessagesPath)) return undefined;
	const messages = JSON.parse(readFileSync(spec.initialMessagesPath, "utf8"));
	return Array.isArray(messages) && messages.length > 0 ? messages : undefined;
}

const result = { session_id: sessionId, mode: MODE, status: "error", finish_reason: null, text: "", iterations: 0, usage: null, submitted: null, error: null, duration_ms: 0 };
let core;
let mcp;
let answerAttempts = 0;
let timedOut = false;
let aborted = false;
const timer = setTimeout(() => {
	timedOut = true;
	core?.abort(sessionId, new Error(`timeout after ${spec.timeoutSeconds} s`)).catch(() => {});
}, Math.max(1, Number(spec.timeoutSeconds || 900)) * 1000);
process.on("SIGTERM", () => {
	aborted = true;
	trace({ type: "abort", by: "SIGTERM" });
	core?.abort(sessionId, new Error("przerwane przez użytkownika")).catch(() => {});
});

try {
	const cwd = spec.cwd;
	void prewarmFileIndex(cwd).catch(() => {});
	const systemPrompt = buildClineSystemPrompt({
		ide: "Terminal Shell",
		workspaceRoot: cwd,
		workspaceName: basename(cwd),
		metadata: await buildWorkspaceMetadata(cwd),
		rules: mergeRulesForSystemPrompt(hapilotRules(spec.rules), undefined),
		mode: MODE,
		providerId: spec.providerId,
		overridePrompt: undefined,
		platform: process.platform,
	});
	trace({ type: "system_prompt", chars: systemPrompt.length, text: systemPrompt });
	// passed as is; true here would mean the CLI had rewritten this prompt before sending it
	trace({
		type: "prompt",
		chars: spec.prompt.length,
		cli_preprocessing: spec.prompt.startsWith("/") || CLI_FILE_MENTION.test(spec.prompt),
	});
	const ask = new Set(spec.approval?.ask ?? []);
	const toolPolicies = { "*": { autoApprove: true } };
	if (MODE === "act") for (const name of ACT_DISABLED) toolPolicies[name] = { enabled: false };
	for (const name of ask) toolPolicies[name] = { autoApprove: false };
	let extraTools = [];
	if (spec.mcp) {
		mcp = await hapilotTools(spec.mcp);
		extraTools = mcp.tools;
		trace({ type: "mcp", server: MCP_SERVER, tools: extraTools.map((t) => t.name) });
	}
	const requestToolApproval = async (request) => {
		if (!ask.has(request.toolName)) return { approved: true };
		let answer = { approved: false, reason: "no approval channel" };
		if (spec.approval?.bridge) {
			try {
				const reply = await bridgeRequest(spec.approval.bridge, "approve", { tool: request.toolName, input: request.input, call_id: request.toolCallId });
				answer = reply?.ok ? { approved: Boolean(reply.result?.approved), reason: reply.result?.reason ?? undefined } : { approved: false, reason: String(reply?.error ?? "bridge error") };
			} catch (error) {
				answer = { approved: false, reason: `bridge unreachable: ${error?.message ?? error}` };
			}
		}
		trace({ type: "approval", tool: request.toolName, id: request.toolCallId, approved: answer.approved, reason: answer.reason ?? null });
		return answer;
	};
	core = await ClineCore.create({
		backendMode: "local",
		clientName: "hapilot-agent",
		capabilities: {
			toolExecutors: {
				// headless: the CLI's answer without a terminal; act mode with a bridge: the user's answer in the UI
				askQuestion: async (question, options) => askUser(question, options),
				submit: async (summary, verified) => {
					if (MODE === "yolo" && spec.answer) await checkAnswer(summary);
					result.submitted = { summary, verified: Boolean(verified) };
					return `Submission recorded (${verified ? "verified" : "unverified"}): ${summary}`;
				},
			},
			requestToolApproval,
		},
		toolPolicies,
		logger,
	});
	core.subscribe(onEvent);
	const started = await core.start({
		source: SessionSource.CLI,
		config: {
			sessionId,
			providerId: spec.providerId,
			modelId: spec.modelId,
			apiKey: spec.apiKey ?? "",
			baseUrl: spec.baseUrl,
			systemPrompt,
			execution: { maxConsecutiveMistakes: MAX_CONSECUTIVE_MISTAKES, loopDetection: LOOP_DETECTION },
			checkpoint: { enabled: false },
			compaction: { enabled: true },
			mode: MODE,
			toolPolicies,
			extraTools,
			enableTools: true,
			enableSpawnAgent: false,
			enableAgentTeams: false,
			cwd,
			workspaceRoot: cwd,
			logger,
			extensionContext: {
				client: { name: "hapilot-agent", version: "0.1.0", platform: "cli", platformVersion: "0.1.0", isMultiRoot: false },
				workspace: { rootPath: cwd, cwd, workspaceName: basename(cwd), ide: "Terminal Shell", platform: process.platform },
				logger,
			},
			onConsecutiveMistakeLimitReached: async (context) => ({
				action: "stop",
				reason: `max consecutive mistakes reached (${context.maxConsecutiveMistakes}) in yolo mode`,
			}),
		},
		prompt: spec.prompt,
		initialMessages: initialMessages(),
		interactive: false,
	});
	const run = started.result ?? (await core.send({ sessionId: started.sessionId, prompt: spec.prompt }));
	const usage = await core.getAccumulatedUsage(started.sessionId).catch(() => undefined);
	Object.assign(result, {
		status: timedOut ? "timeout" : aborted ? "aborted" : run?.finishReason === "completed" ? "ok" : "error",
		finish_reason: run?.finishReason ?? null,
		text: run?.text ?? "",
		iterations: run?.iterations ?? 0,
		usage: usage?.aggregateUsage ?? usage?.usage ?? run?.usage ?? null,
		model: run?.model ?? null,
	});
	if (result.status === "error" && !result.error) result.error = run?.text || `finish reason ${run?.finishReason}`;
} catch (error) {
	result.status = timedOut ? "timeout" : aborted ? "aborted" : "error";
	result.error = String(error?.stack ?? error);
} finally {
	clearTimeout(timer);
	result.duration_ms = Date.now() - startedAt;
	if (spec.messagesPath && core) {
		// the live conversation: an aborted turn is not flushed to the SDK's own transcript
		try {
			const messages = await (core.readLiveMessages ?? core.readMessages).call(core, sessionId);
			writeFileSync(spec.messagesPath, JSON.stringify(messages ?? []));
			result.messages = (messages ?? []).length;
		} catch (error) {
			result.messages_error = String(error?.message ?? error);
		}
	}
	trace({ type: "result", status: result.status, finish_reason: result.finish_reason });
	emit({ ev: "result", status: result.status, finish_reason: result.finish_reason, text: result.text, error: result.error ? cut(result.error, 1000) : null });
	writeFileSync(spec.resultPath, JSON.stringify(result, null, 2));
	await core?.dispose("hapilot_run_done").catch(() => {});
	await mcp?.manager.dispose().catch(() => {});
}
process.exit(result.status === "ok" ? 0 : 1);
