/** Authoritative, at-most-once steering receipts. Separate from legacy command schema. */
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const KEYS = ["id", "task_id", "agent_id", "session_id", "cwd", "title", "description", "requirements"] as const;
type Identity = { id: string; task_id: number; agent_id: string; session_id: string; cwd: string; title: string; description: string; requirements: string };
type Active = Identity & { seq: number; delivery: string; state: string };
type Request = { version: 1; id: string; expires: number; text: string; run: Identity };
type Receipt = { version: 1; request: Request; status: "queued" | "received" | "rejected" | "covered"; seq: number; run_seq: number; detail: string };
type Message = { role: string; customType?: string; details?: unknown; content?: unknown };
type IO = { read: (file: string, limit?: number) => Record<string, unknown>; write: (file: string, data: unknown, exclusive?: boolean) => void; directory: (file: string, create?: boolean) => void };
const uuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const object = (value: unknown): Record<string, unknown> => {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid supplement object.");
    return value as Record<string, unknown>;
};
const keys = (row: Record<string, unknown>, names: readonly string[]) => Object.keys(row).sort().join(",") === [...names].sort().join(",");
const binding = (run: Identity) => JSON.stringify(KEYS.map(key => run[key]));

function parseRequest(value: unknown): Request {
    const row = object(value), run = object(row.run);
    if (!keys(row, ["version", "id", "expires", "text", "run"]) || row.version !== 1 || !uuid(row.id) ||
        !Number.isSafeInteger(row.expires) || (row.expires as number) <= 0 || typeof row.text !== "string" ||
        !row.text.trim() || Array.from(row.text).length > 4000 || !keys(run, KEYS)) throw new Error("Invalid supplement request.");
    for (const key of KEYS) {
        if (key === "task_id" ? !Number.isSafeInteger(run[key]) || (run[key] as number) <= 0 : typeof run[key] !== "string") throw new Error("Invalid supplement run identity.");
    }
    if (![run.id, run.agent_id, run.session_id].every(uuid) || !path.isAbsolute(run.cwd as string)) throw new Error("Invalid supplement binding.");
    // Canonical property order makes identical Python/JS objects independent of JSON key order.
    return { version: 1, id: row.id, expires: row.expires as number, text: row.text,
        run: Object.fromEntries(KEYS.map(key => [key, run[key]])) as Identity };
}

export function supplementContent(request: Request): string {
    return `Waggle run supplement (explicit human confirmation)\nTask run: ${request.run.id}\nSUPPLEMENT_ID=${request.id}\n\n${request.text}\n\nThis supplements only the current task in its original project. Keep normal permission gates; this is not a permission approval or a new task. Acknowledge all received supplements in your final report. Use supplement_id=${request.id} in waggle_report (including progress); older/precomputed reports are not sufficient. If it cannot be fulfilled, report partial/blocked with explicit remaining work. Delivery still needs independent human acceptance.`;
}

