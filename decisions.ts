/** Proposal-only wire data. Only the workbench's human handler can confirm answers. */
export interface Budget { attempts: number; minutes: number; storage_mib: number; stop_conditions: string[] }
export interface DecisionOption { key: string; label: string; benefit: string; risk: string; cost: string; scope: string }
export interface BusinessInput { kind: "choice" | "text"; question: string; options: { key: string; label: string }[] }
export interface DecisionRouting {
    audience: "user_fact" | "user_decision" | "owner_evidence";
    evidence_needs: { gap: string; owner: string; source: string }[];
    advice: { status: "supported" | "insufficient"; value: string; unit: string; conditions: string;
        evidence: string[]; uncertainty: string; owner_signed: boolean };
}
export interface DecisionSpec {
    key: string; kind: "choice" | "approval" | "budget" | "preference";
    question: string; why: string; owner: string; evidence: string[]; missing: string[];
    scope: string; risk: string; cost: string; options: DecisionOption[];
    recommendation: string; reason: string; exclusions: string[]; budget: Budget | null;
    human_input?: BusinessInput; // User-facing answer, never a technical option or grant.
    routing?: DecisionRouting; // New reports separate owner evidence, human fact, and formal proposal.
}
const string = (maxLength: number) => ({ type: "string", minLength: 1, maxLength });
const list = (limit: number, count = 6) => ({ type: "array", maxItems: count, items: string(limit) });
const keySchema = { ...string(48), pattern: "^[a-z][a-z0-9_-]{0,47}$" };
const exact = (properties: Record<string, unknown>) => ({ type: "object", properties, required: Object.keys(properties), additionalProperties: false });
const budgetSchema = exact({ attempts: { type: "integer", minimum: 0, maximum: 2147483647 },
    minutes: { type: "integer", minimum: 0, maximum: 2147483647 }, storage_mib: { type: "integer", minimum: 0, maximum: 2147483647 },
    stop_conditions: { ...list(200), minItems: 1 } });
const optionLimits = { label: 300, benefit: 300, risk: 300, cost: 300, scope: 300 };
const specLimits = { question: 200, why: 400, owner: 120, scope: 500, risk: 300, cost: 200, recommendation: 200, reason: 400 };
const optionProperties = { key: keySchema, ...Object.fromEntries(Object.entries(optionLimits).map(([k, v]) => [k, string(v)])) };
const specProperties = { key: keySchema, kind: { type: "string", enum: ["choice", "approval", "budget", "preference"] },
    ...Object.fromEntries(Object.entries(specLimits).map(([k, v]) => [k, string(v)])), evidence: list(300), missing: list(200),
    exclusions: list(200), options: { type: "array", maxItems: 6, items: exact(optionProperties) }, budget: { anyOf: [budgetSchema, { type: "null" }] },
    routing: exact({ audience: { type: "string", enum: ["user_fact", "user_decision", "owner_evidence"] },
        evidence_needs: { type: "array", maxItems: 6, items: exact({ gap: string(200), owner: string(120), source: string(300) }) },
        advice: exact({ status: { type: "string", enum: ["supported", "insufficient"] }, value: { type: "string", maxLength: 200 },
            unit: { type: "string", maxLength: 40 }, conditions: { type: "string", maxLength: 300 },
            evidence: list(300), uncertainty: string(300), owner_signed: { type: "boolean" } }) }),
    human_input: { type: "object", properties: { kind: { type: "string", enum: ["choice", "text"] }, question: string(200),
        options: { type: "array", maxItems: 6, items: exact({ key: keySchema, label: string(120) }) } },
        required: ["kind", "question", "options"], additionalProperties: false } };
export const decisionSchema = { type: "array", minItems: 1, maxItems: 6, items: { type: "object", properties: specProperties,
    required: Object.keys(specProperties).filter(k => k !== "human_input"), additionalProperties: false },
    description: "Human decision sheet. Proposals only, never permission. Stable task-local keys; comparable options, explicit evidence/missing facts, scope/exclusions, risks, costs, recommendation/reason. budget uses attempts/minutes/MiB + stop conditions. No pending items: omit decisions (not null/empty)." };

