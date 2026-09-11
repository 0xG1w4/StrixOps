/** The report supports a small, non-interactive flowchart language.
 * Parse the whole input and reserialize it; never send the original source to
 * Mermaid, whose other features can fetch images or apply diagram directives.
 */
export const REPORT_MERMAID_MAX_SOURCE = 10_000;
export const REPORT_MERMAID_MAX_NODES = 64;
export const REPORT_MERMAID_MAX_EDGES = 128;
const MAX_LABEL = 256;
const PLAIN_LABEL = /^[\p{L}\p{M}\p{N} .,:;!?/()=+_\-，。！？：；（）]+$/u;

type Shape = "rectangle" | "round" | "diamond";
type Node = { id: string; label: string; shape: Shape; declared: boolean };
type Edge = { from: string; to: string; label: string | null };

function labelText(raw: string): string | null {
  let label = raw.trim();
  if (label.startsWith('"') && label.endsWith('"')) label = label.slice(1, -1).trim();
  return label.length > 0 && label.length <= MAX_LABEL && PLAIN_LABEL.test(label) ? label : null;
}

export function prepareReportMermaid(source: string): string | null {
  if (!source || source.length > REPORT_MERMAID_MAX_SOURCE) return null;
  const lines = source.replace(/\r\n/g, "\n").trim().split("\n");
  const header = /^flowchart[ \t]+(TD|TB|LR|RL|BT)[ \t]*;?$/.exec(lines.shift() || "");
  if (!header) return null;

  const nodes = new Map<string, Node>();
  const edges: Edge[] = [];
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;
    let cursor = 0;
    const space = () => { while (line[cursor] === " " || line[cursor] === "\t") cursor++; };
    const readNode = (): Node | null => {
      space();
      const match = /^[A-Za-z_][A-Za-z0-9_]{0,63}/.exec(line.slice(cursor));
      if (!match) return null;
      const sourceId = match[0];
      cursor += sourceId.length;
      space();
      const opener = line[cursor];
      const closer = opener === "[" ? "]" : opener === "(" ? ")" : opener === "{" ? "}" : null;
      let label: string | null = null;
      const shape: Shape = opener === "(" ? "round" : opener === "{" ? "diamond" : "rectangle";
      if (closer) {
        cursor++;
        space();
        const start = cursor;
        if (line[cursor] === '"') {
          const quote = line.indexOf('"', cursor + 1);
          if (quote < 0) return null;
          cursor = quote + 1;
          label = labelText(line.slice(start, cursor));
          space();
          if (line[cursor] !== closer) return null;
        } else {
          const end = line.indexOf(closer, cursor);
          if (end < 0) return null;
          label = labelText(line.slice(start, end));
          cursor = end;
        }
        if (!label) return null;
        cursor++;
      }
      const previous = nodes.get(sourceId);
      if (previous) {
        if (label !== null) {
          // Conflicting declarations should fall back, not silently change meaning.
          if (previous.declared && (previous.label !== label || previous.shape !== shape)) return null;
          previous.label = label;
          previous.shape = shape;
          previous.declared = true;
        }
        return previous;
      }
      if (nodes.size >= REPORT_MERMAID_MAX_NODES) return null;
      const node: Node = { id: `n${nodes.size}`, label: label ?? sourceId, shape, declared: label !== null };
      nodes.set(sourceId, node);
      return node;
    };

    let from = readNode();
    if (!from) return null;
    while (true) {
      space();
      if (cursor === line.length || (line[cursor] === ";" && cursor === line.length - 1)) break;
      if (!line.startsWith("-->", cursor)) return null;
      cursor += 3;
      space();
      let label: string | null = null;
      if (line[cursor] === "|") {
        const end = line.indexOf("|", cursor + 1);
        if (end < 0) return null;
        label = labelText(line.slice(cursor + 1, end));
        if (label === null) return null;
        cursor = end + 1;
      }
      const to = readNode();
      if (!to || edges.length >= REPORT_MERMAID_MAX_EDGES) return null;
      edges.push({ from: from.id, to: to.id, label });
      from = to;
    }
  }
  if (!nodes.size) return null;

  const canonical = [`flowchart ${header[1]}`];
  for (const node of nodes.values()) {
    const [open, close] = node.shape === "round" ? ["(", ")"] : node.shape === "diamond" ? ["{", "}"] : ["[", "]"];
    canonical.push(`${node.id}${open}"${node.label}"${close}`);
  }
  for (const edge of edges) {
    canonical.push(`${edge.from} -->${edge.label === null ? "" : `|"${edge.label}"|`} ${edge.to}`);
  }
  const result = canonical.join("\n");
  return result.length <= REPORT_MERMAID_MAX_SOURCE ? result : null;
}