export function supplementChannel(pi: ExtensionAPI, io: IO, owned: () => Active | undefined, ready: () => boolean,
                                  save: (run: Active, kind: string, text: string) => void,
                                  capable: (text: string) => boolean | Promise<boolean> = () => true) {
    let endpoint = "", currentRun = "", requiredId = "";
    const notes = new Map<string, Receipt>();
    const file = (id: string) => path.join(endpoint, "supplements", "receipts", `${id}.json`);
    const queued = () => [...notes.values()].some(note => note.status === "queued");
    function update(note: Receipt, status: Receipt["status"], runSeq: number, detail: string) {
        note.status = status; note.seq += 1; note.run_seq = runSeq; note.detail = detail;
        io.write(file(note.request.id), note);
    }
    function invalidate(run: Active | undefined, detail: string) {
        for (const note of notes.values()) {
            if (note.status === "queued") update(note, "rejected", run?.seq ?? note.run_seq, detail);
        }
    }
    function requestFrom(message: Message): Receipt | undefined {
        if (message.role !== "custom" || message.customType !== "waggle-supplement") return;
        const details = message.details;
        if (!details || typeof details !== "object" || Array.isArray(details)) return;
        const row = details as Record<string, unknown>;
        const note = typeof row.supplementId === "string" ? notes.get(row.supplementId) : undefined;
        if (!note || !keys(row, ["runId", "supplementId"]) || row.runId !== note.request.run.id || message.content !== supplementContent(note.request)) return;
        return note;
    }
    pi.on("context", event => {
        let run: Active | undefined;
        try { run = owned(); } catch { /* Invalid cwd/session: no queued instructions enter the model. */ }
        const seen = new Set<string>();
        return { messages: event.messages.filter(message => {
            if (message.role !== "custom" || message.customType !== "waggle-supplement") return true;
            const note = requestFrom(message);
            if (!note || !run || binding(note.request.run) !== binding(run) || !["received", "covered"].includes(note.status) || seen.has(note.request.id)) return false;
            seen.add(note.request.id);
            return true;
        }) };
    });
    async function consume(filename: string) {
        const expectedEndpoint = endpoint;
        const request = parseRequest(io.read(filename, 256 * 1024));
        if (path.basename(filename) !== `${request.id}.json`) throw new Error("Supplement filename/id mismatch.");
        if (fs.existsSync(file(request.id))) {
            const old = io.read(file(request.id), 256 * 1024);
            if (!keys(old, ["version", "request", "status", "seq", "run_seq", "detail"]) || old.version !== 1 ||
                JSON.stringify(parseRequest(old.request)) !== JSON.stringify(request) ||
                !["queued", "received", "rejected", "covered"].includes(String(old.status)) || !Number.isSafeInteger(old.seq) || (old.seq as number) < 1) {
                throw new Error("Existing supplement reservation differs; never replay.");
            }
            return; // A persisted reservation, even uncertain/crashed, is never injected again.
        }
        const run = owned();
        const receipt: Receipt = { version: 1, request, status: "rejected", seq: 1, run_seq: 0, detail: "本轮不在可接收补充的明确执行状态；未投递，不重试。" };
        if (!run || binding(request.run) !== binding(run) || !ready() || Date.now() > request.expires || request.expires > Date.now() + 60000) {
            io.write(file(request.id), receipt, true); return;
        }
        const capableNow = await capable(request.text);
        if (endpoint !== expectedEndpoint) return;
        // Verification may await Fabric: repeat ownership/readiness/expiry BEFORE any reservation or injection.
        if (owned() !== run || binding(request.run) !== binding(run) || !ready() || Date.now() > request.expires) {
            io.write(file(request.id), receipt, true); return;
        }
        if (!capableNow) {
            receipt.detail = "当前报告工具不能接收这份结构化补充；未投递，请先核对桥接连接，不重试。";
            io.write(file(request.id), receipt, true); return;
        }
        if (currentRun !== run.id) { notes.clear(); currentRun = run.id; requiredId = ""; }
        if (queued() || notes.size >= 16) {
            receipt.detail = "已有未确认消息或本轮已达补充上限；未投递。";
            io.write(file(request.id), receipt, true); return;
        }
        run.delivery = "";
        save(run, "supplement_queued", `补充 ${request.id} · 待 Pi 接收；此前交付需重新报告`);
        receipt.status = "queued"; receipt.run_seq = run.seq; receipt.detail = "已排入消息队列，尚未确认接收或执行。";
        io.write(file(request.id), receipt, true); // Durable tombstone BEFORE any native message call.
        notes.set(request.id, receipt);
        pi.sendMessage({ customType: "waggle-supplement", content: supplementContent(request), display: true,
            details: { runId: run.id, supplementId: request.id } }, { deliverAs: "steer" });
        // Omit triggerTurn: steering while streaming, append-only if a racing idle transition occurs. Never start a turn.
    }
    return {
        activate(directory: string, identity: { agent_id: string; session_id: string; cwd: string }) {
            endpoint = directory; notes.clear(); currentRun = ""; requiredId = "";
            for (const subdirectory of ["supplements", "supplements/requests", "supplements/receipts"]) io.directory(path.join(endpoint, subdirectory), true);
            io.write(path.join(endpoint, "capabilities.json"), { version: 1, ...identity, supplement: 1 });
        },
        async tick() {
            const expectedEndpoint = endpoint;
            const directory = path.join(endpoint, "supplements/requests");
            io.directory(path.join(endpoint, "supplements")); io.directory(directory); io.directory(path.join(endpoint, "supplements/receipts"));
            for (const name of fs.readdirSync(directory).filter(name => name.endsWith(".json")).sort().slice(0, 8)) {
                const filename = path.join(directory, name);
                await consume(filename);
                if (endpoint !== expectedEndpoint) return;
                fs.unlinkSync(filename);
            }
        },
        acknowledge(message: Message) {
            const note = requestFrom(message), run = owned();
            if (!note || note.status !== "queued") return;
            if (!run || binding(run) !== binding(note.request.run) || run.state !== "working" || !ready()) {
                update(note, "rejected", note.run_seq, "接收时归属或执行状态已改变，未作为本轮说明。这里不撤回已接收的其他说明。"); return;
            }
            requiredId = note.request.id;
            save(run, "supplement_received", `Pi 已接收补充 ${requiredId} · 接收不等于执行`);
            update(note, "received", run.seq, "Pi 已接收；最终报告须明确覆盖。 ");
        },
        assertReport(run: Active, token: string | undefined) {
            if (currentRun !== run.id) return;
            if (queued() || (requiredId && token !== requiredId)) throw new Error(`A supplement is unacknowledged or the report is stale. Include latest supplement_id=${requiredId || "pending"} after receiving it.`);
        },
        cover(run: Active) {
            for (const note of notes.values()) if (note.request.run.id === run.id && note.status === "received") {
                update(note, "covered", run.seq, "执行者在最终报告中声明已覆盖补充；仍需人工核实。");
            }
        },
        unresolved(run: Active): string[] {
            return [...notes.values()].filter(note => note.request.run.id === run.id && note.status !== "covered").map(note => note.request.text);
        },
        queued, invalidate,
    };
}
