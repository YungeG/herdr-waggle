/** Opt-in Pi extension. Native confirmation or a single-use fresh-session launch grant. */
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { randomUUID } from "node:crypto";
import { defineTool, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { appendKeyEvent, validateEvents, type Report } from "./journal.ts";
import { observeActivity } from "./activity.ts";
import { supplementChannel } from "./supplements.ts";
import { decisionInstructions, parseNewDecisions, parseStrictJSON } from "./decisions.ts";
import { adoptionChannel, canonical, digest, type AdoptionRequest } from "./session_adoption.ts";
import { reportChannel, reportSchema, requiredReportFeatures, type ReportParameters } from "./capabilities.ts";
import { handoffSchema, parseHandoff } from "./handoff.ts";

const MAX_BYTES = 1024 * 1024;
const uuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
type RunState = "queued" | "working" | "waiting" | "finished" | "failed" | "stopped" | "interrupted" | "abandoned";
type Delivery = "" | "progress" | "blocked" | "partial" | "full";
interface Run {
    id: string; task_id: number; agent_id: string; session_id: string; cwd: string;
    title: string; description: string; requirements: string;
    state: RunState; seq: number; delivery: Delivery; summary: string;
    checks: string[]; remaining: string; events: string[];
}
const runKeys = ["id", "task_id", "agent_id", "session_id", "cwd", "title", "description", "requirements", "state", "seq", "delivery", "summary", "checks", "remaining", "events"];
const sameKeys = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).sort().join(",") === [...keys].sort().join(",");
const identity = (run: Run) => JSON.stringify([run.id, run.task_id, run.agent_id, run.session_id, run.cwd, run.title, run.description, run.requirements]);
const terminal = (run: Run) => ["finished", "failed", "stopped", "interrupted", "abandoned"].includes(run.state);