function object(value: unknown, keys: string[]): Record<string, unknown> {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join(",") !== [...keys].sort().join(",")) throw new Error("Invalid decision fields.");
    return value as Record<string, unknown>;
}
function text(value: unknown, max: number): asserts value is string {
    if (typeof value !== "string" || !(/[^\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u.test(value)) || [...value].length > max || new TextDecoder("utf-8", { ignoreBOM: true }).decode(new TextEncoder().encode(value)) !== value) throw new Error("Invalid decision text.");
}
function key(value: unknown): asserts value is string {
    text(value, 48);
    if (!/^[a-z][a-z0-9_-]{0,47}$/.test(value)) throw new Error("Invalid stable proposal key.");
}
function texts(value: unknown, max: number): asserts value is string[] {
    if (!Array.isArray(value) || value.length > 6) throw new Error("Invalid decision list.");
    for (const item of value) text(item, max);
}
export function parseDecisions(value: unknown): DecisionSpec[] {
    if (!Array.isArray(value) || value.length < 1 || value.length > 6) throw new Error("Need 1–6 decision items; omit if absent.");
    const keys = new Set<string>();
    let routed = 0;
    for (const item of value) {
        const row = object(item, Object.keys(specProperties).filter(k => !["human_input", "routing"].includes(k)).concat(
            item && typeof item === "object" && !Array.isArray(item) && "human_input" in item ? ["human_input"] : [],
            item && typeof item === "object" && !Array.isArray(item) && "routing" in item ? ["routing"] : []));
        key(row.key);
        if (keys.has(row.key)) throw new Error("Duplicate decision key.");
        keys.add(row.key);
        if (!["choice", "approval", "budget", "preference"].includes(row.kind as string)) throw new Error("Invalid decision kind.");
        for (const [k, max] of Object.entries(specLimits)) text(row[k], max);
        texts(row.evidence, 300); texts(row.missing, 200); texts(row.exclusions, 200);
        if (!Array.isArray(row.options) || row.options.length > 6) throw new Error("Invalid options.");
        const options = new Set<string>();
        for (const option of row.options) {
            const o = object(option, Object.keys(optionProperties));
            key(o.key);
            if (options.has(o.key)) throw new Error("Duplicate option key.");
            options.add(o.key);
            for (const [k, max] of Object.entries(optionLimits)) text(o[k], max);
        }
        if (["choice", "preference"].includes(row.kind as string) ? options.size < 2 : options.size > 0) throw new Error("Options do not match input kind.");
        if (row.human_input !== undefined) {
            const input = object(row.human_input, ["kind", "question", "options"]);
            if (!["choice", "text"].includes(input.kind as string)) throw new Error("Invalid business question kind.");
            text(input.question, 200);
            if (!Array.isArray(input.options) || input.options.length > 6 ||
                (input.kind === "choice" ? input.options.length < 2 : input.options.length !== 0)) throw new Error("Invalid business question choices.");
            const keys = new Set<string>();
            for (const option of input.options) {
                const choice = object(option, ["key", "label"]); key(choice.key); text(choice.label, 120);
                if (keys.has(choice.key as string)) throw new Error("Duplicate business answer key.");
                keys.add(choice.key as string);
            }
        }
        if (row.routing !== undefined) {
            routed++;
            const route = object(row.routing, ["audience", "evidence_needs", "advice"]);
            if (!["user_fact", "user_decision", "owner_evidence"].includes(route.audience as string)) throw new Error("Invalid decision audience.");
            if (!Array.isArray(route.evidence_needs) || route.evidence_needs.length !== (row.missing as string[]).length || route.evidence_needs.length > 6) throw new Error("Missing evidence owners.");
            for (const [i, need] of route.evidence_needs.entries()) {
                const n = object(need, ["gap", "owner", "source"]);
                text(n.gap, 200); text(n.owner, 120); text(n.source, 300);
                if (n.gap !== (row.missing as string[])[i]) throw new Error("Evidence gap owner mismatch.");
            }
            const advice = object(route.advice, ["status", "value", "unit", "conditions", "evidence", "uncertainty", "owner_signed"]);
            if (!["supported", "insufficient"].includes(advice.status as string) || typeof advice.owner_signed !== "boolean") throw new Error("Invalid advice status.");
            for (const [k, max] of [["value", 200], ["unit", 40], ["conditions", 300]] as const) {
                if (typeof advice[k] !== "string" || [...(advice[k] as string)].length > max) throw new Error("Invalid advice value.");
                if (advice[k]) text(advice[k], max);
            }
            text(advice.uncertainty, 300); texts(advice.evidence, 300);
            const complete = (row.evidence as string[]).length > 0 && !(row.missing as string[]).length &&
                (row.kind !== "budget" || row.budget !== null);
            if (route.audience === "user_fact" && row.human_input === undefined) throw new Error("Human fact needs an answerable question.");
            if (route.audience === "owner_evidence" && row.human_input !== undefined) throw new Error("Owner evidence is not a user question.");
            if (route.audience === "user_decision" && (!complete || advice.status !== "supported")) throw new Error("No formal decision without evidence.");
            if (advice.status === "supported" && (!complete || !advice.value || !advice.unit || !advice.conditions ||
                !(advice.evidence as string[]).length || (advice.evidence as string[]).some(e => !(row.evidence as string[]).includes(e)))) throw new Error("Unsupported advice.");
            if (advice.status === "insufficient" && (!(row.missing as string[]).length || advice.value || advice.unit ||
                advice.conditions || advice.owner_signed)) throw new Error("No guessed value when evidence is missing.");
        }
        if (row.budget !== null) {
            if (row.kind !== "budget") throw new Error("Budget proposal on wrong input kind.");
            const b = object(row.budget, ["attempts", "minutes", "storage_mib", "stop_conditions"]);
            for (const k of ["attempts", "minutes", "storage_mib"]) if (!Number.isSafeInteger(b[k]) || (b[k] as number) < 0 || (b[k] as number) > 2147483647) throw new Error("Invalid bounded integer budget.");
            texts(b.stop_conditions, 200);
            if (!b.stop_conditions.length) throw new Error("Budget needs stop conditions.");
        }
    }
    if (routed && routed !== value.length) throw new Error("Every new decision item needs routing.");
    return value as DecisionSpec[];
}

/** Old journals stay readable, but newly emitted reports must carry role/evidence/advice routing. */
export function parseNewDecisions(value: unknown): DecisionSpec[] {
    const specs = parseDecisions(value);
    if (specs.some(spec => !spec.routing)) throw new Error("New reports require routing on every decision.");
    return specs;
}

/** JSON.parse checks syntax; this bounded second pass rejects even escaped duplicate keys. */
export type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue };
export function parseStrictJSON(raw: string): JSONValue {
    let parsed: JSONValue;
    try { parsed = JSON.parse(raw) as JSONValue; }
    catch (cause) { throw new Error("Invalid JSON syntax.", { cause }); }
    let at = 0;
    const space = () => { while (/\s/.test(raw[at] ?? "") && at < raw.length) at++; };
    const str = () => {
        const start = at++;
        while (raw[at] !== '"') { if (raw[at] === "\\") at++; at++; }
        try { return JSON.parse(raw.slice(start, ++at)) as string; }
        catch (cause) { throw new Error("Invalid JSON string.", { cause }); }
    };
    function walk(depth: number): void {
        if (depth > 64) throw new Error("JSON nesting exceeds bounds.");
        space();
        const token = raw[at];
        if (token === '"') { str(); return; }
        if (token === "{" || token === "[") {
            const object = token === "{";
            const end = object ? "}" : "]";
            const keys = new Set<string>();
            at++; space();
            while (raw[at] !== end) {
                if (object) {
                    const k = str();
                    if (keys.has(k)) throw new Error("Duplicate JSON key.");
                    keys.add(k); space(); at++;
                }
                walk(depth + 1); space();
                if (raw[at] === ",") { at++; space(); } else break;
            }
            at++; return;
        }
        const start = at;
        while (at < raw.length && !/[\s,\]}]/.test(raw[at])) at++;
        const primitive = raw.slice(start, at);
        // Reserved journals contain only integral numbers; retain Python's exact integer wire contract.
        if (/^-?[0-9]/.test(primitive) && !/^-?(0|[1-9][0-9]*)$/.test(primitive)) throw new Error("Non-integral JSON number spelling.");
    }
    walk(0);
    return parsed;
}

