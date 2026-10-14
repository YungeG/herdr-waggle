/** Key-event codec; keeps the existing string-list wire shape, never model clocks. */
import { randomUUID } from "node:crypto";
import { parseDecisions, parseStrictJSON, type DecisionSpec } from "./decisions.ts";
import { parseHandoff, type Handoff } from "./handoff.ts";

const PREFIX = "waggle:event:1 ";
const MAX_EVENTS = 120;
const MAX_BYTES = 384 * 1024;
const reportKinds = new Set(["progress", "blocked", "partial", "full"]);
const uuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
export interface Brief { actions: string[]; outcome: string; verification: string }
export interface Report { summary: string; checks: string[]; remaining: string; brief: Brief | null; decisions?: DecisionSpec[]; handoff?: Handoff }
interface KeyEvent {
    id: string; at: string; kind: string; source: "human" | "system" | "executor" | "simulation";
    run_id: string; seq: number; text: string; report: Report | null;
}
function object(value: unknown, keys: string[]): Record<string, unknown> {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join(",") !== [...keys].sort().join(",")) throw new Error("Invalid key record fields.");
    return value as Record<string, unknown>;
}
function text(value: unknown, limit: number, nonempty = false): value is string {
    return typeof value === "string" && [...value].length <= limit && (!nonempty || Boolean(value.trim())) && Buffer.from(value).toString("utf8") === value;
}
function strings(value: unknown, count: number, limit: number, nonempty = false): value is string[] {
    return Array.isArray(value) && value.length <= count && (!nonempty || value.length > 0) && value.every(item => text(item, limit, nonempty));
}
export function parseEvent(raw: string): KeyEvent | undefined {
    if (!raw.startsWith("waggle:event:")) return undefined;
    if (!raw.startsWith(PREFIX) || Buffer.byteLength(raw) > MAX_BYTES) throw new Error("Unsupported or oversized key record.");
    const row = object(parseStrictJSON(raw.slice(PREFIX.length)), ["id", "at", "kind", "source", "run_id", "seq", "text", "report"]);
    if (!uuid(row.id) || (row.run_id !== "" && !uuid(row.run_id)) || !Number.isSafeInteger(row.seq) || (row.seq as number) < 0 ||
        !text(row.at, 24) || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(row.at) || new Date(row.at).toISOString() !== row.at ||
        !text(row.kind, 48, true) || !text(row.source, 16) || !["human", "system", "executor", "simulation"].includes(row.source) || !text(row.text, 6000)) throw new Error("Invalid key record identity/time/content.");
    if (row.source === "executor" && !reportKinds.has(row.kind)) throw new Error("Executor source requires an explicit report snapshot.");
    if (["created", "edited", "assigned", "dispatched", "returned", "accepted", "abandoned"].includes(row.kind) && !["human", "simulation"].includes(row.source)) throw new Error("Invalid human-action source.");
    if (row.report !== null) {
        const hasDecisions = Object.prototype.hasOwnProperty.call(row.report, "decisions");
        const hasHandoff = Object.prototype.hasOwnProperty.call(row.report, "handoff");
        const report = object(row.report, ["summary", "checks", "remaining", "brief", ...(hasDecisions ? ["decisions"] : []), ...(hasHandoff ? ["handoff"] : [])]);
        if (hasHandoff) parseHandoff(report.handoff);
        if (hasDecisions) {
            parseDecisions(report.decisions);
            if (row.kind === "full") throw new Error("Pending decisions cannot be a full delivery.");
        }
        if (!text(report.summary, 6000, true) || !strings(report.checks, 32, 1000, false) || report.checks.some(check => !check.trim()) || !text(report.remaining, 6000)) throw new Error("Invalid report snapshot.");
        if (report.brief !== null) {
            const brief = object(report.brief, ["actions", "outcome", "verification"]);
            if (!strings(brief.actions, 3, 100, true) || !text(brief.outcome, 160, true) || !text(brief.verification, 160, true)) throw new Error("Invalid explicit report brief.");
        }
        if (!["executor", "simulation"].includes(row.source)) throw new Error("Invalid report source.");
        if (row.kind === "full" && (!(report.checks as string[]).length || (report.remaining as string).trim())) throw new Error("Invalid full report snapshot.");
        if (["partial", "blocked"].includes(row.kind) && !(report.remaining as string).trim()) throw new Error("Partial/blocked snapshot needs remaining work.");
    }
    if (reportKinds.has(row.kind) !== (row.report !== null)) throw new Error("Report kind and snapshot disagree.");
    // SAFETY: exact envelope, scalar fields, report schema and source invariants were validated above.
    return row as unknown as KeyEvent;
}
const bytes = (values: string[]) => Buffer.byteLength(JSON.stringify(values)) + Math.max(0, values.length - 1); // Python's persisted array includes separator spaces.
export function validateEvents(values: string[], runId: string, seq: number) {
    const events = values.map(parseEvent).filter((event): event is KeyEvent => event !== undefined);
    if (events.length && (values.length > MAX_EVENTS || bytes(values) > MAX_BYTES)) throw new Error("Key journal exceeds bounds.");
    if (new Set(events.map(event => event.id)).size !== events.length) throw new Error("Duplicate key event identity.");
    if (events.some(event => event.run_id !== runId || !["system", "executor"].includes(event.source) || event.seq < 1 || event.seq > seq)) throw new Error("Key event source/revision/run mismatch.");
    const origins = events.filter(event => event.kind === "origin");
    if (origins.length > 1 || origins.some(event => event.source !== "system")) throw new Error("Conflicting original session location.");
    const parents = events.filter(event => event.kind === "continued");
    if (parents.length > 1 || parents.some(event => event.source !== "system" || !uuid(event.text) || event.text === runId)) throw new Error("Invalid continuation parent.");
    const adopted = events.filter(event => event.kind === "adopted");
    if (adopted.length > 1 || (adopted.length && parents.length)) throw new Error("Conflicting adoption origin.");
    for (const event of adopted) {
        const source = object(parseStrictJSON(event.text), ["adoption_id", "parent_run_id", "source_digest"]);
        if (event.source !== "system" || !uuid(source.adoption_id) || (source.parent_run_id !== "" && !uuid(source.parent_run_id)) ||
            source.parent_run_id === runId || !text(source.source_digest, 64) || Boolean(source.parent_run_id) !== Boolean(source.source_digest) ||
            (source.source_digest && !/^[0-9a-f]{64}$/.test(source.source_digest))) throw new Error("Invalid adoption origin.");
    }
}
function encoded(kind: string, body: string, runId: string, seq: number, report: Report | null): string {
    const event: KeyEvent = { id: randomUUID(), at: new Date().toISOString(), kind, source: report ? "executor" : "system",
        run_id: runId, seq, text: body, report };
    const value = PREFIX + JSON.stringify(event);
    parseEvent(value);
    return value;
}
export function appendKeyEvent(values: string[], kind: string, body: string, runId: string, seq: number, report: Report | null = null): string[] {
    const bounded = [...body].length > 6000 ? [...body].slice(0, 5960).join("") + "…（记录文字超出上限）" : body;
    const value = encoded(kind, bounded, runId, seq, report);
    const entries = [...values, value].map(raw => ({ raw, event: parseEvent(raw) }));
    let gap = entries.find(entry => entry.event?.kind === "history_gap")?.raw;
    const kept = entries.filter(entry => entry.event?.kind !== "history_gap");
    const latestReport = [...kept].reverse().find(entry => entry.event?.report)?.raw;
    const protectedItems = new Set([value, latestReport, ...kept.filter(entry => ["origin", "continued", "adopted"].includes(entry.event?.kind || "")).map(entry => entry.raw)]);
    while (true) {
        const result = [...(gap ? [gap] : []), ...kept.map(entry => entry.raw)];
        if (result.length <= MAX_EVENTS && bytes(result) <= MAX_BYTES) return result;
        gap ??= encoded("history_gap", "部分较早记录已超出保存上限；以下仅为保留记录。", runId, seq, null);
        const index = kept.findIndex(entry => !protectedItems.has(entry.raw));
        if (index < 0) throw new Error("Protected report and original session exceed journal bounds; refusing to truncate snapshots.");
        kept.splice(index, 1);
    }
}
