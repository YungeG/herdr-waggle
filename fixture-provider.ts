/** Deterministic OFFLINE provider for tests of the real Pi runtime. Not an agent. */
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, type AssistantMessage } from "@earendil-works/pi-ai";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
    if (!process.env.WAGGLE_TEST_PROJECT) throw new Error("Fixture may only run with an explicit disposable test project.");
    pi.registerTool({
        name: "waggle_fixture_effect", label: "Offline fixture effect",
        description: "TEST ONLY: increment a counter within the explicit disposable test project.",
        parameters: Type.Object({ slow: Type.Boolean(), fail: Type.Boolean(), confirm: Type.Optional(Type.Boolean()), latch: Type.Optional(Type.Boolean()) }),
        async execute(_id, params, signal, _update, ctx) {
            if (fs.realpathSync(ctx.cwd) !== process.env.WAGGLE_TEST_PROJECT) throw new Error("Fixture refuses non-test cwd.");
            if (params.confirm && !await ctx.ui.confirm("Offline fixture permission", "Approve the isolated test file effect?")) {
                throw new Error("Human declined the fixture effect.");
            }
            if (params.latch) await new Promise<void>((resolve, reject) => {
                const deadline = Date.now() + 20000;
                const finish = (error?: Error) => { clearInterval(timer); signal?.removeEventListener("abort", abort); if (error) reject(error); else resolve(); };
                const abort = () => finish(new Error("Fixture latch aborted"));
                const timer = setInterval(() => {
                    if (fs.existsSync(path.join(ctx.cwd, "fixture-supplement-release"))) { fs.unlinkSync(path.join(ctx.cwd, "fixture-supplement-release")); finish(); }
                    else if (Date.now() > deadline) finish(new Error("Fixture latch timeout"));
                }, 40);
                signal?.addEventListener("abort", abort, { once: true });
                if (signal?.aborted) abort();
            });
            if (params.slow) await new Promise<void>((resolve, reject) => {
                const abort = () => { clearTimeout(timer); reject(new Error("Fixture aborted")); };
                const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, 15000);
                signal?.addEventListener("abort", abort, { once: true });
                if (signal?.aborted) abort();
            });
            if (params.fail) throw new Error("Deliberate fixture tool failure");
            const file = path.join(ctx.cwd, "fixture-result.json");
            let counter: number;
            try {
                counter = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")).counter + 1 : 1;
                if (!Number.isSafeInteger(counter) || counter < 1) throw new Error("Invalid counter value.");
            } catch (cause) {
                throw new Error("Invalid offline fixture counter; refusing to reset or continue.", { cause });
            }
            fs.writeFileSync(file, JSON.stringify({ counter, text: "中文临时成果" }), { mode: 0o600 });
            return { content: [{ type: "text", text: `Observed fixture counter=${counter}` }], details: { counter } };
        },
    });
    pi.registerProvider("waggle-test", {
        baseUrl: "http://unused.invalid", apiKey: "offline-fixture", api: "waggle-test",
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
                if (!text.startsWith("Waggle run supplement (") && text.includes("RUN_ID=")) { prompt = text; start = i; break; }
            }
            const runId = /RUN_ID=([0-9a-f-]{36})/.exec(prompt)?.[1];
            const compactions = Number(/FIXTURE_COMPACTED=(\d+)/.exec(prompt)?.[1] || 0);
            const fabricMode = process.env.WAGGLE_TEST_FABRIC === "full";
            const used = (name: string) => messages.slice(start + 1).some(message => {
                if (message.role !== "toolResult") return false;
                if (message.toolName === name) return true;
                if (message.toolName !== "fabric_exec") return false;
                try {
                    const text = message.content.filter(part => part.type === "text").map(part => part.text).join("\n");
                    return JSON.parse(text).fixture_tools?.includes(name) === true;
                } catch { return false; }
            });
            const effectDone = compactions > 0 || used("waggle_fixture_effect");
            const compactTask = prompt.includes("Task: COMPACT") || prompt.includes("Task: SUPPLEMENT_COMPACT");
            const compactAttempted = messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "context_compact");
            const extraInput = messages.slice(start + 1).some(message => {
                if (message.role !== "user") return false;
                const text = typeof message.content === "string" ? message.content : message.content.filter(part => part.type === "text").map(part => part.text).join("\n");
                return !text.startsWith("Waggle run supplement (") && (!compactTask || !text.startsWith("context_compact "));
            });
            const confirmation = prompt.includes("Task: CONFIRM");
            const noReport = prompt.includes("Task: NO_REPORT");
            const blocked = prompt.includes("Task: BLOCKED");
            const activityTask = prompt.includes("Task: ACTIVITY_");
            const childDone = messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "subagent");
            const slow = prompt.includes("Task: SLOW") || activityTask;
            const lateFailure = prompt.includes("Task: LATE_FAILURE");
            const taskTitle = /^Task: (.+)$/m.exec(prompt)?.[1] || "COMPACT";
            const inventoryRequest = /Rework \/ remaining requirements:\n([\s\S]*?)\n\nWork only on this task/.exec(prompt)?.[1] || "";
            // TEST ONLY: ambiguous literal envelopes in old text never select a completed current review.
            const unambiguousRequest = prompt.split("Rework / remaining requirements:\n").length === 2 &&
                prompt.split("\n\nWork only on this task").length === 2;
            const completePartialReview = taskTitle === "PARTIAL" && unambiguousRequest && inventoryRequest.startsWith(
                "本轮核查合同：\n交付物：完成剩余工作 · FIXTURE_COMPLETE_PARTIAL\n允许的范围：");
            const partial = (prompt.includes("Task: PARTIAL") || prompt.includes("Task: HANDOFF")) && !completePartialReview;
            const directTask = taskTitle.startsWith("DIRECT_");
            const needsDecision = taskTitle === "DECISIONS" && !prompt.includes("选择：a · 路线 A");
            const directContinuation = prompt.startsWith("Waggle task continuation (");
            const supplementTask = taskTitle.startsWith("SUPPLEMENT");
            const notes = messages.slice(start + 1).flatMap(message => {
                if (message.role !== "user") return [];
                const text = typeof message.content === "string" ? message.content : message.content.filter(part => part.type === "text").map(part => part.text).join("\n");
                return text.startsWith("Waggle run supplement (") ? [text] : [];
            });
            const supplementId = /SUPPLEMENT_ID=([0-9a-f-]{36})/.exec(notes.at(-1) || (supplementTask ? prompt : ""))?.[1];
            const staleSupplement = supplementTask && taskTitle.endsWith("STALE");
            if (supplementTask && compactions > 0) fs.writeFileSync(path.join(process.env.WAGGLE_TEST_PROJECT!, "fixture-supplement-summary.txt"), prompt, { mode: 0o600 });
            const reportAttempted = used("waggle_report");
            if (supplementTask) fs.writeFileSync(path.join(process.env.WAGGLE_TEST_PROJECT!, "fixture-supplement-visible.json"), JSON.stringify(notes), { mode: 0o600 });
            const slowContinuation = taskTitle === "COMPACT_SLOW" && compactions > 0 && !messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "waggle_fixture_effect");
            const output: AssistantMessage = {
                role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
                usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
                stopReason: "stop", timestamp: Date.now(),
            };
            const endedFabric = fabricMode && messages.slice(start + 1).some(message => message.role === "toolResult" && message.toolName === "fabric_exec" &&
                (message.isError || (reportAttempted && taskTitle !== "HISTORY")));
            if (endedFabric || !runId || (extraInput && !directTask) || (effectDone && noReport) || (staleSupplement && reportAttempted)
                    || (taskTitle === "DIRECT_QUEUE" && reportAttempted && !extraInput)
                    || ((taskTitle === "EVIDENCE_FULL" || taskTitle === "INVENTORY_R2") && reportAttempted)) output.content = [{ type: "text", text: "Offline fixture ended without final delivery." }];
            else if (taskTitle === "INVENTORY_R2") output.content = [{ type: "toolCall", id: `inventory-${runId}`, name: "waggle_report", arguments: {
                run_id: runId, kind: "full", summary: "仅核对显式成果引用，未调用写入工具",
                checks: ["来源与人工验收引用来自当前请求；原件及写者释放仍未核实",
                    `Fixture received request sha256=${createHash("sha256").update(inventoryRequest, "utf8").digest("hex")}`], remaining: "",
            } }];
            else if (taskTitle === "EVIDENCE_FULL") output.content = [{ type: "toolCall", id: `wrong-full-${runId}`, name: "waggle_report", arguments: {
                run_id: runId, kind: "full", summary: "Incorrectly claims original task is done", checks: ["Only an offline evidence-only request was received"], remaining: "",
            } }];
            else if (needsDecision) output.content = [{ type: "toolCall", id: `decision-${runId}`, name: "waggle_report", arguments: {
                run_id: runId, kind: "partial", summary: "等待人工选择；尚未写入成果", checks: ["离线夹具尚未调用 effect 工具"], remaining: "请决定隔离验证路线",
                decisions: [{ key: "route", kind: "choice", question: "选择哪条隔离验证路线？", why: "需要明确范围", owner: "用户",
                    evidence: ["路线 A 单次写入，路线 B 仅分析"], missing: [], scope: "仅 WAGGLE_TEST_PROJECT 临时目录", risk: "夹具可能失败", cost: "一次离线调用",
                    options: [{ key: "a", label: "路线 A", benefit: "验证全链路", risk: "临时写入", cost: "一次", scope: "临时成果文件" },
                        { key: "b", label: "路线 B", benefit: "不写入", risk: "未验证写入", cost: "一次分析", scope: "仅分析" }],
                    recommendation: "a", reason: "可独立核实计数", exclusions: ["不触及真实任务和外部文件"], budget: null,
                    routing: { audience: "user_decision", evidence_needs: [], advice: { status: "supported", value: "路线 A", unit: "项",
                        conditions: "仅离线夹具目录", evidence: ["路线 A 单次写入，路线 B 仅分析"], uncertainty: "夹具可能失败", owner_signed: false } } }],
            } }];
            else if (activityTask && !childDone) output.content = [{ type: "toolCall", id: `child-${runId}`, name: "subagent",
                arguments: taskTitle === "ACTIVITY_MANAGEMENT" ? { action: "status" } : { agent: taskTitle.slice("ACTIVITY_".length) } }];
            else if (!effectDone) output.content = [{ type: "toolCall", id: `effect-${runId}`, name: "waggle_fixture_effect", arguments: { slow, fail: false, confirm: confirmation, latch: supplementTask || taskTitle === "DIRECT_LATCH" || taskTitle === "DIRECT_QUEUE" } }];
            else if (taskTitle === "SUPPLEMENT_MULTI" && messages.slice(start + 1).filter(message => message.role === "toolResult" && message.toolName === "waggle_fixture_effect").length < 2) {
                output.content = [{ type: "toolCall", id: `second-${runId}`, name: "waggle_fixture_effect", arguments: { slow: false, fail: false, latch: true } }];
            }
            else if (slowContinuation) output.content = [{ type: "toolCall", id: `continued-effect-${runId}`, name: "waggle_fixture_effect", arguments: { slow: true, fail: false } }];
            else if (compactTask && !compactAttempted && compactions < (prompt.includes("Task: COMPACT_TWICE") ? 2 : 1)) {
                output.content = [{ type: "toolCall", id: `compact-${runId}-${compactions}`, name: "context_compact", arguments: {
                    target: "root", summary: `RUN_ID=${runId}\nTask: ${taskTitle}\nFIXTURE_COMPACTED=${compactions + 1}\nThe fixture effect already happened once; continue with the report, do not replay the effect.${supplementTask ? "\n" + notes.join("\n\n") : ""}`,
                } }];
            }
            else {
                const progress = taskTitle === "HISTORY" && !used("waggle_report");
                output.content = [{ type: "toolCall", id: `report-${runId}-${progress ? "progress" : "final"}${directTask ? "-" + messages.length : ""}`, name: "waggle_report", arguments: {
                    ...(supplementTask && !staleSupplement && supplementId ? { supplement_id: supplementId } : {}),
                    run_id: runId, kind: progress ? "progress" : blocked ? "blocked" : partial ? "partial" : "full",
                    summary: progress ? "第一份报告：已写入隔离文件，仍需最终核对。" : "Offline fixture report · 中文成果已写入",
                    checks: ["Fixture tool observed counter; integration test independently reads the file."],
                    remaining: progress ? "仍需最终核对" : partial ? "第二阶段尚未完成" : blocked ? "等待测试输入" : "",
                    ...(taskTitle === "HANDOFF" ? { handoff: { result: "仅核对了临时成果", blocker: "交接材料还缺下一步方案",
                        next_actor: "executor", next_action: "先补齐方案与核对依据", user_need: "none" } } : {}),
                    ...(taskTitle === "FULL_LEGACY" ? {} : { brief: { actions: ["写入隔离测试文件"], outcome: "已保存中文样例成果", verification: "自报计数器结果；集成测试另行读文件核实" } }),
                } }];
                if (lateFailure && !used("waggle_report")) {
                    output.content.push({ type: "toolCall", id: `fail-${runId}`, name: "waggle_fixture_effect", arguments: { slow: false, fail: true } });
                } else if (lateFailure) output.content = [{ type: "text", text: "Fixture ends after deliberate late failure, no replacement report." }];
            }
            if (fabricMode) {
                const visible = new Set<string>();
                for (const message of messages) if (message.role === "system") {
                    message.toolsAdded?.forEach(tool => visible.add(tool.name));
                    message.toolsRemoved?.forEach(tool => visible.delete(tool.name));
                }
                fs.writeFileSync(path.join(process.env.WAGGLE_TEST_PROJECT!, "fixture-fabric-loadout.json"), JSON.stringify([...visible]), { mode: 0o600 });
                const latest = messages.findLastIndex(message => message.role === "user");
                const user = messages[latest];
                const text = user?.role === "user" ? (typeof user.content === "string" ? user.content : user.content.filter(part => part.type === "text").map(part => part.text).join("\n")) : "";
                if (text === "FIXTURE_FABRIC_ROUTE_PROBE") {
                    const attempted = messages.slice(latest + 1).some(message => message.role === "toolResult" && message.toolName === "fabric_exec");
                    output.content = attempted ? [{ type: "text", text: "Offline Fabric probe finished." }] : [{ type: "toolCall", id: "fixture-fabric-route", name: "fabric_exec", arguments: {
                        code: `let descriptor; try {descriptor=await tools.describe({ref:'extensions.waggle_report'});}catch{}; let error=''; try {await extensions.waggle_report({run_id:'00000000-0000-4000-8000-000000000001',kind:'progress',summary:'Disposable route probe',checks:[],remaining:''});}catch(e){error=String(e)}; return {listed:!!descriptor,error};`, resultFormat: "json",
                    } }];
                } else {
                    output.content = output.content.map(part => {
                        if (part.type !== "toolCall") return part;
                        if (part.name === "waggle_report" && taskTitle === "DECISIONS") {
                            const specs = part.arguments.decisions as Record<string, unknown>[];
                            part.arguments.decisions = [1, 2, 3, 4].map(index => ({ ...specs[0], key: `fixture_${index}`, question: `隔离问题 ${index}？` }));
                            part.arguments.handoff = { result: "已提交四项隔离问题", blocker: "仅验证报告通道", next_actor: "executor", next_action: "核对临时报告字段", user_need: "none" };
                        }
                        return { type: "toolCall", id: `fabric-${part.id}`, name: "fabric_exec", arguments: {
                            code: `const result=await extensions.${part.name}(JSON.parse(π.args)); return {fixture_tools:[${JSON.stringify(part.name)}],result};`,
                            payloads: { args: JSON.stringify(part.arguments) }, resultFormat: "json",
                        } };
                    });
                }
            }
            const toolUse = output.content.some(part => part.type === "toolCall");
            output.stopReason = toolUse ? "toolUse" : "stop";
            if (taskTitle === "PROVIDER_ERROR" || (taskTitle === "DIRECT_ERROR" && !directContinuation) || (taskTitle === "COMPACT_PROVIDER_ERROR" && compactions > 0)) {
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