export const decisionInstructions = "When a human decision blocks progress, include decisions in waggle_report (1–6 bounded items). Provide the question/why/owner, concrete evidence and missing facts, comparable options with benefit/risk/cost/scope, recommendation/reason, exclusions; budget has attempts/minutes/storage_mib/stop_conditions. Write question/why in plain language matching the task, without unexplained implementation codes. For NEW reports put routing on EVERY decision: audience owner_evidence for owner-only gaps, user_fact only with an answerable human_input, user_decision only for fully evidenced technical proposals. Match every missing gap to evidence_needs with named owner and original source. advice.status supported requires cited existing evidence, value with unit, conditions, uncertainty and explicit owner_signed; otherwise mark insufficient, leave value/unit/conditions empty and name missing evidence. Never invent settings, owner signoff or use legacy recommendation text as permission. When a user must supply a business requirement, add human_input as a separate choice (with yes/no/conditional/unknown or relevant exhaustive answers) or text question, distinct from comparable technical options; if the owner must supply evidence, omit human_input. State what the user must answer in ordinary terms, and label who supplies each missing fact (user vs executor/owner); do not ask the user to invent technical evidence. If the business need is unknown, say so and ask the specific question instead of guessing. Reuse a key only for the same task-local question. Missing evidence is missing material, not an approval option. Preference is never permission. Only explicit human answers constitute decisions; legacy prose or your interpretation cannot grant authority. Preserve unchanged prior confirmations without asking again, but changed proposals need review. Previously approved budgets are total caps, never reset by a new run; unknown consumption requires clarification. Ordinary tool approval gates stay intact.";
