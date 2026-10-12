/** TEST ONLY public optional-provider DTOs. Never launches a child or calls a model. */
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
    const project = process.env.WAGGLE_TEST_PROJECT;
    if (!project || fs.realpathSync(process.cwd()) !== project) throw new Error("Activity fixture requires its disposable project.");
    let mode = "CHILD", queries = 0;
    const id = "fixture-owned-backend-run";
    pi.registerTool({
        name: "subagent", label: "Offline derived-run fixture", description: "TEST ONLY. Returns synthetic typed identities; no real child.",
        parameters: Type.Object({ agent: Type.Optional(Type.String()), action: Type.Optional(Type.String()) }),
        async execute(_call, args) {
            mode = args.agent || "MANAGEMENT";
            return { content: [{ type: "text", text: "PRIVATE-RESULT-DO-NOT-EXPORT" }],
                details: { runId: id, asyncId: id, task: "PRIVATE-TASK-DO-NOT-EXPORT" } };
        },
    });
    pi.events.on("subagents:rpc:v1:request", raw => {
        const request = raw as { version: number; requestId: string; method: string; params: object };
        queries += 1;
        fs.appendFileSync(path.join(project, "fixture-rpc-methods.txt"), request.method + "\n", { mode: 0o600 });
        if (request.method !== "status" || request.version !== 1) throw new Error("Observer attempted non-status RPC");
        if (mode === "NO_PROVIDER") return;
        const reply = { version: 1, requestId: request.requestId, success: true,
            data: { text: "PRIVATE-STATUS-DO-NOT-EXPORT", asyncSnapshot: {
                kind: "pi-subagents.async-status-snapshot", version: mode === "BAD_VERSION" ? 2 : 1,
                generatedAt: Date.now(), runs: [
                    { id, state: queries === 1 ? "running" : "complete", label: "PRIVATE-LABEL-DO-NOT-EXPORT" },
                    { id: "another-tasks-run", state: "failed", label: "PRIVATE-OTHER-TASK" },
                ],
            } } };
        if (mode === "WRONG_REPLY") reply.requestId = "unrelated-request";
        pi.events.emit(`subagents:rpc:v1:reply:${request.requestId}`, reply);
    });
}
