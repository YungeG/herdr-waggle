/** Explicit attachment is a control receipt, never a prompt, execution or budget grant. */
import * as fs from "node:fs";
import * as path from "node:path";
import { createHash } from "node:crypto";

export interface AdoptionSource {
    run_id: string; agent_id: string; session_id: string; cwd: string; seq: number; digest: string; pid: number;
}
export interface AdoptionRequest {
    version: 1; id: string; agent_id: string; session_id: string; cwd: string; pid: number;
    task_id: number; title: string; project: string; description: string; requirements: string;
    checkpoint: string; source: AdoptionSource | null; previous_checked: boolean; requested_at: number; expires: number;
}
interface Receipt {
    version: 1; id: string; request_hash: string; agent_id: string; session_id: string; cwd: string;
    task_id: number; seq: number; state: "waiting" | "attached" | "rejected" | "consumed" | "detached";
    at: number; accepted_at: number; branch_anchor: string; run_id: string; detail: string;
}
interface IO {
    read(file: string, limit?: number): Record<string, unknown>;
    write(file: string, value: unknown, exclusive?: boolean): void;
    directory(directory: string, create?: boolean): void;
}
interface State {
    endpoint: string; agent_id: string; session_id: string; cwd: string; pid: number;
    idle: boolean; owned: boolean; leaf: string;
}
const uuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const integer = (value: unknown, minimum = 0): value is number => Number.isSafeInteger(value) && (value as number) >= minimum;
const text = (value: unknown, limit: number, required = false): value is string => typeof value === "string" && [...value].length <= limit && (!required || Boolean(value.trim())) && Buffer.from(value).toString("utf8") === value;
function object(value: unknown, keys: string[]): Record<string, unknown> {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().join(",") !== [...keys].sort().join(",")) throw new Error("Invalid adoption fields.");
    return value as Record<string, unknown>;
}
export function canonical(value: unknown): string {
    if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
    if (value !== null && typeof value === "object") {
        const row = value as Record<string, unknown>;
        return "{" + Object.keys(row).sort().map(key => JSON.stringify(key) + ":" + canonical(row[key])).join(",") + "}";
    }
    const encoded = JSON.stringify(value);
    if (encoded === undefined) throw new Error("Non-JSON adoption value.");
    return encoded;
}
export const digest = (value: unknown): string => createHash("sha256").update(canonical(value), "utf8").digest("hex");
export function parseAdoption(value: unknown): AdoptionRequest {
    const row = object(value, ["version", "id", "agent_id", "session_id", "cwd", "pid", "task_id", "title", "project", "description", "requirements", "checkpoint", "source", "previous_checked", "requested_at", "expires"]);
    if (row.version !== 1 || !uuid(row.id) || !uuid(row.agent_id) || !uuid(row.session_id) || !integer(row.pid, 1) || !integer(row.task_id, 1) ||
        !text(row.cwd, 4096, true) || !path.isAbsolute(row.cwd) || !text(row.title, 300, true) || !text(row.project, 300, true) ||
        !text(row.description, 20000) || !text(row.requirements, 6000) || !text(row.checkpoint, 1800, true) || typeof row.previous_checked !== "boolean" ||
        !integer(row.requested_at, 1) || !integer(row.expires, 1) || row.expires <= row.requested_at || row.expires - row.requested_at > 600000) throw new Error("Invalid adoption identity, scope or window.");
    if (row.source !== null) {
        const source = object(row.source, ["run_id", "agent_id", "session_id", "cwd", "seq", "digest", "pid"]);
        if (!uuid(source.run_id) || !uuid(source.agent_id) || !uuid(source.session_id) || !text(source.cwd, 4096, true) ||
            !integer(source.seq) || !hash(source.digest) || !integer(source.pid, 1) || source.session_id !== row.session_id || source.cwd !== row.cwd || !row.previous_checked) throw new Error("Adoption cannot transfer another session or unconfirmed execution.");
    } else if (row.previous_checked) throw new Error("A new task cannot claim old execution evidence.");
    // Exact schema, scalar bounds and source identity were checked above.
    return row as unknown as AdoptionRequest;
}

