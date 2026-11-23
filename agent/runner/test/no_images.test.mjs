import assert from "node:assert/strict";
import { test } from "node:test";
import { IMAGE_NOTE, installNoImages, stripImages } from "../no_images.mjs";

const request = {
	model: "qwen",
	messages: [
		{ role: "system", content: "rules" },
		{ role: "tool", tool_call_id: "1", content: "Successfully read image" },
		{ role: "user", content: [{ type: "text", text: "obraz:" }, { type: "image_url", image_url: { url: "data:image/png;base64,iVBOR" } }] },
	],
};

test("image parts become a text note, the rest stays", () => {
	const { body, removed } = stripImages(JSON.stringify(request));
	assert.equal(removed, 1);
	const data = JSON.parse(body);
	assert.deepEqual(data.messages[2].content, [{ type: "text", text: "obraz:" }, { type: "text", text: IMAGE_NOTE }]);
	assert.equal(data.messages[0].content, "rules");
	assert.doesNotMatch(body, /iVBOR/);
});

test("other bodies pass unchanged", () => {
	const plain = JSON.stringify({ messages: [{ role: "user", content: "Ile kosztuje image?" }] });
	assert.deepEqual(stripImages(plain), { body: plain, removed: 0 });
	assert.deepEqual(stripImages("not json image"), { body: "not json image", removed: 0 });
	assert.deepEqual(stripImages(undefined), { body: undefined, removed: 0 });
});

test("fetch sends the stripped body and reports it", async () => {
	const seen = [];
	const before = globalThis.fetch;
	globalThis.fetch = async (input, init) => {
		seen.push(init.body);
		return { ok: true };
	};
	let count = 0;
	try {
		installNoImages((n) => {
			count += n;
		});
		installNoImages(); // twice: still one wrapper
		await globalThis.fetch("http://model:8000/v1/chat/completions", { method: "POST", body: JSON.stringify(request) });
		await globalThis.fetch("http://model:8000/v1/models", { method: "GET" });
	} finally {
		globalThis.fetch = before;
	}
	assert.equal(count, 1);
	assert.doesNotMatch(seen[0], /image_url/);
	assert.equal(seen[1], undefined);
});
