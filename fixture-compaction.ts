/** Test-only pi-context lifecycle: abort, settle, deferred navigation, custom continuation. */
import * as fs from "node:fs";
import type { ExtensionAPI, ExtensionCommandContext, SessionManager } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

export default function (pi: ExtensionAPI) {
    if (!process.env.WAGGLE_TEST_PROJECT) throw new Error("Compaction fixture requires a disposable project.");
    let command: ExtensionCommandContext | undefined;
    let mode = "normal";
    let pending: { target: string; summary: string } | undefined;
    let release: (() => void) | undefined;
    pi.registerCommand("fixture-compaction", {
        description: "Configure only the disposable compaction fixture",
        handler: async (args, ctx) => {
            if (fs.realpathSync(ctx.cwd) !== process.env.WAGGLE_TEST_PROJECT) throw new Error("Non-test cwd.");
            if (args === "release") { release?.(); release = undefined; return; }
            if (args === "notice") {
                pi.sendMessage({ customType: "pi-context", content: "context_compact complete. Unrequested old continuation.", display: false }, { triggerTurn: true });
                return;
            }
            if (args === "navigate") {
                const target = ctx.sessionManager.getEntries()[0].id;
                const result = await ctx.navigateTree(target, { summarize: false });
                ctx.ui.notify(`fixture-navigation-cancelled=${result.cancelled}`, "info");
                return;
            }
            command = ctx;
            mode = args || "normal";
        },
    });
    pi.registerTool({
        name: "context_compact", label: "Fixture context compact", description: "OFFLINE lifecycle fixture only",
        parameters: Type.Object({ target: Type.String(), summary: Type.String() }),
        async execute(_id, params) {
            if (!command) throw new Error("Enable the disposable fixture first.");
            if (mode === "tool-error") throw new Error("Deliberate compaction tool failure.");
            if (mode === "noop") return { content: [{ type: "text", text: "Already at target fixture" }], details: {} };
            pending = params;
            if (mode === "pending-tool") await command.ui.confirm("Fixture compact receipt", "Pause before the compaction receipt.");
            return { content: [{ type: "text", text: "compact start" }], details: {} };
        },
    });
    pi.on("turn_end", (_event, ctx) => { if (pending) ctx.abort(); });
    pi.on("agent_end", (_event, ctx) => {
        if (!pending || !command) return;
        const request = pending, control = command;
        pending = undefined;
        const oldLeaf = ctx.sessionManager.getLeafId();
        // Match pi-context: scheduling outside agent_end is necessary to let the core settle.
        setTimeout(async () => {
            try {
                await control.waitForIdle();
                if (mode === "pause" || mode === "missing") {
                    await new Promise<void>(resolve => {
                        release = resolve;
                        control.ui.notify("fixture-compaction-paused", "info");
                    });
                    if (mode === "missing") return;
                }
                const branch = control.sessionManager.getBranch();
                const index = branch.findIndex(entry => entry.id === oldLeaf);
                const advanced = index < 0 || branch.slice(index + 1).some(entry =>
                    !["custom", "label", "session_info", "model_change", "thinking_level_change"].includes(entry.type));
                if (advanced || mode === "cancel") {
                    pi.sendMessage({ customType: "pi-context", content: "context_compact cancelled: conversation advanced before compaction completed.", display: false }, { triggerTurn: true, deliverAs: "followUp" });
                    return;
                }
                const sm = control.sessionManager as SessionManager;
                const target = mode === "failure" ? "missing-target" : sm.getEntries()[0].id;
                const summary = mode === "mismatch" ? "Not the requested summary" : `(handoff summary from fixture)\n${request.summary}`;
                const next = sm.branchWithSummary(target, summary);
                sm.branch(target);
                // Like the installed pi-context, it currently ignores cancellation here.
                await control.navigateTree(next, { summarize: false });
                pi.sendMessage({ customType: "pi-context", content: "context_compact complete. A handoff summary of your previous conversation path was injected above. Read it to understand your new state. Execute the Next Step from the summary", display: false }, { triggerTurn: true, deliverAs: "followUp" });
            } catch {
                pi.sendMessage({ customType: "pi-context", content: "context_compact failed: fixture navigation failed. No compaction was applied; continue from the current path.", display: false }, { triggerTurn: true, deliverAs: "followUp" });
            }
        }, 0);
    });
}
