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
 * Usage: node --import ./egress_guard.mjs run_task.mjs <spec.json>
 * spec.json: {prompt, cwd, providerId, modelId, apiKey, baseUrl, timeoutSeconds, tracePath, resultPath}
 * Exit code 0 when the agent completed, 1 otherwise; the result file is written in every case.
 */
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import { basename } from "node:path";
import {
	buildWorkspaceMetadata,
	ClineCore,
	// @cline/shared's buildClineSystemPrompt (the function the CLI imports), re-exported by core under this name
	getClineDefaultSystemPrompt as buildClineSystemPrompt,
	mergeRulesForSystemPrompt,
	prewarmFileIndex,
	SessionSource,
} from "@cline/sdk";

const MODE = "yolo";
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
			}
			break;
		case "content_end":
			if (e.contentType === "tool") {
				trace({ type: "tool_end", tool: e.toolName, id: e.toolCallId, output: clip(e.output), error: e.error ?? null, ms: e.durationMs ?? null });
			} else if (e.contentType === "text") {
				trace({ type: "text", text: clip(e.text ?? "") });
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
			break;
		case "error":
			trace({ type: "error", message: String(e.error?.message ?? e.error), recoverable: e.recoverable, iteration: e.iteration });
			break;
		case "done":
			trace({ type: "done", reason: e.reason, iterations: e.iterations });
			break;
		default:
	}
}

const result = { session_id: sessionId, status: "error", finish_reason: null, text: "", iterations: 0, usage: null, submitted: null, error: null, duration_ms: 0 };
let core;
let timedOut = false;
const timer = setTimeout(() => {
	timedOut = true;
	core?.abort(sessionId, new Error(`timeout after ${spec.timeoutSeconds} s`)).catch(() => {});
}, Math.max(1, Number(spec.timeoutSeconds || 900)) * 1000);

try {
	const cwd = spec.cwd;
	void prewarmFileIndex(cwd).catch(() => {});
	const systemPrompt = buildClineSystemPrompt({
		ide: "Terminal Shell",
		workspaceRoot: cwd,
		workspaceName: basename(cwd),
		metadata: await buildWorkspaceMetadata(cwd),
		rules: mergeRulesForSystemPrompt(undefined, undefined),
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
	const toolPolicies = { "*": { autoApprove: true } };
	core = await ClineCore.create({
		backendMode: "local",
		clientName: "hapilot-agent",
		capabilities: {
			toolExecutors: {
				// same answers as the CLI without a terminal
				askQuestion: async (_question, options) => options[0] ?? "",
				submit: async (summary, verified) => {
					result.submitted = { summary, verified: Boolean(verified) };
					return `Submission recorded (${verified ? "verified" : "unverified"}): ${summary}`;
				},
			},
			requestToolApproval: async () => ({ approved: true }),
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
		interactive: false,
	});
	const run = started.result ?? (await core.send({ sessionId: started.sessionId, prompt: spec.prompt }));
	const usage = await core.getAccumulatedUsage(started.sessionId).catch(() => undefined);
	Object.assign(result, {
		status: timedOut ? "timeout" : run?.finishReason === "completed" ? "ok" : "error",
		finish_reason: run?.finishReason ?? null,
		text: run?.text ?? "",
		iterations: run?.iterations ?? 0,
		usage: usage?.aggregateUsage ?? usage?.usage ?? run?.usage ?? null,
		model: run?.model ?? null,
	});
	if (result.status === "error" && !result.error) result.error = run?.text || `finish reason ${run?.finishReason}`;
} catch (error) {
	result.status = timedOut ? "timeout" : "error";
	result.error = String(error?.stack ?? error);
} finally {
	clearTimeout(timer);
	result.duration_ms = Date.now() - startedAt;
	trace({ type: "result", status: result.status, finish_reason: result.finish_reason });
	writeFileSync(spec.resultPath, JSON.stringify(result, null, 2));
	await core?.dispose("hapilot_run_done").catch(() => {});
}
process.exit(result.status === "ok" ? 0 : 1);
