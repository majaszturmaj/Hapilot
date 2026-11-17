/**
 * Egress guard for the agent runner, loaded first with `node --import ./egress_guard.mjs`.
 *
 * Every outbound connection this Node process starts goes through net.Socket.prototype.connect (http, https, fetch,
 * undici, WebSocket and TLS all end there). The guard writes each attempt to HAPILOT_EGRESS_LOG (JSONL) and refuses
 * every TCP destination that is not in HAPILOT_EGRESS_ALLOW ("host:port,host:port"). UDP sends and DNS lookups of
 * other names are recorded and refused as well. Local IPC (Unix sockets, pipes) is left alone.
 *
 * This is evidence, not the barrier: the container runs on an internal Docker network whose only reachable host is
 * the model relay. Commands the agent runs (python, curl, ...) are separate processes, outside this guard but inside
 * the same network namespace; the driver watches their connections from /proc/net/tcp.
 */
import dgram from "node:dgram";
import dns from "node:dns";
import { appendFileSync } from "node:fs";
import net from "node:net";

const LOG = process.env.HAPILOT_EGRESS_LOG || "";
const ALLOW = new Set(
	(process.env.HAPILOT_EGRESS_ALLOW || "")
		.split(",")
		.map((s) => s.trim().toLowerCase())
		.filter(Boolean),
);
const ALLOWED_HOSTS = new Set([...ALLOW].map((a) => a.slice(0, a.lastIndexOf(":"))));

function record(entry) {
	if (!LOG) return;
	try {
		appendFileSync(LOG, `${JSON.stringify({ time: new Date().toISOString(), pid: process.pid, ...entry })}\n`);
	} catch {
		// the log is evidence only; a failed write must not change the agent's behaviour
	}
}

/** Destination of a Socket.connect call: {path} for IPC, else {host, port}. */
export function destination(args) {
	let first = args[0];
	if (Array.isArray(first)) first = first[0]; // net.connect passes its normalised [options, callback]
	if (first && typeof first === "object") {
		if (first.path) return { path: String(first.path) };
		return { host: String(first.host || "localhost").toLowerCase(), port: Number(first.port) };
	}
	if (typeof first === "string" && Number.isNaN(Number(first))) return { path: first };
	const host = typeof args[1] === "string" ? args[1] : "localhost";
	return { host: host.toLowerCase(), port: Number(first) };
}

export function allowed(dest) {
	return Boolean(dest.path) || ALLOW.has(`${dest.host}:${dest.port}`);
}

const originalConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function guardedConnect(...args) {
	const dest = destination(args);
	if (dest.path) return originalConnect.apply(this, args);
	const ok = allowed(dest);
	record({ kind: "tcp", host: dest.host, port: dest.port, allowed: ok });
	if (ok) return originalConnect.apply(this, args);
	const error = Object.assign(new Error(`egress blocked: ${dest.host}:${dest.port}`), { code: "EHAPILOTEGRESS" });
	process.nextTick(() => this.destroy(error));
	return this;
};

const originalSend = dgram.Socket.prototype.send;
dgram.Socket.prototype.send = function guardedSend(...args) {
	const port = args.find((a, i) => i > 0 && typeof a === "number");
	const host = args.find((a, i) => i > 0 && typeof a === "string");
	record({ kind: "udp", host: host || "?", port: port ?? null, allowed: false });
	const callback = args.find((a) => typeof a === "function");
	if (callback) process.nextTick(() => callback(new Error("egress blocked: udp")));
	return undefined;
};
void originalSend;

function guardLookup(original) {
	return function guardedLookup(hostname, ...rest) {
		const name = String(hostname || "").toLowerCase();
		if (name && !ALLOWED_HOSTS.has(name) && name !== "localhost") {
			record({ kind: "dns", host: name, allowed: false });
			const callback = rest.find((a) => typeof a === "function");
			const error = Object.assign(new Error(`egress blocked: lookup ${name}`), { code: "ENOTFOUND" });
			if (callback) {
				process.nextTick(() => callback(error));
				return undefined;
			}
			return Promise.reject(error);
		}
		return original.call(this, hostname, ...rest);
	};
}
dns.lookup = guardLookup(dns.lookup);
dns.promises.lookup = guardLookup(dns.promises.lookup);
