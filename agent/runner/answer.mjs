/**
 * The final answer of an evaluation run (stage 5): when the agent calls submit_and_exit, the runner sends the answer
 * (the JSON file the task names, else the submit summary) to the host bridge, which checks it against the task's
 * format. An invalid answer is refused `corrections` times with the reason: the submit tool then fails, and the SDK
 * goes on with the run (only a successful completing tool ends it), so the agent can correct the answer. After that,
 * or when the bridge cannot be reached, the answer is accepted as it is and recorded as invalid or unchecked.
 */

export function correctionMessage(error) {
	return (
		`Odpowiedź końcowa nie została przyjęta: ${error}. Popraw odpowiedź (jeden obiekt JSON z wymaganymi polami ` +
		"w pliku wskazanym w zadaniu) i wywołaj submit_and_exit ponownie."
	);
}

/** {accept, message?} for one submit: `verdict` from the bridge ({valid, error}) or null, `attempt` counts from 1. */
export function verdictAction(verdict, attempt, corrections) {
	if (!verdict || verdict.valid) return { accept: true };
	if (attempt <= corrections) return { accept: false, message: correctionMessage(verdict.error ?? "niepoprawny format") };
	return { accept: true };
}

/** The text that is checked: the answer file if it was written, else the submit summary. */
export function answerText(fileText, summary) {
	if (typeof fileText === "string" && fileText.trim()) return fileText;
	return summary === undefined || summary === null ? "" : String(summary);
}
