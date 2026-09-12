export type RunTab = "conversation" | "findings" | "notes" | "files" | "report";
export type NotebookView = "shared" | "coverage" | "threat_models";
export type FileFilter = "all" | "evidence" | "artifacts";

/** Keep saved links to the former nine-tab cockpit useful. */
export function readRunNavigation(params: Pick<URLSearchParams, "get">) {
  const requested = params.get("tab");
  const aliases: Record<string, RunTab> = {
    conversation: "conversation", agents: "conversation", hints: "conversation",
    findings: "findings", notes: "notes", assessment: "notes",
    files: "files", evidence: "files", artifacts: "files", report: "report",
  };
  const tab: RunTab = requested && Object.prototype.hasOwnProperty.call(aliases, requested)
    ? aliases[requested] : "conversation";
  const requestedNotes = params.get("note_view");
  const noteView: NotebookView = requestedNotes === "shared" || requestedNotes === "coverage" || requestedNotes === "threat_models"
    ? requestedNotes : requested === "assessment" ? "coverage" : "shared";
  const requestedFiles = params.get("file_filter");
  const fileFilter: FileFilter = requestedFiles === "all" || requestedFiles === "evidence" || requestedFiles === "artifacts"
    ? requestedFiles : requested === "evidence" ? "evidence" : "all";
  const requestedAgent = params.get("agent");
  const agentId = requestedAgent === "all" ? "" : requestedAgent ?? "";
  return { tab, noteView, fileFilter, agentId };
}