function privateStat(info: fs.Stats, directory: boolean) {
    if (!process.getuid || info.uid !== process.getuid() || (info.mode & 0o077) !== 0 || !(directory ? info.isDirectory() : info.isFile())) {
        throw new Error("Mailbox requires owner-only POSIX files/directories, no symlinks.");
    }
}
function privateDir(directory: string, create = false) {
    if (create) fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    privateStat(fs.lstatSync(directory), true);
    if (fs.realpathSync(directory) !== path.resolve(directory)) throw new Error("Mailbox directory must not contain symlinks.");
}
function readJSON(file: string, limit = MAX_BYTES): Record<string, unknown> {
    const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    try {
        const info = fs.fstatSync(fd);
        privateStat(info, false);
        if (info.size > limit) throw new Error("Mailbox record exceeds size limit.");
        const buffer = Buffer.alloc(Math.min(info.size + 1, limit + 1));
        const size = fs.readSync(fd, buffer, 0, buffer.length, 0);
        if (size > limit || size !== info.size) throw new Error("Mailbox record changed while reading.");
        let record: unknown;
        try { record = parseStrictJSON(buffer.subarray(0, size).toString("utf8")); }
        catch { throw new Error("Invalid mailbox JSON; raw input is not included in the error."); }
        if (!record || Array.isArray(record) || typeof record !== "object") throw new Error("Mailbox record must be an object.");
        return record as Record<string, unknown>; // Shape checked above; each consumer validates exact keys/types.
    } finally { fs.closeSync(fd); }
}
function writeJSON(file: string, record: unknown, exclusive = false) {
    privateDir(path.dirname(file));
    const data = JSON.stringify(record);
    if (Buffer.byteLength(data) > MAX_BYTES) throw new Error("Mailbox snapshot exceeds 1 MiB.");
    const temp = path.join(path.dirname(file), `.${randomUUID()}.tmp`);
    const fd = fs.openSync(temp, "wx", 0o600);
    try {
        try { fs.writeFileSync(fd, data); fs.fsyncSync(fd); }
        finally { fs.closeSync(fd); }
        if (exclusive) fs.linkSync(temp, file);
        else {
            if (fs.existsSync(file)) privateStat(fs.lstatSync(file), false);
            fs.renameSync(temp, file);
        }
        const directory = fs.openSync(path.dirname(file), fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
        try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
    } finally { if (fs.existsSync(temp)) fs.unlinkSync(temp); }
}
function parseRun(record: unknown): Run {
    if (!record || typeof record !== "object" || Array.isArray(record) || !sameKeys(record as Record<string, unknown>, runKeys)) throw new Error("Invalid run schema.");
    const row = record as Record<string, unknown>;
    for (const key of runKeys) {
        const value = row[key];
        if (["task_id", "seq"].includes(key)) {
            if (!Number.isSafeInteger(value) || (value as number) < (key === "task_id" ? 1 : 0)) throw new Error("Invalid run integer.");
        } else if (["checks", "events"].includes(key)) {
            if (!Array.isArray(value) || value.some(item => typeof item !== "string")) throw new Error("Invalid run string list.");
        } else if (typeof value !== "string") throw new Error("Invalid run string.");
    }
    // SAFETY: exact keys and primitive field types were checked; semantic invariants are checked below.
    const run = row as unknown as Run;
    if (![run.id, run.agent_id, run.session_id].every(uuid) || !path.isAbsolute(run.cwd) || !run.title.trim()) throw new Error("Invalid run identity.");
    if (!["queued", "working", "waiting", "finished", "failed", "stopped", "interrupted", "abandoned"].includes(run.state) || !["", "progress", "blocked", "partial", "full"].includes(run.delivery)) throw new Error("Invalid run state.");
    validateEvents(run.events, run.id, run.seq);
    return run;
}
function rootDirectory() {
    if (process.env.WAGGLE_BRIDGE_DIR) {
        if (!path.isAbsolute(process.env.WAGGLE_BRIDGE_DIR)) throw new Error("WAGGLE_BRIDGE_DIR must be absolute.");
        return process.env.WAGGLE_BRIDGE_DIR;
    }
    const state = process.env.XDG_STATE_HOME;
    return path.join(state && path.isAbsolute(state) ? state : path.join(os.homedir(), ".local/state"), "waggle/bridges");
}

export default function (pi: ExtensionAPI) {
    let context: ExtensionContext | undefined;
    let endpoint = "", agentId = "", sessionId = "", cwd = "";
    let timer: ReturnType<typeof setInterval> | undefined;
    let active: Run | undefined;
    const channel = reportChannel(pi, () => active?.id);
    let connecting = false, ticking = false;
    let retained: Run | undefined;
    let following = false, choosing = false, deferredSettled = false, continuationContext = false;
    let nativePending = 0, sessionSeq = 0, assistantAborted = false, sessionUnavailable = false;
    let nativeTurnOpen = false;
    let adoptionContext: AdoptionRequest | undefined;
    let acknowledged = false, stopRequested = false, ownershipLost = false, broken = false;
    let assistantError = "", promptDepth = 0, lastHeartbeat = 0;
    // pi-context intentionally settles before its deferred same-session continuation.
    // Keep an in-memory receipt, never revive a terminal run or adopt a new session.
    let compaction: { callId: string; origin: string; summary: string; target?: string; armed: boolean; navigated: boolean } | undefined;
    function hasCompactionReceipt(ctx: ExtensionContext, fromId?: string): boolean {
        const pending = compaction;
        if (!pending) return false;
        const branch = ctx.sessionManager.getBranch(fromId);
        const origin = branch.findIndex(entry => entry.id === pending.origin);
        return origin >= 0 && branch.some((entry, index) => index > origin && entry.type === "message" &&
            entry.message.role === "toolResult" && entry.message.toolCallId === pending.callId &&
            entry.message.toolName === "context_compact" && !entry.message.isError);
    }

    const notify = (message: string, error = false) => context?.ui.notify(`Waggle: ${message}`, error ? "error" : "info");
    const idle = () => Boolean(context?.isIdle() && !context.hasPendingMessages() && context.ui.getEditorText().length === 0);
    const matches = (ctx: ExtensionContext) => ctx.sessionManager.getSessionId() === sessionId && fs.realpathSync(ctx.cwd) === cwd;
    const runFile = (id: string) => path.join(endpoint, "runs", `${id}.json`);
    const activity = observeActivity(pi, () => {
        if (!active || !acknowledged || ownershipLost || broken || terminal(active) || !context || !matches(context)) return;
        return active;
    }, snapshot => writeJSON(path.join(endpoint, "activity.json"), snapshot), channel.isDiagnostic);
    const supplements = supplementChannel(pi, { read: readJSON, write: writeJSON, directory: privateDir }, () => {
        if (!active || !acknowledged || broken || stopRequested || ownershipLost || terminal(active) || !context || !matches(context)) return;
        return active;
    }, () => Boolean(context && active?.state === "working" && !promptDepth && !compaction && !choosing && !nativePending && !context.isIdle() &&
        !context.hasPendingMessages() && context.ui.getEditorText().length === 0), (run, kind, text) => {
        if (!active || active !== run) throw new Error("Supplement run changed before persistence.");
        save(active, kind, text);
    }, async text => {
        if (!text.includes("结构化人工决定 · 草稿不是批准")) return true;
        await channel.verify();
        const proof = channel.capabilities({ agent_id: agentId, session_id: sessionId, cwd });
        return proof.callable && requiredReportFeatures(text, true).every(feature => proof.features.includes(feature));
    });
    const adoptions = adoptionChannel({ read: readJSON, write: writeJSON, directory: privateDir }, () => ({
        endpoint, agent_id: agentId, session_id: sessionId, cwd, pid: process.pid,
        idle: idle() && !nativeTurnOpen && !choosing && !promptDepth && !compaction,
        owned: Boolean(active), leaf: context?.sessionManager.getLeafId() || "",
    }), request => {
        if (!context || !matches(context)) throw new Error("Session/cwd changed before attachment.");
        if (!request.source) return;
        const source = request.source;
        const directory = path.join(rootDirectory(), source.agent_id);
        privateDir(directory); privateDir(path.join(directory, "runs"));
        const originalAgent = readJSON(path.join(directory, "agent.json"), 16384);
        const parent = parseRun(readJSON(path.join(directory, "runs", `${source.run_id}.json`)));
        if (!terminal(parent) || parent.task_id !== request.task_id || parent.session_id !== sessionId || parent.cwd !== cwd ||
            parent.agent_id !== source.agent_id || parent.seq !== source.seq || digest(parent) !== source.digest ||
            parent.title !== request.title || parent.description !== request.description ||
            originalAgent.version !== 1 || originalAgent.id !== source.agent_id || originalAgent.session_id !== sessionId ||
            originalAgent.cwd !== cwd || originalAgent.pid !== source.pid || originalAgent.active_run) throw new Error("Original terminal identity or execution changed.");
        if (fs.existsSync(path.join(directory, "runs", `${source.run_id}.next.json`))) throw new Error("Original run already has a continuation; refresh before attachment.");
    });
    function reportHeartbeat(observedAt = Date.now()) {
        if (!endpoint) return;
        try { writeJSON(path.join(endpoint, "report-capabilities.json"), channel.capabilities({ agent_id: agentId, session_id: sessionId, cwd }, observedAt)); }
        catch { notify("报告能力证明暂不可写；旧执行仍可观察，新请求须先核对连接。", true); }
    }
    function heartbeat(online = true) {
        if (!endpoint || !context) return;
        const observedAt = Date.now();
        reportHeartbeat(observedAt); // No successful verification timestamp is invented by a heartbeat.
        writeJSON(path.join(endpoint, "adoption-capabilities.json"), adoptions.capabilities(observedAt));
        writeJSON(path.join(endpoint, "agent.json"), {
            version: 1, id: agentId, session_id: sessionId, cwd,
            name: context.sessionManager.getSessionName() || `Pi ${process.pid}`,
            model: context.model ? `${context.model.provider}/${context.model.id}` : "未选模型",
            pid: process.pid, heartbeat: observedAt, online,
            idle: online && !connecting && !active && idle(), active_run: active?.id || "",
        });
        lastHeartbeat = Date.now();
        // Separate native activity from agent.json's backwards-compatible dispatch availability.
        try {
            writeJSON(path.join(endpoint, "session.json"), {
                version: 1, agent_id: agentId, session_id: sessionId, cwd, seq: ++sessionSeq, observed_at: Date.now(),
                state: !online || broken ? "offline" : choosing || promptDepth ? "waiting" : context.isIdle() ? "idle" : "working",
                run_id: active && !ownershipLost && !broken ? active.id : "", linked_run_id: retained?.id || adoptions.bound()?.source?.run_id || "",
                following: following && Boolean(retained), input_pending: choosing || nativePending > 0,
            });
            sessionUnavailable = false;
        } catch {
            if (!sessionUnavailable) notify("会话观测暂不可写；工作台须核实当前活动，执行记录仍独立保存。", true);
            sessionUnavailable = true; // Optional telemetry does not revoke an otherwise valid execution receipt.
        }
        activity.pulse();
    }
    function save(run: Run, kind?: string, text = "", exclusive = false, report: Report | null = null) {
        if (kind) run.events = appendKeyEvent(run.events, kind, text, run.id, run.seq + 1, report);
        run.seq += 1;
        writeJSON(runFile(run.id), run, exclusive);
    }
    function failClosed(error: unknown) {
        broken = true;
        activity.invalidate();
        if (timer) clearInterval(timer);
        timer = undefined;
        notify(`桥接已暂停：${String(error)}。不会重试执行；请在 Pi 中核实当前工作。`, true);
        try { heartbeat(false); } catch { /* Disk may be unavailable; stale heartbeat stays explicitly unknown. */ }
    }
    function guarded(action: () => void) {
        try { action(); } catch (error) { failClosed(error); }
    }
    function reject(run: Run, reason: string, state: RunState = "failed") {
        run.state = state; run.summary = reason; run.delivery = ""; run.checks = []; run.remaining = run.requirements;
        save(run, state, reason, true);
    }
    function requestAbort() {
        if (!context || !active || !acknowledged || ownershipLost || choosing || nativePending || context.hasPendingMessages()) {
            if (active) save(active, "warning", "停止尚未执行：未确认归属或存在额外输入，请到 Pi 核实；不会终止其他工作。");
            return;
        }
        if (compaction) {
            save(active, "warning", "停止尚未执行：上下文压缩正在交接，无法取消外部扩展的待续接回调；请在 Pi 核实，续接后再确认停止。");
            return;
        }
        stopRequested = true;
        save(active, "stop_requested", "收到人工停止请求 · 正在等待 Pi 完全空闲");
        context.abort(); // Pi returns void; only agent_settled confirms completion.
    }
    const EVIDENCE_ONLY_PREFIX = "仅补证请求："; // Must match decisions.py; a run marker, never a grant.
    function finalReportInstructions(requirements: string): string {
        const contract = requirements.startsWith(EVIDENCE_ONLY_PREFIX)
            ? "This run only gathers evidence. Report final partial or blocked with the original task still pending; full is rejected even if evidence collection succeeded."
            : "Use waggle_report for progress and final blocked, partial or full delivery. Full requires concrete checks and no remaining work.";
        const route = channel.capabilities({ agent_id: agentId, session_id: sessionId, cwd }).transport === "fabric"
            ? " Invoke it inside fabric_exec with await extensions.waggle_report(args); this is not a top-level tool call." : "";
        return contract + route + " Supply handoff in waggle_report. " + handoffSchema.description;
    }
    function dispatch(run: Run, expires: number) {
        const existing = runFile(run.id);
        if (fs.existsSync(existing)) {
            if (identity(parseRun(readJSON(existing))) !== identity(run)) throw new Error("Duplicate run UUID with different immutable input.");
            return; // Even a crashed reservation is never injected a second time.
        }
        if (Date.now() > expires) return reject(run, "派发请求已过期；未执行，也不会自动重试。");
        if (run.state !== "queued" || run.seq !== 0 || run.delivery || run.events.length || run.summary || run.checks.length || run.remaining) return reject(run, "派发必须是新的空白执行意图。");
        if (run.title.length > 300 || run.description.length > 20000 || run.requirements.length > 6000) return reject(run, "任务说明超出桥接上限；请缩短后另行派发。");
        if (!context || !matches(context) || run.cwd !== cwd || fs.realpathSync(run.cwd) !== cwd) return reject(run, "精确项目目录或 Pi 会话已改变；未执行。");
        if (active || !idle()) return reject(run, "Pi 正忙、有待处理消息或编辑器未清空；未执行。");
        if (adoptions.bound() && adoptions.bound()?.task_id !== run.task_id) return reject(run, "Pi 已纳管其他任务；请先明确解除关联，未执行。");
        const capability = channel.capabilities({ agent_id: agentId, session_id: sessionId, cwd });
        if (!capability.callable || !requiredReportFeatures(run.requirements).every(feature => capability.features.includes(feature)))
            return reject(run, "当前 Pi 的报告工具不能接收本次要求；未执行，请先核对桥接连接，不要重复发送。");
        save(run, "origin", context.sessionManager.getSessionFile() || "", true);
        adoptions.consume(run.id);
        active = retained = run; following = false; nativePending = 0; continuationContext = false;
        acknowledged = false; stopRequested = false; ownershipLost = false; assistantError = ""; assistantAborted = false; promptDepth = 0; compaction = undefined;
        showBinding(); heartbeat();
        pi.sendMessage({
            customType: "waggle-task", display: true, details: { runId: run.id },
            content: `Waggle task (explicit human dispatch)\nRUN_ID=${run.id}\nProject: ${cwd}\nTask: ${run.title}\n\n${run.description}\n\nRework / remaining requirements:\n${run.requirements || "None"}\n\nWork only on this task in the exact project directory. Keep ordinary Pi approval gates. ${decisionInstructions} ${finalReportInstructions(run.requirements)} Do not label unrun tests as passed. Also provide brief: actions (1-3 concrete short actions, each <=100 characters), outcome (<=160 characters), verification (<=160 characters, explicitly say unrun/unverified checks). Keep summary as the complete structured report with line breaks, and checks as concrete evidence. Never infer success from tool counts or hide remaining work in a brief. A report and turn end are NOT human acceptance. If work is blocked, report why rather than inventing results.`,
        }, { triggerTurn: true });
    }
    function stop(run: Run, expires: number) {
        if (Date.now() > expires) {
            if (active?.id === run.id) save(active, "warning", "停止请求已过期；请重新确认停止。");
            return;
        }
        if (fs.existsSync(runFile(run.id))) {
            const previous = parseRun(readJSON(runFile(run.id)));
            if (identity(previous) !== identity(run)) throw new Error("Stop identity mismatch.");
            if (terminal(previous)) return;
            if (active?.id === run.id) requestAbort();
        } else {
            // A stop may overtake a queued dispatch. Persist a tombstone; do not abort unrelated Pi work.
            reject(run, "人工取消：该执行尚未注入 Pi。", "stopped");
        }
    }
    async function consume(file: string) {
        const command = readJSON(file);
        if (!sameKeys(command, ["version", "id", "kind", "agent_id", "session_id", "expires", "run"]) || command.version !== 1 || !uuid(command.id) || path.basename(file) !== `${command.id}.json` || command.agent_id !== agentId || command.session_id !== sessionId || !Number.isSafeInteger(command.expires) || (command.expires as number) > Date.now() + 60000 || !["dispatch", "stop"].includes(String(command.kind))) throw new Error("Invalid command schema/identity/expiry.");
        const run = parseRun(command.run);
        if (run.agent_id !== agentId || run.session_id !== sessionId || run.cwd !== cwd) throw new Error("Command/run identity mismatch.");
        if (command.kind === "dispatch") {
            await channel.verify(); // Effective route, not a cached registry-only check, BEFORE reservation.
            if (!context || !endpoint || run.agent_id !== agentId || run.session_id !== sessionId || !matches(context))
                throw new Error("Pi connection changed during report-channel verification; command retained.");
            dispatch(run, command.expires as number); // Rechecks expiry, idle, identity and schema after the await.
        } else stop(run, command.expires as number);
    }
    async function tick() {
        if (ticking || !endpoint || broken || !context) return;
        const expectedEndpoint = endpoint;
        ticking = true;
        try {
            if (!matches(context)) throw new Error("Pi session/cwd changed; reconnect explicitly.");
            const directory = path.join(endpoint, "commands");
            privateDir(directory);
            // One bounded consumer: awaiting a probe never allows a second reservation/injection.
            for (const name of fs.readdirSync(directory).filter(name => name.endsWith(".json")).sort().slice(0, 8)) {
                const file = path.join(directory, name);
                await consume(file);
                if (endpoint !== expectedEndpoint || broken) return;
                fs.unlinkSync(file);
            }
            await supplements.tick();
            if (endpoint !== expectedEndpoint || broken) return;
            const previousAttachment = adoptions.bound()?.id;
            adoptions.tick();
            if (previousAttachment !== adoptions.bound()?.id) { showBinding(); heartbeat(); }
            if (connecting || Date.now() - lastHeartbeat >= 2000) {
                await channel.verify();
                if (endpoint !== expectedEndpoint || broken) return;
                connecting = false;
                heartbeat();
            }
        } catch (error) {
            // Old asynchronous work cannot pause a later explicitly connected endpoint.
            if (endpoint === expectedEndpoint) failClosed(error);
        } finally { ticking = false; }
    }
    function disconnect(reason: string) {
        if (timer) clearInterval(timer);
        timer = undefined;
        channel.invalidate(); connecting = false;
        if (active) {
            active.state = "interrupted"; active.delivery = ""; active.summary = `Pi 连接结束 (${reason})；没有确认完成。`;
            guarded(() => {
                if (active) save(active, "interrupted", active.summary);
                supplements.invalidate(active, "连接结束；尚未接收的补充不再投递。");
            });
            active = undefined;
        }
        retained = undefined; following = false; nativePending = 0; choosing = false; deferredSettled = false;
        guarded(() => adoptions.disconnect());
        adoptionContext = undefined;
        guarded(() => heartbeat(false));
        context?.ui.setStatus("waggle", undefined);
        endpoint = ""; agentId = ""; sessionId = ""; cwd = ""; broken = false; compaction = undefined;
    }

    function activate(ctx: ExtensionContext, exact: string, session: string, id: string = randomUUID()) {
        context = ctx;
        if (endpoint || !ctx.hasUI || !["tui", "rpc"].includes(ctx.mode) || !idle() || ctx.sessionManager.getSessionId() !== session || fs.realpathSync(ctx.cwd) !== exact) {
            throw new Error("Pi changed before connection; no session was adopted.");
        }
        const root = rootDirectory(); privateDir(root, true);
        const nextEndpoint = path.join(root, id);
        fs.mkdirSync(nextEndpoint, { mode: 0o700 }); // Never reuse/overwrite an existing endpoint.
        privateDir(nextEndpoint);
        privateDir(path.join(nextEndpoint, "commands"), true); privateDir(path.join(nextEndpoint, "runs"), true);
        cwd = exact; sessionId = session; agentId = id; endpoint = nextEndpoint;
        adoptions.activate(endpoint);
        broken = false; // A new explicit consent may recover a failed pre-activation grant.
        channel.invalidate(); connecting = true;
        // Public re-registration refreshes Fabric capture at explicit connection only.
        // Do not reactivate a tool the user hid/disabled, or rewrite a downgraded schema.
        const schema = channel.capabilities({ agent_id: agentId, session_id: sessionId, cwd });
        if (schema.active && ["report-v1", "handoff-v1", "decisions-v3"].every(feature => schema.features.includes(feature))) pi.registerTool(reportTool);
        supplements.activate(endpoint, { agent_id: agentId, session_id: sessionId, cwd });
        heartbeat(); // Observable now, available after the first bounded probe (after all session_start handlers).
        timer = setInterval(() => { void tick(); }, 250);
        timer.unref();
        ctx.ui.setStatus("waggle", `Waggle ${agentId.slice(0, 8)}`);
        notify(`已明确加入 ${cwd} · ${agentId.slice(0, 8)}；等待你在工作台选择并派发。`);
    }
    pi.registerFlag("waggle-launch", { type: "string", description: "Single-use workbench consent for this exact fresh Pi session" });
    pi.on("session_start", (event, ctx) => {
        const file = pi.getFlag("waggle-launch");
        if (event.reason !== "startup" || typeof file !== "string" || !file) return;
        context = ctx;
        guarded(() => {
            const root = rootDirectory(); privateDir(root);
            privateDir(path.join(root, "launches"));
            privateDir(path.dirname(file));
            const grant = readJSON(file);
            if (!sameKeys(grant, ["version", "id", "session_id", "cwd", "expires"]) || grant.version !== 1 || !uuid(grant.id) || !uuid(grant.session_id) || typeof grant.cwd !== "string" || !path.isAbsolute(grant.cwd) || !Number.isSafeInteger(grant.expires)) throw new Error("Invalid launch consent schema.");
            if (file !== path.join(root, "launches", grant.id, "request.json") || (grant.expires as number) < Date.now() || (grant.expires as number) > Date.now() + 600000) throw new Error("Launch consent path/expiry mismatch.");
            if (ctx.sessionManager.getSessionId() !== grant.session_id || fs.realpathSync(ctx.cwd) !== grant.cwd || ctx.sessionManager.getBranch().some(entry => entry.type === "message")) throw new Error("Launch consent only applies to the exact new, empty session.");
            if (!ctx.hasUI || !["tui", "rpc"].includes(ctx.mode) || !idle()) throw new Error("New Pi is not ready; no automatic adoption.");
            // Claim before activation. Even an uncertain crash may never replay this permission.
            fs.linkSync(file, path.join(path.dirname(file), "consumed.json"));
            fs.unlinkSync(file);
            const directory = fs.openSync(path.dirname(file), fs.constants.O_RDONLY | fs.constants.O_DIRECTORY);
            try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
            activate(ctx, grant.cwd, grant.session_id, grant.id);
        });
    });

    pi.registerCommand("waggle", {
        description: "Explicitly connect/disconnect this Pi to the local task workbench",
        handler: async (args, ctx) => {
            context = ctx;
            if (args.trim() === "detach") {
                guarded(() => { detach(); if (ctx.isIdle() && !ctx.hasPendingMessages()) settle(); });
                notify("已解除任务关联；没有停止 Pi，也不会把独立输入计入旧任务。");
                return;
            }
            if (args.trim() === "disconnect") {
                if (active) { notify("仍有本工作台的执行；先停止并等它结束，再 disconnect。", true); return; }
                disconnect("human disconnect");
                notify("已断开；Pi 本身没有退出。");
                return;
            }
            if (args.trim() !== "connect") { notify("使用 /waggle connect、/waggle detach 或 /waggle disconnect；直接输入可确认持续关联当前任务。"); return; }
            if (endpoint) { notify(broken ? "桥接故障；请核实并结束旧执行后断开/重连。" : "本会话已经连接。", broken); return; }
            if (!ctx.hasUI || !["tui", "rpc"].includes(ctx.mode)) { notify("连接需要交互确认。", true); return; }
            if (!idle()) { notify("先让 Pi 完全空闲，并清空编辑器和待处理输入。", true); return; }
            const exact = fs.realpathSync(ctx.cwd), session = ctx.sessionManager.getSessionId();
            const approved = await ctx.ui.confirm("连接 Waggle", `允许本用户的本地任务工作台向此 Pi 派发任务、向仍归属本轮的执行发送补充说明，并请求停止其派发的执行。\n目录：${exact}\n会话：${session}\n不会切换目录、自动派活或绕过 Pi 的权限确认。每次 /reload、换会话或重启都需要重新连接。`);
            if (!approved) return;
            try {
                activate(ctx, exact, session);
            } catch (error) { failClosed(error); }
        },
    });
    const reportTool = defineTool({
        name: "waggle_report", label: "Waggle report",
        description: "Report progress or final delivery for the currently active Waggle RUN_ID only. Full requires concrete evidence and no remaining work; checks are self-reported, human acceptance is separate. " + decisionInstructions,
        parameters: Type.Unsafe<ReportParameters>(reportSchema),
        prepareLoadout(loadout) {
            channel.observe(loadout);
            reportHeartbeat(); // Withdraw stale route evidence immediately on native loadout replacement.
            return undefined;
        },
        async execute(_id, params, _signal, _update, ctx) {
            // A diagnostic remains a no-op even if native tool middleware rewrites its arguments.
            if (channel.isDiagnostic(_id) || _signal?.aborted || !active || broken || !acknowledged || ownershipLost || stopRequested || active.id !== params.run_id || !matches(ctx)) throw new Error("No matching active Waggle run; report rejected.");
            if (compaction) throw new Error("Waggle context handoff is pending; wait for its verified continuation before reporting.");
            if (nativePending || ctx.hasPendingMessages()) throw new Error("Native task input is pending; a precomputed report cannot cover it.");
            supplements.assertReport(active, params.supplement_id);
            if (!params.summary.trim() || params.checks.some(check => !check.trim())) throw new Error("Report text/evidence must be nonempty.");
            if (params.kind === "full" && active.requirements.startsWith(EVIDENCE_ONLY_PREFIX)) throw new Error("Evidence-only run cannot report full completion of the original task.");
            if (params.kind === "full" && (!params.checks.length || params.remaining.trim())) throw new Error("Full delivery requires evidence and no remaining work.");
            if (["blocked", "partial"].includes(params.kind) && !params.remaining.trim()) throw new Error("Blocked/partial delivery requires remaining work or a reason.");
            if (params.brief && (!params.brief.actions.every(action => action.trim()) || !params.brief.outcome.trim() || !params.brief.verification.trim())) throw new Error("Explicit report brief must be nonempty.");
            const handoff = params.handoff === undefined ? undefined : parseHandoff(params.handoff);
            const decisions = params.decisions === undefined ? undefined : parseNewDecisions(params.decisions);
            if (decisions && params.kind === "full") throw new Error("Pending decisions cannot be a full delivery.");
            active.delivery = params.kind; active.summary = params.summary.trim(); active.checks = params.checks; active.remaining = params.remaining.trim();
            const report: Report = { summary: active.summary, checks: active.checks, remaining: active.remaining, brief: params.brief ?? null,
                ...(decisions === undefined ? {} : { decisions }), ...(handoff === undefined ? {} : { handoff }) };
            try {
                save(active, params.kind, `执行者报告 · ${params.kind}`, false, report);
                if (params.kind !== "progress") supplements.cover(active);
            }
            catch (error) { failClosed(error); throw error; }
            return { content: [{ type: "text", text: "报告已保存；只有 Pi 完全空闲后才可进入人工验收。" }], details: { runId: active.id, kind: params.kind }, terminate: params.kind !== "progress" };
        },
    });
    pi.registerTool(reportTool);
    pi.on("message_end", (event, ctx) => guarded(() => {
        context = ctx;
        if (!active || broken) return;
        const message = event.message;
        supplements.acknowledge(message);
        if (message.role === "custom" && message.customType === "waggle-task" && (message.details as { runId?: string } | undefined)?.runId === active.id) {
            acknowledged = true; active.state = "working";
            save(active, "acknowledged", "Pi 已确认对应任务消息 · 开始执行");
        } else if (message.role === "user" && nativePending && !ownershipLost) {
            nativePending -= 1;
            const first = !acknowledged;
            acknowledged = true; active.state = promptDepth ? "waiting" : "working";
            save(active, first ? "acknowledged" : "input_received", "Pi 已接收本任务的直接输入；不是新的工作台派发。");
            heartbeat();
        } else if (acknowledged && message.role === "custom" && message.customType === "pi-context") {
            if (stopRequested || ownershipLost) return;
            if (!compaction?.armed) {
                ownershipLost = true; active.delivery = "";
                activity.invalidate();
                save(active, "invalidated", "Pi 收到未经本轮请求的上下文续接；不接管该输入，也不接受本轮交付。");
                supplements.invalidate(active, "归属失效；未接收的补充不会用于该输入。");
                return;
            }
            const text = typeof message.content === "string" ? message.content : message.content.filter(part => part.type === "text").map(part => part.text).join("\n");
            const continued = compaction.navigated && text.startsWith("context_compact complete.") &&
                ctx.sessionManager.getBranch().some(entry => entry.id === compaction?.target);
            const unchanged = !compaction.target && /^(context_compact cancelled:|context_compact failed:)/.test(text) && hasCompactionReceipt(ctx);
            if (!matches(ctx) || (!continued && !unchanged)) throw new Error("Cannot verify the pi-context continuation; task binding remains uncertain.");
            compaction = undefined; assistantError = ""; active.state = promptDepth ? "waiting" : "working";
            save(active, continued ? undefined : "warning", "上下文压缩取消/失败 · 原分支续接已核验，不重放任务");
            heartbeat();
        } else if (acknowledged && message.role === "assistant") {
            assistantAborted = message.stopReason === "aborted";
            assistantError = ["error", "aborted"].includes(message.stopReason) ? message.errorMessage || message.stopReason : "";
            if (assistantError) save(active, "warning", `Pi 响应错误 · ${assistantError}`);
        }
    }));
    pi.on("tool_call", (event, ctx) => {
        if (!active || !acknowledged || event.toolName !== "context_compact") return;
        if (broken || stopRequested || ownershipLost || compaction || choosing || nativePending || supplements.queued() || !matches(ctx)) return { block: true, reason: "Waggle cannot authorize another context handoff in this state." };
        const origin = ctx.sessionManager.getLeafId();
        if (!origin || typeof event.input.summary !== "string") return { block: true, reason: "Invalid Waggle context handoff." };
        // Reserve before execution too: another tool_result handler may await UI/I/O.
        compaction = { callId: event.toolCallId, origin, summary: event.input.summary, armed: false, navigated: false };
    });
    pi.on("tool_result", (event, ctx) => guarded(() => {
        if (!active || !acknowledged || broken || event.toolName !== "context_compact" || event.toolCallId !== compaction?.callId) return;
        if (event.isError || !event.content.some(part => part.type === "text" && part.text === "compact start")) {
            compaction = undefined;
            return;
        }
        if (!matches(ctx)) throw new Error("Compaction changed the Waggle session or directory.");
        compaction.armed = true;
        const invalidated = ["full", "partial", "blocked"].includes(active.delivery);
        active.delivery = "";
        save(active, invalidated ? "invalidated" : undefined, "上下文交接使此前交付失效；需在续接后重新报告，尚未结束任务");
    }));
    pi.on("tool_execution_end", event => guarded(() => {
        if (channel.isDiagnostic(event.toolCallId)) return;
        if (!active || !acknowledged || broken || !event.isError) return;
        const invalidated = active.delivery === "full";
        if (invalidated) {
            active.delivery = "partial"; active.remaining = `完整报告之后工具 ${event.toolName} 失败，需要重新核实交付。`;
        }
        save(active, invalidated ? "invalidated" : "warning", invalidated ? active.remaining : `工具失败 · ${event.toolName}；不代表任务已完成，需要后续报告说明影响。`);
    }));
    pi.on("ui_prompt_start", () => guarded(() => {
        if (!endpoint || broken) return;
        promptDepth += 1;
        if (active && acknowledged && !ownershipLost) {
            active.state = "waiting";
            save(active, "waiting", "等待 Pi 窗口中的人工确认；工作台不会自动回答。");
        }
        heartbeat();
    }));
    pi.on("ui_prompt_end", () => guarded(() => {
        if (!endpoint || broken) return;
        promptDepth = Math.max(0, promptDepth - 1);
        if (!promptDepth && active && acknowledged && !ownershipLost) {
            active.state = "working"; save(active, "resumed", "Pi 人工确认已返回；继续观察执行。");
        }
        heartbeat();
    }));
    function showBinding() {
        if (endpoint && !retained && adoptions.bound()) {
            context?.ui.setStatus("waggle", `Waggle D${String(adoptions.bound()?.task_id).padStart(3, "0")} · 已纳管/未启动 · /waggle detach 解除`);
            return;
        }
        if (endpoint) context?.ui.setStatus("waggle", retained
            ? `Waggle D${String(retained.task_id).padStart(3, "0")} · ${following ? "本任务输入已关联" : "直接输入需确认归属"} · /waggle detach 解除`
            : `Waggle ${agentId.slice(0, 8)} · 未关联任务`);
    }
    function detach() {
        adoptions.invalidate("用户已解除关联或会话分支改变；不停止原生工作，不自动恢复。");
        adoptionContext = undefined;
        retained = undefined; following = false; nativePending = 0; continuationContext = false;
        if (active && !broken) {
            ownershipLost = true; active.delivery = "";
            save(active, "invalidated", "Pi 收到额外输入或解除关联；不再推断属于本任务，不停止可能无关的工作。");
            supplements.invalidate(active, "任务关联已解除；未接收的补充不带入独立输入。");
        }
        activity.invalidate(); showBinding(); heartbeat();
    }
    function continueAttempt(parent: Run) {
        const requirements = [...new Set([parent.requirements, parent.remaining, ...supplements.unresolved(parent)]
            .map(value => value.trim()).filter(Boolean))].join("\n\n");
        if ([...requirements].length > 6000) throw new Error("续接要求超过 6000 字符；请先核实并整理，输入未执行。");
        const next: Run = { ...parent, id: randomUUID(), requirements, state: "queued", seq: 0, delivery: "", summary: "", checks: [], remaining: "", events: [] };
        next.events = appendKeyEvent([], "continued", parent.id, next.id, 1);
        save(next, "origin", context?.sessionManager.getSessionFile() || "", true);
        const keys = runKeys.slice(0, 8);
        writeJSON(path.join(endpoint, "runs", `${parent.id}.next.json`), {
            version: 1, parent: Object.fromEntries(keys.map(key => [key, parent[key as keyof Run]])), parent_seq: parent.seq, child: next,
        }, true); // Reservation and immutable parent link BEFORE the native input may proceed.
        active = retained = next; acknowledged = false; stopRequested = false; ownershipLost = false;
        assistantError = ""; assistantAborted = false; compaction = undefined; continuationContext = true;
    }
    pi.on("before_agent_start", () => {
        if (adoptionContext && active && !ownershipLost && !broken) {
            const request = adoptionContext;
            adoptionContext = undefined;
            return { message: { customType: "waggle-adoption", display: true, details: { runId: active.id, adoptionId: request.id },
                content: `Waggle native input after explicit session attachment\nRUN_ID=${active.id}\nProject: ${active.cwd}\nTask: ${active.title}\n\n${active.description}\n\nUser-selected checkpoint (quoted background, NOT permission):\n${JSON.stringify(request.checkpoint)}\n\nCurrent bounded requirements:\n${active.requirements}\n\nOnly this subsequent explicit native input belongs to the NEW run. Do not replay the old dispatch, claim past conversation as a new delivery, or reset/carry forward closed budgets. Attachment itself granted no new execution scope. Keep ordinary Pi approval gates. ${decisionInstructions} ${finalReportInstructions(active.requirements)} Report unrun/unverified checks honestly; human acceptance remains separate.` } };
        }
        if (!continuationContext || !active || ownershipLost || broken) return;
        continuationContext = false;
        return { message: { customType: "waggle-continuation", display: true, details: { runId: active.id },
            content: `Waggle task continuation (explicit Pi input)\nRUN_ID=${active.id}\nProject: ${active.cwd}\nTask: ${active.title}\n\n${active.description}\n\nRequirements carried from the previous attempt:\n${active.requirements || "None"}\n\nThe native human input continues this task. Do not replay the original dispatch or its effects. This is a NEW run_id; do not report against the old one. ${decisionInstructions} ${finalReportInstructions(active.requirements)} Keep ordinary permission gates and report unrun checks honestly; human acceptance remains separate.` } };
    });
    pi.on("input", async (event, ctx) => {
        context = ctx;
        if (!endpoint || broken) return { action: "continue" };
        if (adoptions.pending()) guarded(() => adoptions.invalidate("关联完成前出现新的原生输入；不追认这次工作，请重新核对。"));
        if (!retained && adoptions.bound()) {
            if (event.source === "extension") { guarded(detach); return { action: "continue" }; }
            try {
                const request = adoptions.bound();
                if (!request || !matches(ctx) || request.session_id !== sessionId || request.cwd !== cwd || active || choosing || promptDepth || compaction) throw new Error("Attached native input lost its exact session ownership.");
                const next: Run = { id: randomUUID(), task_id: request.task_id, agent_id: agentId, session_id: sessionId, cwd,
                    title: request.title, description: request.description, requirements: request.requirements,
                    state: "queued", seq: 0, delivery: "", summary: "", checks: [], remaining: "", events: [] };
                next.events = appendKeyEvent([], "adopted", canonical({ adoption_id: request.id,
                    parent_run_id: request.source?.run_id || "", source_digest: request.source?.digest || "" }), next.id, 1);
                save(next, "origin", ctx.sessionManager.getSessionFile() || "", true);
                adoptions.consume(next.id);
                active = retained = next; following = true; nativePending = 1;
                acknowledged = false; stopRequested = false; ownershipLost = false; assistantError = ""; assistantAborted = false;
                adoptionContext = request;
                save(next, "direct_input", "用户在已明确纳管的会话继续输入；只记录此检查点之后的新轮，未重放原工作。");
                showBinding(); heartbeat();
                return { action: "continue" };
            } catch (error) { failClosed(error); return { action: "handled" }; }
        }
        if (!retained) return { action: "continue" };
        if (event.source === "extension") { guarded(detach); return { action: "continue" }; }
        if (choosing || stopRequested || (active && !acknowledged && !nativePending)) {
            notify("本任务仍有归属确认、停止或上下文交接待处理；本次输入未提交，请稍后重试或明确解除关联。", true);
            return { action: "handled" };
        }
        try {
            if (!matches(ctx)) throw new Error("Pi session/cwd changed before direct input.");
            if (!following || compaction) {
                choosing = true; heartbeat();
                const choice = await ctx.ui.select(`本次输入属于 D${String(retained.task_id).padStart(3, "0")} 吗？`,
                    ["继续当前任务", "独立输入（解除任务关联）", "取消输入"]);
                choosing = false;
                if (choice !== "继续当前任务" && choice !== "独立输入（解除任务关联）") {
                    if (event.source === "interactive") ctx.ui.setEditorText(event.text);
                    if (deferredSettled) { deferredSettled = false; settle(); }
                    heartbeat(); return { action: "handled" };
                }
                if (choice === "独立输入（解除任务关联）") {
                    detach();
                    if (deferredSettled) { deferredSettled = false; settle(); }
                    return { action: "continue" };
                }
                if (broken || !retained || !matches(ctx)) throw new Error("Task binding changed during confirmation; input not submitted.");
                if (compaction) {
                    if (event.source === "interactive") ctx.ui.setEditorText(event.text);
                    notify("本任务的上下文交接尚未确认；本次输入未提交，交接完成后再继续。", true);
                    if (deferredSettled) { deferredSettled = false; settle(); }
                    heartbeat(); return { action: "handled" };
                }
                following = true;
            }
            if (deferredSettled) { deferredSettled = false; settle(); }
            if (retained && terminal(retained)) continueAttempt(retained);
            if (!active || ownershipLost) throw new Error("No owned task for direct input.");
            nativePending += 1; active.delivery = "";
            save(active, "direct_input", "用户在 Pi 确认继续本任务；等待原生输入接收，此前交付需重新报告。");
            showBinding(); heartbeat();
            return { action: "continue" }; // Keep native text/images, expansion and steer/follow-up semantics.
        } catch (error) {
            choosing = false; failClosed(error);
            return { action: "handled" }; // An uncertain reservation is never automatically retried or injected.
        }
    });
    function settle() {
        if (!active || (!acknowledged && !ownershipLost) || broken) return;
        if (choosing) { deferredSettled = true; return; }
        if (nativePending && !ownershipLost) { heartbeat(); return; }
        if (compaction?.armed && !stopRequested && !ownershipLost) {
            active.state = "working";
            save(active); // Keep the handoff state durable without a compaction/tool transcript.
            heartbeat();
            return;
        }
        if (stopRequested || ownershipLost || assistantError) {
            active.state = stopRequested ? "stopped" : ownershipLost ? "interrupted" : assistantAborted ? "stopped" : "failed";
            active.delivery = "";
            active.summary = stopRequested ? "人工停止已确认；Pi 已完全空闲。" : ownershipLost ? "额外输入使执行归属中断；请核实成果后另行安排。" : assistantAborted ? "Pi 执行中止并已空闲（中止来源未确认）；任务未验收。" : `Pi 响应失败：${assistantError}`;
        } else if (["full", "partial", "blocked"].includes(active.delivery)) {
            active.state = active.delivery === "blocked" ? "failed" : "finished";
        } else {
            active.state = "failed"; active.summary = "Pi 已空闲，但未提交最终 waggle_report；不能认为任务完成。";
        }
        save(active, active.state === "finished" ? "settled" : active.state,
            (active.state === "finished" ? "" : active.summary + "\n") + "Pi agent_settled · 执行完全空闲，人工验收仍独立");
        supplements.invalidate(active, "本轮已结束；尚未接收的补充不重放，也不启动下一轮。");
        active = undefined; acknowledged = false; compaction = undefined; showBinding(); heartbeat();
    }
    pi.on("agent_start", (_event, ctx) => guarded(() => { context = ctx; nativeTurnOpen = true; heartbeat(); }));
    pi.on("agent_settled", (_event, ctx) => guarded(() => {
        context = ctx; nativeTurnOpen = false; settle();
        adoptions.tick(); showBinding(); heartbeat();
    }));
    const protectNavigation = () => active ? { cancel: true } : undefined;
    pi.on("session_before_tree", (event, ctx) => {
        if (!active) return;
        let allowed = false;
        guarded(() => {
            const entry = ctx.sessionManager.getEntry(event.preparation.targetId);
            if (compaction?.armed && !compaction.target && !broken && !stopRequested && !ownershipLost && matches(ctx) &&
                entry?.type === "branch_summary" && entry.summary.endsWith(`\n${compaction.summary}`) && hasCompactionReceipt(ctx, entry.fromId)) {
                compaction.target = entry.id;
                allowed = true;
            }
        });
        return allowed ? undefined : { cancel: true };
    });
    pi.on("session_tree", (event, ctx) => guarded(() => {
        if (!active) { if (endpoint) detach(); return; }
        if (broken) return;
        if (!compaction || event.newLeafId !== compaction.target || !matches(ctx)) throw new Error("Unverified navigation during a Waggle run.");
        compaction.navigated = true;
    }));
    pi.on("session_before_switch", protectNavigation);
    pi.on("session_before_fork", protectNavigation);
    pi.on("session_shutdown", event => disconnect(event.reason));
}
