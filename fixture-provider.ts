/** Deterministic OFFLINE provider for tests of the real Pi runtime. Not an agent. */
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, type AssistantMessage } from "@earendil-works/pi-ai";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
    if (!process.env.TASKROOM_TEST_PROJECT) throw new Error("Fixture may only run with an explicit disposable test project.");
    pi.registerTool({
        name: "taskroom_fixture_effect", label: "Offline fixture effect",
        description: "TEST ONLY: increment a counter within the explicit disposable test project.",
        parameters: Type.Object({ slow: Type.Boolean(), fail: Type.Boolean(), confirm: Type.Optional(Type.Boolean()) }),
        async execute(_id, params, signal, _update, ctx) {
            if (fs.realpathSync(ctx.cwd) !== process.env.TASKROOM_TEST_PROJECT) throw new Error("Fixture refuses non-test cwd.");
            if (params.confirm && !await ctx.ui.confirm("Offline fixture permission", "Approve the isolated test file effect?")) {
                throw new Error("Human declined the fixture effect.");
            }
            if (params.slow) await new Promise<void>((resolve, reject) => {
                const abort = () => { clearTimeout(timer); reject(new Error("Fixture aborted")); };
                const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, 15000);
                signal?.addEventListener("abort", abort, { once: true });
                if (signal?.aborted) abort();
            });
            if (params.fail) throw new Error("Deliberate fixture tool failure");
            const file = path.join(ctx.cwd, "fixture-result.json");
            const counter = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")).counter + 1 : 1;
            fs.writeFileSync(file, JSON.stringify({ counter, text: "中文临时成果" }), { mode: 0o600 });
            return { content: [{ type: "text", text: `Observed fixture counter=${counter}` }], details: { counter } };
        },
    });
    pi.registerProvider("taskroom-test", {
        baseUrl: "http://unused.invalid", apiKey: "offline-fixture", api: "taskroom-test",
        models: [{ id: "fixture", name: "Offline fixture", reasoning: false, input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 4096 }],
        streamSimple(model, context) {
            const stream = createAssistantMessageEventStream();
            const messages = context.messages;
            let prompt = "", start = -1;
            for (let i = messages.length - 1; i >= 0; i--) {
                const message = messages[i];
                if (message.role !== "user") continue;
                const text = typeof message.content === "string" ? message.content : message.content.filter(part => part.type === "text").map(part => part.text).join("\n");
                if (text.includes("RUN_ID=")) { prompt = text; start = i; break; }
            }
            const runId = /RUN_ID=([0-9a-f-]{36})/.exec(prompt)?.[1];
            const compactions = Number(/FIXTURE_COMPACTED=(\d+)/.exec(prompt)?.[1] || 0);
            const effectDone = compactions > 0 || messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "taskroom_fixture_effect");
            const compactTask = prompt.includes("Task: COMPACT");
            const compactAttempted = messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "context_compact");
            const extraInput = messages.slice(start + 1).some(message => {
                if (message.role !== "user") return false;
                const text = typeof message.content === "string" ? message.content : message.content.filter(part => part.type === "text").map(part => part.text).join("\n");
                return !compactTask || !text.startsWith("context_compact ");
            });
            const confirmation = prompt.includes("Task: CONFIRM");
            const noReport = prompt.includes("Task: NO_REPORT");
            const partial = prompt.includes("Task: PARTIAL");
            const blocked = prompt.includes("Task: BLOCKED");
            const slow = prompt.includes("Task: SLOW");
            const lateFailure = prompt.includes("Task: LATE_FAILURE");
            const taskTitle = /^Task: (.+)$/m.exec(prompt)?.[1] || "COMPACT";
            const slowContinuation = taskTitle === "COMPACT_SLOW" && compactions > 0 && !messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "taskroom_fixture_effect");
            const output: AssistantMessage = {
                role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
                usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
                stopReason: "stop", timestamp: Date.now(),
            };
            if (!runId || extraInput || (effectDone && noReport)) output.content = [{ type: "text", text: "Offline fixture ended without final delivery." }];
            else if (!effectDone) output.content = [{ type: "toolCall", id: `effect-${runId}`, name: "taskroom_fixture_effect", arguments: { slow, fail: false, confirm: confirmation } }];
            else if (slowContinuation) output.content = [{ type: "toolCall", id: `continued-effect-${runId}`, name: "taskroom_fixture_effect", arguments: { slow: true, fail: false } }];
            else if (compactTask && !compactAttempted && compactions < (prompt.includes("Task: COMPACT_TWICE") ? 2 : 1)) {
                output.content = [{ type: "toolCall", id: `compact-${runId}-${compactions}`, name: "context_compact", arguments: {
                    target: "root", summary: `RUN_ID=${runId}\nTask: ${taskTitle}\nFIXTURE_COMPACTED=${compactions + 1}\nThe fixture effect already happened once; continue with the report, do not replay the effect.`,
                } }];
            }
            else {
                const progress = taskTitle === "HISTORY" && !messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "taskroom_report");
                output.content = [{ type: "toolCall", id: `report-${runId}-${progress ? "progress" : "final"}`, name: "taskroom_report", arguments: {
                    run_id: runId, kind: progress ? "progress" : blocked ? "blocked" : partial ? "partial" : "full",
                    summary: progress ? "第一份报告：已写入隔离文件，仍需最终核对。" : "Offline fixture report · 中文成果已写入",
                    checks: ["Fixture tool observed counter; integration test independently reads the file."],
                    remaining: progress ? "仍需最终核对" : partial ? "第二阶段尚未完成" : blocked ? "等待测试输入" : "",
                    ...(taskTitle === "FULL_LEGACY" ? {} : { brief: { actions: ["写入隔离测试文件"], outcome: "已保存中文样例成果", verification: "自报计数器结果；集成测试另行读文件核实" } }),
                } }];
                if (lateFailure && !messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "taskroom_report")) {
                    output.content.push({ type: "toolCall", id: `fail-${runId}`, name: "taskroom_fixture_effect", arguments: { slow: false, fail: true } });
                } else if (lateFailure) output.content = [{ type: "text", text: "Fixture ends after deliberate late failure, no replacement report." }];
            }
            const toolUse = output.content.some(part => part.type === "toolCall");
            output.stopReason = toolUse ? "toolUse" : "stop";
            if (taskTitle === "PROVIDER_ERROR" || (taskTitle === "COMPACT_PROVIDER_ERROR" && compactions > 0)) {
                output.content = []; output.stopReason = "error"; output.errorMessage = "Deliberate offline provider failure";
                stream.push({ type: "error", reason: "error", error: output });
                stream.end();
                return stream;
            }
            stream.push({ type: "start", partial: output });
            for (let i = 0; i < output.content.length; i++) {
                const part = output.content[i];
                if (part.type === "toolCall") stream.push({ type: "toolcall_end", contentIndex: i, toolCall: part, partial: output });
                else if (part.type === "text") stream.push({ type: "text_end", contentIndex: i, content: part.text, partial: output });
            }
            stream.push({ type: "done", reason: output.stopReason, message: output });
            stream.end();
            return stream;
        },
    });
}
