/** Attest the schema Pi actually exposes, not this source file or a static version flag. */
import { createHash, randomUUID } from "node:crypto";
import type { ExtensionAPI, ToolLoadout } from "@earendil-works/pi-coding-agent";
import { decisionSchema, type DecisionSpec } from "./decisions.ts";
import { handoffSchema, type Handoff } from "./handoff.ts";

export interface ReportParameters {
    run_id: string; kind: "progress" | "blocked" | "partial" | "full";
    summary: string; checks: string[]; remaining: string;
    decisions?: DecisionSpec[]; handoff?: Handoff; supplement_id?: string;
    brief?: { actions: string[]; outcome: string; verification: string };
}
// One supported contract for registration and comparison, including EVERY constraint.
// The live proof is still computed from getAllTools(), never from this object alone.
export const reportSchema = {
    type: "object", required: ["run_id", "kind", "summary", "checks", "remaining"],
    properties: {
        run_id: { type: "string" }, kind: { type: "string", enum: ["progress", "blocked", "partial", "full"] },
        summary: { type: "string", minLength: 1, maxLength: 6000 },
        checks: { type: "array", maxItems: 32, items: { type: "string", minLength: 1, maxLength: 1000 } },
        remaining: { type: "string", maxLength: 6000 }, decisions: decisionSchema, handoff: handoffSchema,
        supplement_id: { type: "string", description: "Latest received SUPPLEMENT_ID; required after any Waggle runtime supplement." },
        brief: { type: "object", additionalProperties: false, required: ["actions", "outcome", "verification"], properties: {
            actions: { type: "array", minItems: 1, maxItems: 3, items: { type: "string", minLength: 1, maxLength: 100 } },
            outcome: { type: "string", minLength: 1, maxLength: 160 }, verification: { type: "string", minLength: 1, maxLength: 160 },
        } },
    },
};
type ToolAPI = Pick<ExtensionAPI, "getAllTools" | "getActiveTools">;
function canonical(value: unknown): string {
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (value && typeof value === "object") return "{" + Object.entries(value)
        .filter(([key]) => !["description", "title"].includes(key)).sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => JSON.stringify(key) + ":" + canonical(item)).join(",") + "}";
    return JSON.stringify(value) ?? "undefined";
}
const sameSchema = (actual: unknown, expected: unknown) => canonical(actual) === canonical(expected);
function coreSchema(schema: { properties?: Record<string, unknown> }) {
    // Only these two optional feature bodies are independent of the base report.
    // Their presence in required, extra fields and ANY other constraints stay in the comparison.
    return { ...schema, properties: Object.fromEntries(Object.entries(schema.properties || {})
        .filter(([key]) => !["handoff", "decisions"].includes(key))) };
}
export function requiredReportFeatures(requirements: string, structured = false): string[] {
    return ["report-v1", "handoff-v1", ...(structured || requirements.includes("交接目标：交付可审提案，不批准或执行新工作。") ||
        requirements.includes("结构化人工决定 · 草稿不是批准") ? ["decisions-v3"] : [])];
}
export interface ReportCallability {
    transport: "native" | "fabric"; ref: "waggle_report" | "extensions.waggle_report";
    verified_at: number; boundary: "native-report-guard" | "fabric-report-guard" | "fabric-host-guard";
    schema_sha256: string;
}
export function reportCapabilities(pi: ToolAPI, binding: { agent_id: string; session_id: string; cwd: string },
                                   observed_at = Date.now(), evidence?: ReportCallability) {
    let active = false, schema_sha256 = "", features: string[] = [];
    try {
        const matches = pi.getAllTools().filter(tool => tool.name === "waggle_report");
        if (matches.length === 1) {
            const schema = JSON.parse(JSON.stringify(matches[0].parameters));
            schema_sha256 = createHash("sha256").update(canonical(schema)).digest("hex");
            active = matches[0].exposure !== "hidden" && pi.getActiveTools().includes("waggle_report");
            const props = schema.properties || {};
            if (sameSchema(coreSchema(schema), coreSchema(reportSchema))) features.push("report-v1");
            if (sameSchema(props.handoff, handoffSchema)) features.push("handoff-v1");
            if (sameSchema(props.decisions, decisionSchema)) features.push("decisions-v3");
        }
    } catch { active = false; schema_sha256 = ""; features = []; } // Unknown is never compatible.
    const verified = active && evidence?.schema_sha256 === schema_sha256 &&
        0 <= observed_at - evidence.verified_at && observed_at - evidence.verified_at <= 8000;
    return { version: 2, ...binding, observed_at, tool: "waggle_report", active, schema_sha256, features,
        callable: Boolean(verified), transport: verified ? evidence.transport : "unknown",
        ref: verified ? evidence.ref : "", verified_at: verified ? evidence.verified_at : 0,
        boundary: verified ? evidence.boundary : "" };
}

