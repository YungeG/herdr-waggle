/** Opt-in Pi extension. Native confirmation or a single-use fresh-session launch grant. */
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

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
function readJSON(file: string): Record<string, unknown> {
    const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    try {
        const info = fs.fstatSync(fd);
        privateStat(info, false);
        if (info.size > MAX_BYTES) throw new Error("Mailbox record exceeds 1 MiB.");
        const buffer = Buffer.alloc(Math.min(info.size + 1, MAX_BYTES + 1));
        const size = fs.readSync(fd, buffer, 0, buffer.length, 0);
        if (size > MAX_BYTES || size !== info.size) throw new Error("Mailbox record changed while reading.");
        const record = JSON.parse(buffer.subarray(0, size).toString("utf8"));
        if (!record || Array.isArray(record) || typeof record !== "object") throw new Error("Mailbox record must be an object.");
        return record;
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
    const run = row as unknown as Run;
    if (![run.id, run.agent_id, run.session_id].every(uuid) || !path.isAbsolute(run.cwd) || !run.title.trim()) throw new Error("Invalid run identity.");
    if (!["queued", "working", "waiting", "finished", "failed", "stopped", "interrupted", "abandoned"].includes(run.state) || !["", "progress", "blocked", "partial", "full"].includes(run.delivery)) throw new Error("Invalid run state.");
    return run;
}
function rootDirectory() {
    if (process.env.TASKROOM_BRIDGE_DIR) {
        if (!path.isAbsolute(process.env.TASKROOM_BRIDGE_DIR)) throw new Error("TASKROOM_BRIDGE_DIR must be absolute.");
        return process.env.TASKROOM_BRIDGE_DIR;
    }
    const state = process.env.XDG_STATE_HOME;
    return path.join(state && path.isAbsolute(state) ? state : path.join(os.homedir(), ".local/state"), "taskroom/bridges");
}

export default function (pi: ExtensionAPI) {
    let context: ExtensionContext | undefined;
    let endpoint = "", agentId = "", sessionId = "", cwd = "";
    let timer: ReturnType<typeof setInterval> | undefined;
    let active: Run | undefined;
    let acknowledged = false, stopRequested = false, ownershipLost = false, broken = false;
    let assistantError = "", promptDepth = 0, lastHeartbeat = 0;

    const notify = (message: string, error = false) => context?.ui.notify(`Taskroom: ${message}`, error ? "error" : "info");
    const idle = () => Boolean(context?.isIdle() && !context.hasPendingMessages() && context.ui.getEditorText().length === 0);
    const matches = (ctx: ExtensionContext) => ctx.sessionManager.getSessionId() === sessionId && fs.realpathSync(ctx.cwd) === cwd;
    const runFile = (id: string) => path.join(endpoint, "runs", `${id}.json`);
    function heartbeat(online = true) {
        if (!endpoint || !context) return;
        writeJSON(path.join(endpoint, "agent.json"), {
            version: 1, id: agentId, session_id: sessionId, cwd,
            name: context.sessionManager.getSessionName() || `Pi ${process.pid}`,
            model: context.model ? `${context.model.provider}/${context.model.id}` : "未选模型",
            pid: process.pid, heartbeat: Date.now(), online,
            idle: online && !active && idle(), active_run: active?.id || "",
        });
        lastHeartbeat = Date.now();
    }
    function save(run: Run, event?: string, exclusive = false) {
        if (event) run.events.push(event.slice(0, 800));
        // ponytail: bounded per-run journal; keep the newest 119 events plus a truncation notice.
        if (run.events.length > 120) run.events = ["较早进展已截断；完整工具记录请查看原 Pi 会话。", ...run.events.slice(-119)];
        run.seq += 1;
        writeJSON(runFile(run.id), run, exclusive);
    }
    function failClosed(error: unknown) {
        broken = true;
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
        save(run, reason, true);
    }
    function requestAbort() {
        if (!context || !active || !acknowledged || ownershipLost || context.hasPendingMessages()) {
            if (active) save(active, "停止尚未执行：未确认归属或存在额外输入，请到 Pi 核实；不会终止其他工作。");
            return;
        }
        stopRequested = true;
        save(active, "收到人工停止请求 · 正在等待 Pi 完全空闲");
        context.abort(); // Pi returns void; only agent_settled confirms completion.
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
        save(run, "Pi 已预留执行身份；等待对应任务消息确认", true);
        active = run; acknowledged = false; stopRequested = false; ownershipLost = false; assistantError = ""; promptDepth = 0;
        heartbeat();
        pi.sendMessage({
            customType: "taskroom-task", display: true, details: { runId: run.id },
            content: `Taskroom task (explicit human dispatch)\nRUN_ID=${run.id}\nProject: ${cwd}\nTask: ${run.title}\n\n${run.description}\n\nRework / remaining requirements:\n${run.requirements || "None"}\n\nWork only on this task in the exact project directory. Keep ordinary Pi approval gates. Use taskroom_report with this run_id for progress and a final blocked, partial or full report. Full requires concrete checks/evidence and no remaining work; do not label unrun tests as passed. A report and turn end are NOT human acceptance. If work is blocked, report why rather than inventing results.`,
        }, { triggerTurn: true });
    }
    function stop(run: Run, expires: number) {
        if (Date.now() > expires) {
            if (active?.id === run.id) save(active, "停止请求已过期；请重新确认停止。");
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
    function consume(file: string) {
        const command = readJSON(file);
        if (!sameKeys(command, ["version", "id", "kind", "agent_id", "session_id", "expires", "run"]) || command.version !== 1 || !uuid(command.id) || path.basename(file) !== `${command.id}.json` || command.agent_id !== agentId || command.session_id !== sessionId || !Number.isSafeInteger(command.expires) || (command.expires as number) > Date.now() + 60000 || !["dispatch", "stop"].includes(String(command.kind))) throw new Error("Invalid command schema/identity/expiry.");
        const run = parseRun(command.run);
        if (run.agent_id !== agentId || run.session_id !== sessionId || run.cwd !== cwd) throw new Error("Command/run identity mismatch.");
        if (command.kind === "dispatch") dispatch(run, command.expires as number);
        else stop(run, command.expires as number);
    }
    function tick() {
        if (!endpoint || broken || !context) return;
        if (!matches(context)) throw new Error("Pi session/cwd changed; reconnect explicitly.");
        const directory = path.join(endpoint, "commands");
        privateDir(directory);
        // Bounded batch prevents a mailbox backlog from monopolizing Pi's event loop.
        for (const name of fs.readdirSync(directory).filter(name => name.endsWith(".json")).sort().slice(0, 8)) {
            const file = path.join(directory, name);
            // Any rejection or uncertain I/O pauses this endpoint via guarded(tick).
            // Retain failed commands for diagnosis; storage recovery never retries execution.
            consume(file);
            fs.unlinkSync(file);
        }
        if (Date.now() - lastHeartbeat >= 2000) heartbeat();
    }
    function disconnect(reason: string) {
        if (timer) clearInterval(timer);
        timer = undefined;
        if (active) {
            active.state = "interrupted"; active.delivery = ""; active.summary = `Pi 连接结束 (${reason})；没有确认完成。`;
            guarded(() => { if (active) save(active, active.summary); });
            active = undefined;
        }
        guarded(() => heartbeat(false));
        context?.ui.setStatus("taskroom", undefined);
        endpoint = ""; agentId = ""; sessionId = ""; cwd = ""; broken = false;
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
        broken = false; // A new explicit consent may recover a failed pre-activation grant.
        heartbeat();
        timer = setInterval(() => guarded(tick), 250);
        timer.unref();
        ctx.ui.setStatus("taskroom", `Taskroom ${agentId.slice(0, 8)}`);
        notify(`已明确加入 ${cwd} · ${agentId.slice(0, 8)}；等待你在工作台选择并派发。`);
    }
    pi.registerFlag("taskroom-launch", { type: "string", description: "Single-use workbench consent for this exact fresh Pi session" });
    pi.on("session_start", (event, ctx) => {
        const file = pi.getFlag("taskroom-launch");
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

    pi.registerCommand("taskroom", {
        description: "Explicitly connect/disconnect this Pi to the local task workbench",
        handler: async (args, ctx) => {
            context = ctx;
            if (args.trim() === "disconnect") {
                if (active) { notify("仍有本工作台的执行；先停止并等它结束，再 disconnect。", true); return; }
                disconnect("human disconnect");
                notify("已断开；Pi 本身没有退出。");
                return;
            }
            if (args.trim() !== "connect") { notify("使用 /taskroom connect 或 /taskroom disconnect；连接后才能被工作台明确分配。"); return; }
            if (endpoint) { notify(broken ? "桥接故障；请核实并结束旧执行后断开/重连。" : "本会话已经连接。", broken); return; }
            if (!ctx.hasUI || !["tui", "rpc"].includes(ctx.mode)) { notify("连接需要交互确认。", true); return; }
            if (!idle()) { notify("先让 Pi 完全空闲，并清空编辑器和待处理输入。", true); return; }
            const exact = fs.realpathSync(ctx.cwd), session = ctx.sessionManager.getSessionId();
            const approved = await ctx.ui.confirm("连接 Taskroom", `允许本用户的本地任务工作台向此 Pi 派发任务并停止其派发的执行。\n目录：${exact}\n会话：${session}\n不会切换目录、自动派活或绕过 Pi 的权限确认。每次 /reload、换会话或重启都需要重新连接。`);
            if (!approved) return;
            try {
                activate(ctx, exact, session);
            } catch (error) { failClosed(error); }
        },
    });
    pi.registerTool({
        name: "taskroom_report", label: "Taskroom report",
        description: "Report progress or final delivery for the currently active Taskroom RUN_ID only. Full requires concrete evidence and no remaining work; checks are self-reported, human acceptance is separate.",
        parameters: Type.Object({
            run_id: Type.String(), kind: StringEnum(["progress", "blocked", "partial", "full"] as const),
            summary: Type.String({ minLength: 1, maxLength: 6000 }),
            checks: Type.Array(Type.String({ minLength: 1, maxLength: 1000 }), { maxItems: 32 }),
            remaining: Type.String({ maxLength: 6000 }),
        }),
        async execute(_id, params, _signal, _update, ctx) {
            if (!active || broken || !acknowledged || ownershipLost || stopRequested || active.id !== params.run_id || !matches(ctx)) throw new Error("No matching active Taskroom run; report rejected.");
            if (!params.summary.trim() || params.checks.some(check => !check.trim())) throw new Error("Report text/evidence must be nonempty.");
            if (params.kind === "full" && (!params.checks.length || params.remaining.trim())) throw new Error("Full delivery requires evidence and no remaining work.");
            if (["blocked", "partial"].includes(params.kind) && !params.remaining.trim()) throw new Error("Blocked/partial delivery requires remaining work or a reason.");
            active.delivery = params.kind; active.summary = params.summary.trim(); active.checks = params.checks; active.remaining = params.remaining.trim();
            try { save(active, `执行者报告 · ${params.kind} · ${active.summary}`); }
            catch (error) { failClosed(error); throw error; }
            return { content: [{ type: "text", text: "报告已保存；只有 Pi 完全空闲后才可进入人工验收。" }], details: { runId: active.id, kind: params.kind }, terminate: params.kind !== "progress" };
        },
    });
    pi.on("message_end", (event, ctx) => guarded(() => {
        context = ctx;
        if (!active || broken) return;
        const message = event.message;
        if (message.role === "custom" && message.customType === "taskroom-task" && (message.details as { runId?: string } | undefined)?.runId === active.id) {
            acknowledged = true; active.state = "working";
            save(active, "Pi 已确认对应任务消息 · 开始执行");
        } else if (acknowledged && message.role === "assistant") {
            assistantError = ["error", "aborted"].includes(message.stopReason) ? message.errorMessage || message.stopReason : "";
            const text = message.content.filter(part => part.type === "text").map(part => part.text).join("\n");
            if (text || assistantError) save(active, assistantError ? `Pi 响应错误 · ${assistantError}` : `Pi · ${text}`);
        }
    }));
    pi.on("tool_execution_start", event => guarded(() => {
        if (active && acknowledged && !broken) save(active, `工具开始 · ${event.toolName}`);
    }));
    pi.on("tool_execution_end", event => guarded(() => {
        if (!active || !acknowledged || broken) return;
        if (event.isError && active.delivery === "full") {
            active.delivery = "partial"; active.remaining = `完整报告之后工具 ${event.toolName} 失败，需要重新核实交付。`;
        }
        save(active, `工具${event.isError ? "失败" : "返回成功"} · ${event.toolName}（不等于测试已验证）`);
    }));
    pi.on("ui_prompt_start", () => guarded(() => {
        if (!active || !acknowledged || broken) return;
        promptDepth += 1; active.state = "waiting";
        save(active, "等待 Pi 窗口中的人工确认；工作台不会自动回答。");
    }));
    pi.on("ui_prompt_end", () => guarded(() => {
        if (!active || !acknowledged || broken) return;
        promptDepth = Math.max(0, promptDepth - 1);
        if (!promptDepth) { active.state = "working"; save(active, "Pi 人工确认已返回；继续观察执行。"); }
    }));
    pi.on("input", () => {
        guarded(() => {
            if (active && acknowledged && !broken) {
                ownershipLost = true; active.delivery = "";
                save(active, "Pi 收到额外输入；工作台停止归属推断，不会再停止可能无关的工作或接受本轮交付。");
            }
        });
        return { action: "continue" };
    });
    pi.on("agent_settled", (_event, ctx) => guarded(() => {
        context = ctx;
        if (!active || !acknowledged || broken) return;
        if (stopRequested || ownershipLost || assistantError) {
            active.state = stopRequested ? "stopped" : ownershipLost ? "interrupted" : "failed";
            active.delivery = "";
            active.summary = stopRequested ? "人工停止已确认；Pi 已完全空闲。" : ownershipLost ? "额外输入使执行归属中断；请核实成果后另行安排。" : `Pi 响应失败：${assistantError}`;
        } else if (["full", "partial", "blocked"].includes(active.delivery)) {
            active.state = active.delivery === "blocked" ? "failed" : "finished";
        } else {
            active.state = "failed"; active.summary = "Pi 已空闲，但未提交最终 taskroom_report；不能认为任务完成。";
        }
        save(active, "Pi agent_settled · 执行完全空闲，人工验收仍独立");
        active = undefined; acknowledged = false; heartbeat();
    }));
    const protectNavigation = () => active ? { cancel: true } : undefined;
    pi.on("session_before_tree", protectNavigation);
    pi.on("session_before_switch", protectNavigation);
    pi.on("session_before_fork", protectNavigation);
    pi.on("session_shutdown", event => disconnect(event.reason));
}
