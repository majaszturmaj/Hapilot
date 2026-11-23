/**
 * Hapilot v3: the local model takes no image input (llama-server without mmproj answers "image input is not
 * supported"), but the Cline SDK's read_files returns a PNG/JPG as an image for the model, and the turn then fails
 * after four retries. Found on the pilot (2026-11-23): the agent drew a chart, opened it with read_files to check it,
 * and the user got the error instead of an answer.
 *
 * The runner wraps fetch: in a JSON request body every image part of a message ({type: "image_url"} of the OpenAI
 * chat API, {type: "image"} of other shapes) becomes a short text part that tells the agent it cannot see images.
 * The SDK has a switch for this (toolContextMetadata.modelSupportsImages), but in SDK 0.2.0 it does not reach the
 * tools from the session config. Plain functions, unit-tested (test/no_images.test.mjs).
 */

export const IMAGE_NOTE =
	"[Obraz pominięty: model nie widzi obrazów. Nie otwieraj plików PNG/JPG narzędziem read_files; " +
	"sprawdź wykres w skrypcie (dane, serie, opisy osi) i poleceniem ls -l.]";

function isImagePart(part) {
	return Boolean(part) && typeof part === "object" && (part.type === "image_url" || part.type === "image" || part.type === "input_image");
}

/** The request body with image parts replaced by IMAGE_NOTE; {body, removed}. Bodies that are not JSON pass as is. */
export function stripImages(body) {
	if (typeof body !== "string" || !body.includes("image")) return { body, removed: 0 };
	let data;
	try {
		data = JSON.parse(body);
	} catch {
		return { body, removed: 0 };
	}
	if (!data || !Array.isArray(data.messages)) return { body, removed: 0 };
	let removed = 0;
	for (const message of data.messages) {
		if (!message || !Array.isArray(message.content)) continue;
		message.content = message.content.map((part) => {
			if (!isImagePart(part)) return part;
			removed += 1;
			return { type: "text", text: IMAGE_NOTE };
		});
	}
	return removed ? { body: JSON.stringify(data), removed } : { body, removed: 0 };
}

/** Wrap globalThis.fetch (the SDK's model client looks it up per request); onRemoved(count) for the trace. */
export function installNoImages(onRemoved = () => {}) {
	const original = globalThis.fetch;
	if (typeof original !== "function" || original.hapilotNoImages) return;
	const wrapped = async (input, init) => {
		if (init && typeof init.body === "string") {
			const { body, removed } = stripImages(init.body);
			if (removed) {
				onRemoved(removed);
				init = { ...init, body };
			}
		}
		return original(input, init);
	};
	wrapped.hapilotNoImages = true;
	globalThis.fetch = wrapped;
}