/** Fixed, unmatched-run diagnostics only. Never execute caller-supplied code or forge an assistant. */
export function reportChannel(pi: ToolAPI, currentRun: () => string | undefined) {
    type Tool = ToolLoadout["registered"][number];
    let native: Tool | undefined, fabric: Tool | undefined, epoch = 0;
    let evidence: ReportCallability | undefined, pending: Promise<void> | undefined;
    let controller: AbortController | undefined;
    const diagnostics = new Set<string>();
    const schemaProof = () => reportCapabilities(pi, { agent_id: "", session_id: "", cwd: "" });
    function invalidate() { epoch += 1; evidence = undefined; controller?.abort(); }
    function observe(loadout: ToolLoadout) {
        const nextNative = loadout.callable.find(tool => tool.name === "waggle_report");
        const nextFabric = loadout.declared.find(tool => tool.name === "fabric_exec");
        if (native?.execute !== nextNative?.execute || fabric?.execute !== nextFabric?.execute) invalidate();
        native = nextNative; fabric = nextFabric;
        return undefined;
    }
    async function verify(retry = true): Promise<void> {
        if (pending) return pending;
        const generation = epoch, nativeTool = native, fabricTool = fabric;
        const before = schemaProof();
        evidence = undefined; // No timestamp refresh while a probe is incomplete or fails.
        if (!nativeTool || !before.active) return;
        const run_id = randomUUID();
        if (run_id === currentRun()) return; // A diagnostic must NEVER match an owned run.
        const args: ReportParameters = { run_id, kind: "progress", summary: "Waggle report channel diagnostic", checks: [], remaining: "" };
        const probeId = `waggle-report-probe:${run_id}`; diagnostics.add(probeId);
        const abort = new AbortController(); controller = abort;
        const deadline = setTimeout(() => abort.abort(), 3000); deadline.unref();
        pending = (async () => {
            let boundary: ReportCallability["boundary"] | undefined, transport: "native" | "fabric" = "native";
            if (fabricTool) {
                // The public loadout supplies the real Fabric executable/runner context. This fixed
                // program only discovers the schema and invokes an invalid run; normal report gates remain.
                // Put the fresh unmatched-run arguments in the program, not just payloads:
                // Fabric scopes its repeated-program guard to code, while each probe has a new identity.
                const code = `const report=${JSON.stringify(args)};
const providers=await tools.providers();
if(!providers.some(p=>p.name==='pi')) return {native:true};
const descriptor=await tools.describe({ref:'extensions.waggle_report'});
if(!descriptor) return {unavailable:true};
let receipt=''; try {await tools.call({ref:'extensions.waggle_report',args:report});} catch(e) {receipt=String(e);}
return {schema:descriptor.inputSchema,receipt};`;
                const result = await fabricTool.execute(probeId, { code, resultFormat: "json" }, abort.signal);
                if (!(result.details as { success?: boolean } | undefined)?.success) return;
                const value = JSON.parse(result.content.filter(part => part.type === "text").map(part => part.text).join("\n"));
                if (value.native !== true) {
                    if (!sameSchema(value.schema, nativeTool.parameters)) return;
                    transport = "fabric";
                    if (value.receipt === "Error: No matching active Waggle run; report rejected.") boundary = "fabric-report-guard";
                    // Fresh Pi has no assistant attribution yet. This is the native host authority
                    // boundary, NOT proof that the report body executed or that a report was saved.
                    if (value.receipt === "Error: No assistant message issued this call") boundary = "fabric-host-guard";
                }
            }
            if (transport === "native") {
                try { await nativeTool.execute(probeId, args, abort.signal); }
                catch (error) { if (String(error) === "Error: No matching active Waggle run; report rejected.") boundary = "native-report-guard"; }
            }
            if (!abort.signal.aborted && generation === epoch && boundary && schemaProof().schema_sha256 === before.schema_sha256) {
                evidence = { transport, ref: transport === "fabric" ? "extensions.waggle_report" : "waggle_report",
                    boundary, verified_at: Date.now(), schema_sha256: before.schema_sha256 };
            }
        })().catch(() => { /* Unknown/denied/stale transport stays non-callable; never bypass policy. */ }).finally(() => {
            diagnostics.delete(probeId);
            clearTimeout(deadline); if (controller === abort) controller = undefined; pending = undefined;
        });
        await pending;
        // First Fabric activation may replace its executable while ensuring the runtime.
        // Probe the new loadout once; repeated drift remains unknown, never an unbounded retry.
        if (retry && generation !== epoch) await verify(false);
    }
    return { observe, verify, invalidate,
        isDiagnostic: (id: string) => [...diagnostics].some(parent => id.includes(parent)),
        capabilities: (binding: { agent_id: string; session_id: string; cwd: string }, now = Date.now()) => reportCapabilities(pi, binding, now, evidence) };
}
