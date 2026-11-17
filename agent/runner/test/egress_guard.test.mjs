// Run: node --test agent/runner/test/  (no network needed: blocked attempts never leave the process)
import { execFile } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const GUARD = join(dirname(fileURLToPath(import.meta.url)), "..", "egress_guard.mjs");

async function runGuarded(script, allow) {
	const log = join(mkdtempSync(join(tmpdir(), "egress-")), "egress.jsonl");
	// asynchronous on purpose: the second test's server lives in this process and must keep answering
	const r = await new Promise((resolve) => {
		execFile(
			process.execPath,
			["--import", GUARD, "--input-type=module", "-e", script],
			{ env: { ...process.env, HAPILOT_EGRESS_LOG: log, HAPILOT_EGRESS_ALLOW: allow }, encoding: "utf8", timeout: 20_000 },
			(error, stdout, stderr) => resolve({ status: error ? (error.code ?? 1) : 0, stdout, stderr }),
		);
	});
	let entries = [];
	try {
		entries = readFileSync(log, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
	} catch {}
	return { ...r, entries };
}

test("blocks and records fetch, raw TCP, UDP and DNS to hosts outside the allow list", async () => {
	const script = `
		import dns from "node:dns";
		import dgram from "node:dgram";
		import net from "node:net";
		const out = {};
		try { await fetch("http://telemetry.example.com/v1/track"); out.fetch = "sent"; } catch (e) { out.fetch = String(e.cause?.code ?? e.code ?? e.message); }
		out.tcp = await new Promise((res) => { const s = net.connect(443, "203.0.113.7"); s.on("connect", () => res("open")); s.on("error", (e) => res(e.code)); });
		out.dns = await dns.promises.lookup("api.example.net").then(() => "resolved", (e) => e.code);
		out.udp = await new Promise((res) => { const s = dgram.createSocket("udp4"); s.send("x", 53, "198.51.100.1", (e) => { s.close(); res(e ? "blocked" : "sent"); }); });
		console.log(JSON.stringify(out));`;
	const r = await runGuarded(script, "model:8000");
	assert.equal(r.status, 0, r.stderr);
	const out = JSON.parse(r.stdout.trim().split("\n").pop());
	assert.equal(out.fetch, "EHAPILOTEGRESS");
	assert.equal(out.tcp, "EHAPILOTEGRESS");
	assert.equal(out.dns, "ENOTFOUND");
	assert.equal(out.udp, "blocked");
	const kinds = r.entries.map((e) => `${e.kind}:${e.host}:${e.allowed}`);
	assert.ok(kinds.includes("tcp:telemetry.example.com:false"), kinds.join(" "));
	assert.ok(kinds.includes("tcp:203.0.113.7:false"), kinds.join(" "));
	assert.ok(kinds.includes("dns:api.example.net:false"), kinds.join(" "));
	assert.ok(kinds.includes("udp:198.51.100.1:false"), kinds.join(" "));
});

test("lets the allowed endpoint through and records it as allowed", async () => {
	const server = createServer((s) => s.end("ok"));
	await new Promise((res) => server.listen(0, "127.0.0.1", res));
	const port = server.address().port;
	const script = `
		import net from "node:net";
		const s = net.connect(${port}, "127.0.0.1");
		s.on("data", (d) => { console.log(String(d)); s.destroy(); });
		s.on("error", (e) => { console.log(e.code); });`;
	const r = await runGuarded(script, `127.0.0.1:${port}`);
	server.close();
	assert.equal(r.stdout.trim(), "ok", r.stderr);
	assert.deepEqual(
		r.entries.map((e) => [e.kind, e.host, e.port, e.allowed]),
		[["tcp", "127.0.0.1", port, true]],
	);
});
