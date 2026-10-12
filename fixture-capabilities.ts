/** Local test controls only; never loaded by ordinary Waggle/Pi launch paths. */
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { reportSchema } from "./capabilities.ts";
export default function (pi: ExtensionAPI) {
    if (!process.env.WAGGLE_TEST_PROJECT) throw new Error("Capability fixture requires a disposable project.");
    let rewriteProbes = false, currentRun = "";
    pi.on("message_end", event => {
        const message = event.message;
        if (message.role === "custom" && message.customType === "waggle-task") currentRun = (message.details as { runId: string }).runId;
    });
    pi.on("tool_call", (event, ctx) => {
        if (!rewriteProbes || !currentRun || event.toolName !== "waggle_report" || !event.toolCallId.includes("waggle-report-probe:")) return;
        if (fs.realpathSync(ctx.cwd) !== process.env.WAGGLE_TEST_PROJECT) throw new Error("Fixture refuses non-test cwd.");
        event.input.run_id = currentRun; // Test the real SDK's supported input-transforming middleware.
        fs.writeFileSync(path.join(ctx.cwd, "fixture-probe-rewrite.json"), JSON.stringify({ currentRun }), { mode: 0o600 });
    });
    pi.registerCommand("capability-fixture", { description: "TEST ONLY: hide/show the real exposed report tool",
        handler: async (args) => {
            if (args === "rewrite-probes") { rewriteProbes = true; return; }
            if (args === "invalidate-schema" || args === "restore-schema") {
                const tool = pi.getAllTools().find(tool => tool.name === "waggle_report");
                if (!tool) throw new Error("Missing fixture report tool.");
                const schema = tool.parameters as typeof reportSchema;
                schema.properties.checks.maxItems = args === "invalidate-schema" ? 0 : 32;
                return;
            }
            const names = pi.getActiveTools().filter(name => name !== "waggle_report");
            if (args === "show") names.push("waggle_report");
            else if (args !== "hide") throw new Error("Unknown fixture mode.");
            pi.setActiveTools(names);
        } });
}
