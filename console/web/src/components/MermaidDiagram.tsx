"use client";

/* Render only the report's bounded flowchart subset. Unsupported syntax and
 * render failures retain the original, escaped code block. */
import * as React from "react";
import { prepareReportMermaid, REPORT_MERMAID_MAX_EDGES, REPORT_MERMAID_MAX_SOURCE } from "@/lib/report-mermaid";

let initialize: Promise<typeof import("mermaid")["default"]> | null = null;

function mermaidReady() {
  if (!initialize) {
    initialize = import("mermaid").then((mod) => {
      mod.default.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "dark",
        htmlLabels: false,
        flowchart: { useMaxWidth: false },
        suppressErrorRendering: true,
        maxTextSize: REPORT_MERMAID_MAX_SOURCE,
        maxEdges: REPORT_MERMAID_MAX_EDGES,
      });
      return mod.default;
    }).catch(error => {
      initialize = null;
      throw error;
    });
  }
  return initialize;
}

let counter = 0;

export default function MermaidDiagram({ chart }: { chart: string }) {
  const canonical = React.useMemo(() => prepareReportMermaid(chart), [chart]);
  const [result, setResult] = React.useState<{ chart: string; svg: string } | null>(null);

  React.useEffect(() => {
    if (canonical === null) return;
    let cancelled = false;
    let host: HTMLDivElement | null = null;
    const render = async () => {
      try {
        const mermaid = await mermaidReady();
        if (cancelled) return;
        const id = `report-mmd-${++counter}`;
        host = document.createElement("div");
        host.dataset.reportMermaidRenderer = id;
        host.setAttribute("aria-hidden", "true");
        // Mermaid needs measurable DOM during layout; keep it outside the page
        // flow and remove the owned container on success, failure, or unmount.
        Object.assign(host.style, {
          position: "fixed", left: "-10000px", top: "0", width: "1000px",
          visibility: "hidden", pointerEvents: "none",
        });
        document.body.appendChild(host);
        const { svg } = await mermaid.render(id, canonical, host);
        if (!cancelled) setResult({ chart, svg });
      } catch {
        if (!cancelled) setResult({ chart, svg: "" });
      } finally {
        host?.remove();
      }
    };
    void render();
    return () => {
      cancelled = true;
      host?.remove();
    };
  }, [chart, canonical]);

  const svg = canonical !== null && result?.chart === chart ? result.svg : "";
  if (!svg) {
    return (
      <pre>
        <code className="language-mermaid">{chart}</code>
      </pre>
    );
  }
  return <figure className="mermaid-block" dangerouslySetInnerHTML={{ __html: svg }} />;
}