export function adoptionChannel(io: IO, state: () => State, verifySource: (request: AdoptionRequest) => void) {
    let endpoint = "";
    let pending: AdoptionRequest | undefined;
    let attached: AdoptionRequest | undefined;
    const receipts = new Map<string, Receipt>();
    const root = () => path.join(endpoint, "adoptions");
    const receiptFile = (id: string) => path.join(root(), "receipts", `${id}.json`);
    function checkTarget(request: AdoptionRequest) {
        const current = state();
        if (current.endpoint !== endpoint || (request.agent_id !== current.agent_id || request.session_id !== current.session_id || request.cwd !== current.cwd || request.pid !== current.pid)) throw new Error("Adoption target changed.");
    }
    function writeReceipt(request: AdoptionRequest, next: Receipt["state"], detail: string, runId = "") {
        const previous = receipts.get(request.id);
        const now = Date.now();
        const nextReceipt: Receipt = {
            version: 1, id: request.id, request_hash: digest(request), agent_id: request.agent_id,
            session_id: request.session_id, cwd: request.cwd, task_id: request.task_id,
            seq: (previous?.seq || 0) + 1, state: next, at: now,
            accepted_at: previous?.accepted_at || (next === "attached" ? now : 0),
            branch_anchor: previous?.branch_anchor || state().leaf, run_id: runId, detail,
        };
        io.write(receiptFile(request.id), nextReceipt);
        receipts.set(request.id, nextReceipt);
    }
    function archive(request: AdoptionRequest) {
        const from = path.join(root(), "requests", `${request.id}.json`);
        const to = path.join(root(), "history", `${request.id}.json`);
        if (!fs.existsSync(from)) return;
        if (fs.existsSync(to)) {
            if (digest(io.read(to)) !== digest(request)) throw new Error("Adoption history conflict.");
            fs.unlinkSync(from);
        } else fs.renameSync(from, to);
    }
    function reject(request: AdoptionRequest, reason: string) {
        writeReceipt(request, "rejected", reason);
        archive(request);
        if (pending?.id === request.id) pending = undefined;
    }
    function advance() {
        if (!pending) return;
        const request = pending;
        checkTarget(request);
        if (Date.now() > request.expires) return reject(request, "纳管窗口已过期；没有启动或重复发送，请重新核对。");
        if (state().owned || attached) return reject(request, "会话已有明确任务归属；不会接管其他执行。");
        verifySource(request);
        if (!state().idle) {
            if (receipts.get(request.id)?.state !== "waiting") writeReceipt(request, "waiting", "等待当前原生回合完全结束；不插话、不停止、不重发。");
            return;
        }
        writeReceipt(request, "attached", "会话已纳管；没有创建 Run 或启动模型。后续明确输入才开始新轮。");
        attached = request;
        pending = undefined;
        archive(request);
    }
    function tick() {
        if (!endpoint || state().endpoint !== endpoint) return;
        io.directory(path.join(root(), "requests"));
        for (const name of fs.readdirSync(path.join(root(), "requests")).filter(name => name.endsWith(".json")).sort().slice(0, 8)) {
            const request = parseAdoption(io.read(path.join(root(), "requests", name)));
            if (name !== `${request.id}.json`) throw new Error("Adoption file identity mismatch.");
            checkTarget(request);
            if (fs.existsSync(receiptFile(request.id))) {
                const previous = io.read(receiptFile(request.id), 16384);
                if (previous.id !== request.id || previous.request_hash !== digest(request)) throw new Error("Adoption replay conflict.");
                if (previous.state !== "waiting") { archive(request); continue; }
            }
            if ((pending && pending.id !== request.id) || attached || state().owned) {
                reject(request, "已有纳管意图或已关联任务；不会覆盖、分叉或重复接管。");
                continue;
            }
            if (request.requested_at > Date.now() + 2000) return reject(request, "纳管请求的时间无效；未建立关联。");
            pending = request;
            try { advance(); }
            catch { reject(request, "原轮次、原始内容或会话归属未核实；未建立关联，请核对声明来源。"); }
        }
        if (pending) {
            try { advance(); }
            catch { reject(pending, "原轮次、原始内容或会话归属已变化；未建立关联，请重新核对。"); }
        }
    }
    function invalidate(reason: string) {
        if (pending) reject(pending, reason);
        if (attached) {
            writeReceipt(attached, "detached", reason);
            attached = undefined;
        }
    }
    return {
        activate(value: string) {
            endpoint = value; pending = attached = undefined; receipts.clear();
            for (const directory of [root(), path.join(root(), "requests"), path.join(root(), "history"), path.join(root(), "receipts")]) io.directory(directory, true);
        },
        capabilities(observedAt = Date.now()) {
            const current = state();
            return { version: 1, agent_id: current.agent_id, session_id: current.session_id, cwd: current.cwd,
                pid: current.pid, observed_at: observedAt, association_id: attached?.id || "" };
        },
        bound: () => attached,
        pending: () => Boolean(pending),
        tick,
        invalidate,
        consume(runId: string) {
            if (pending) reject(pending, "已有明确新输入或派发；纳管请求不追认这次工作。");
            if (attached) {
                writeReceipt(attached, "consumed", "后续明确输入/派发已开始新的 Run；旧轮原封保留，预算不重置。", runId);
                attached = undefined;
            }
        },
        disconnect() {
            if (endpoint) invalidate("连接或会话已结束；关联不在重连后自动恢复。");
            endpoint = ""; pending = attached = undefined; receipts.clear();
        },
    };
}
