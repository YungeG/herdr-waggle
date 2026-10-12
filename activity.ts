/** Optional, bounded observation only. No report, run-state, or control authority. */
import { randomUUID } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Identity = { id: string; agent_id: string; session_id: string; cwd: string };
type Tool = { id: string; name: string; started_at: number };
type Child = { id: string; state: string };
type Snapshot = {
    version: 1; run_id: string; agent_id: string; session_id: string; cwd: string;
    seq: number; observed_at: number; bound: boolean; tools: Tool[];
    last_tool: (Tool & { ended_at: number; is_error: boolean }) | null;
    children: Child[]; children_observed_at: number | null; overflow: boolean;
};
const states = new Set(["queued", "running", "complete", "failed", "partial", "paused", "stopped", "rejected"]);
const token = (value: unknown): value is string => typeof value === "string" && /^[\x21-\x7e]{1,160}$/.test(value);
const object = (value: unknown): Record<string, unknown> | undefined => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;

export function observeActivity(pi: ExtensionAPI, current: () => Identity | undefined, write: (snapshot: Snapshot) => void,
                                diagnostic: (id: string) => boolean = () => false) {
    let snapshot: Snapshot | undefined;
    const creations = new Set<string>();
    let cancelQuery: (() => void) | undefined;
    let lastQuery = 0;

    function publish(value: Snapshot) {
        value.seq += 1;
        value.observed_at = Date.now();
        // Telemetry failure must not reach the execution bridge's failClosed path.
        try {
            if (Buffer.byteLength(JSON.stringify(value)) > 16 * 1024) return;
            write(value);
        } catch { /* The previous observation expires; execution is unaffected. */ }
    }
    function invalidate() {
        cancelQuery?.(); cancelQuery = undefined;
        creations.clear();
        if (snapshot?.bound) {
            snapshot.bound = false;
            publish(snapshot);
        }
    }
    function safe(action: () => void) {
        try { action(); } catch { invalidate(); }
    }
    function owned(): Snapshot | undefined {
        const run = current();
        if (!run) { invalidate(); return; }
        if (!snapshot || snapshot.run_id !== run.id) {
            invalidate();
            snapshot = { version: 1, run_id: run.id, agent_id: run.agent_id, session_id: run.session_id,
                cwd: run.cwd, seq: 0, observed_at: Date.now(), bound: true, tools: [], last_tool: null,
                children: [], children_observed_at: null, overflow: false };
            lastQuery = 0;
        }
        if (snapshot.agent_id !== run.agent_id || snapshot.session_id !== run.session_id || snapshot.cwd !== run.cwd) {
            invalidate(); return;
        }
        return snapshot.bound ? snapshot : undefined;
    }
    function queryChildren(value: Snapshot) {
        if (!value.children.length || cancelQuery || Date.now() - lastQuery < 5000) return;
        const requestId = randomUUID(), requestedAt = Date.now();
        lastQuery = requestedAt;
        const finish = () => { clearTimeout(timeout); unsubscribe(); cancelQuery = undefined; };
        const unknown = () => {
            value.children = value.children.map(child => ({ id: child.id, state: "unknown" }));
            value.children_observed_at = null;
            publish(value);
        };
        const unsubscribe = pi.events.on(`subagents:rpc:v1:reply:${requestId}`, raw => safe(() => {
            const reply = object(raw);
            if (reply?.version !== 1 || reply.requestId !== requestId) return;
            finish();
            if (owned() !== value) return;
            const dto = object(object(reply.data)?.asyncSnapshot);
            if (reply.success !== true || dto?.kind !== "pi-subagents.async-status-snapshot" || dto.version !== 1 ||
                typeof dto.generatedAt !== "number" || !Number.isSafeInteger(dto.generatedAt) ||
                dto.generatedAt < requestedAt - 2000 || dto.generatedAt > Date.now() + 2000 ||
                !Array.isArray(dto.runs) || dto.runs.length > 20) { unknown(); return; }
            const rows = dto.runs.map(object);
            value.children = value.children.map(child => {
                const matches = rows.filter(row => row?.id === child.id);
                const state = matches.length === 1 ? matches[0]?.state : undefined;
                return { id: child.id, state: typeof state === "string" && states.has(state) ? state : "unknown" };
            });
            value.children_observed_at = dto.generatedAt;
            publish(value);
        }));
        const timeout = setTimeout(() => safe(() => {
            finish();
            if (owned() === value) unknown();
        }), 3000);
        timeout.unref();
        cancelQuery = finish;
        // Public, in-process, current-session projection. Never spawn or control work.
        pi.events.emit("subagents:rpc:v1:request", { version: 1, requestId, method: "status", params: {} });
    }
    function pulse() {
        safe(() => {
            const value = owned();
            if (value) { publish(value); queryChildren(value); }
        });
    }
    pi.on("tool_execution_start", event => safe(() => {
        if (diagnostic(event.toolCallId)) return; // Only this bridge-owned in-flight probe, not model-chosen lookalike IDs.
        const value = owned();
        if (!value) return;
        if (!token(event.toolCallId) || !/^[A-Za-z0-9_.:-]{1,80}$/.test(event.toolName) || value.tools.length >= 8 ||
            value.tools.some(tool => tool.id === event.toolCallId)) {
            value.overflow = true; publish(value); return;
        }
        if (value.last_tool?.id === event.toolCallId) value.last_tool = null;
        value.tools.push({ id: event.toolCallId, name: event.toolName, started_at: Date.now() });
        const args = object(event.args);
        // Retain only a boolean provenance fact, never the task, script, or arguments.
        if (event.toolName === "subagent" && args && !("action" in args) &&
            ["agent", "workflow", "workflowScript", "workflowScriptPath"].some(key => typeof args[key] === "string")) {
            creations.add(event.toolCallId);
        }
        publish(value);
    }));
    pi.on("tool_execution_end", event => safe(() => {
        const value = owned();
        if (!value) return;
        const index = value.tools.findIndex(tool => tool.id === event.toolCallId && tool.name === event.toolName);
        if (index < 0) return; // No start provenance: never attach an unrelated or late result.
        const tool = value.tools.splice(index, 1)[0];
        value.last_tool = { ...tool, ended_at: Date.now(), is_error: event.isError };
        if (creations.delete(event.toolCallId) && !event.isError) {
            const details = object(object(event.result)?.details);
            const id = details?.asyncId;
            if (token(id) && details?.runId === id && !value.children.some(child => child.id === id)) {
                if (value.children.length < 8) value.children.push({ id, state: "unknown" });
                else value.overflow = true;
            }
        }
        publish(value);
        queryChildren(value);
    }));
    // The bridge decides whether native input continues the same task or detaches it.
    pi.on("session_shutdown", invalidate);
    return { pulse, invalidate };
}
