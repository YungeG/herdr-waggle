/** Optional, short UI handoff. No permissions or domain decisions live here. */
export interface Handoff {
    result: string; blocker: string; next_actor: "executor" | "user" | "none";
    next_action: string; user_need: "none" | "input" | "decision";
}
const line = { type: "string", minLength: 1, maxLength: 160, pattern: "^[^\\u0000-\\u001f\\u007f-\\u009f]+$" };
export const handoffSchema = {
    type: "object", additionalProperties: false,
    properties: { result: line, blocker: { ...line, minLength: 0, pattern: "^[^\\u0000-\\u001f\\u007f-\\u009f]*$" },
        next_actor: { type: "string", enum: ["executor", "user", "none"] }, next_action: line,
        user_need: { type: "string", enum: ["none", "input", "decision"] } },
    required: ["result", "blocker", "next_actor", "next_action", "user_need"],
    description: "Short plain-language handoff for Waggle's homepage: actual result, blocker (empty if none), who does what next, and whether the user needs to decide now or supply input. Legacy brief.outcome, brief.actions and brief.verification summarize results, work and checks; they are not a complete handoff. No file paths, schema fields or technical background here: keep those in summary/checks. Do not invent owners, evidence or approval. Missing proposal/evidence is executor work, not a user approval request. This is self-report, NOT permission or human acceptance.",
};
export function parseHandoff(value: unknown): Handoff {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        Object.keys(value).sort().join(",") !== [...handoffSchema.required].sort().join(",")) throw new Error("Invalid short handoff fields.");
    const row = value as Record<string, unknown>;
    for (const key of ["result", "blocker", "next_action"]) {
        const text = row[key];
        if (typeof text !== "string" || [...text].length > 160 ||
            ((!text.trim()) && (key !== "blocker" || text !== "")) || /[\u0000-\u001f\u007f-\u009f]/u.test(text) ||
            Buffer.from(text).toString("utf8") !== text) throw new Error("Short handoff must use bounded nonempty single lines.");
    }
    if (typeof row.next_actor !== "string" || typeof row.user_need !== "string" ||
        !["executor", "user", "none"].includes(row.next_actor) || !["none", "input", "decision"].includes(row.user_need) ||
        (row.next_actor === "user") !== (row.user_need !== "none")) throw new Error("Invalid handoff responsibility/user need.");
    // SAFETY: exact keys, bounded UTF-8 single lines, enum strings and responsibility/need consistency checked above.
    return row as unknown as Handoff;
}
