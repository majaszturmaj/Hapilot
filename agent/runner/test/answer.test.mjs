import assert from "node:assert/strict";
import { test } from "node:test";
import { answerText, correctionMessage, verdictAction } from "../answer.mjs";

test("a valid or unchecked answer is accepted", () => {
	assert.deepEqual(verdictAction({ valid: true, error: null }, 1, 1), { accept: true });
	assert.deepEqual(verdictAction(null, 1, 1), { accept: true });
});

test("an invalid answer is refused once with the reason, then accepted", () => {
	const first = verdictAction({ valid: false, error: "missing fields: kwota" }, 1, 1);
	assert.equal(first.accept, false);
	assert.match(first.message, /missing fields: kwota/);
	assert.match(first.message, /submit_and_exit/);
	assert.deepEqual(verdictAction({ valid: false, error: "x" }, 2, 1), { accept: true });
	assert.deepEqual(verdictAction({ valid: false, error: "x" }, 1, 0), { accept: true });
	assert.match(verdictAction({ valid: false }, 1, 1).message, /niepoprawny format/);
});

test("the answer file wins over the summary", () => {
	assert.equal(answerText('{"a": 1}', "gotowe"), '{"a": 1}');
	assert.equal(answerText("  ", "gotowe"), "gotowe");
	assert.equal(answerText(null, undefined), "");
	assert.match(correctionMessage("e"), /^Odpowiedź końcowa nie została przyjęta: e\./);
});
