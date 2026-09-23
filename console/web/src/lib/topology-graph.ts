import type { TopologyEdge, TopologyNode } from "./topology";

export type GroupingBasis = "recorded_subnet" | "scan_range" | "address_group" | "unassigned";
export type Point = { x: number; y: number };
export type Rect = Point & { width: number; height: number };
export type Camera = Point & { zoom: number };
export type TopologyGroup = { id: string; cidr: string; context: string; contexts: string[]; bases: GroupingBasis[]; nodes: TopologyNode[] };
export type PlacedGroup = TopologyGroup & Rect;
export type GraphPositions = { groups: Record<string, Point>; nodes: Record<string, Point> };
export type EdgeBundle = { id: string; source: string; target: string; edges: TopologyEdge[] };
export const HOST_WIDTH = 132;
export const HOST_HEIGHT = 116;
export const GROUP_HEADER = 96;
export const GROUP_PADDING = 28;
export const CELL_WIDTH = 168;
export const CELL_HEIGHT = 140;

function ipParts(input: string): { bits: number; value: bigint } | null {
  const text = input.trim().replace(/^\[|\]$/g, "").replace(/%[^%\s]+$/, "");
  if (/^\d+\.\d+\.\d+\.\d+$/.test(text)) {
    const parts = text.split(".").map(Number);
    if (parts.some((part) => part > 255)) return null;
    return { bits: 32, value: parts.reduce((result, part) => result * 256n + BigInt(part), 0n) };
  }
  if (!text.includes(":") || text.includes("%")) return null;
  let address = text;
  if (address.includes(".")) {
    const index = address.lastIndexOf(":"), v4 = ipParts(address.slice(index + 1));
    if (!v4 || v4.bits !== 32) return null;
    address = `${address.slice(0, index)}:${(v4.value >> 16n).toString(16)}:${(v4.value & 65535n).toString(16)}`;
  }
  const halves = address.split("::");
  if (halves.length > 2) return null;
  const left = halves[0] ? halves[0].split(":") : [], right = halves[1] ? halves[1].split(":") : [];
  const missing = 8 - left.length - right.length;
  if ((halves.length === 1 && missing !== 0) || (halves.length === 2 && missing < 1)) return null;
  const parts = [...left, ...Array(missing).fill("0"), ...right];
  if (parts.some((part) => !/^[\da-f]{1,4}$/i.test(part))) return null;
  return { bits: 128, value: parts.reduce((result, part) => result * 65536n + BigInt(parseInt(part, 16)), 0n) };
}

function formatIp(value: bigint, bits: number): string {
  if (bits === 32) return [24n, 16n, 8n, 0n].map((shift) => Number((value >> shift) & 255n)).join(".");
  const parts = Array.from({ length: 8 }, (_, index) => ((value >> BigInt((7 - index) * 16)) & 65535n).toString(16));
  let best = -1, length = 1;
  for (let i = 0; i < 8;) {
    if (parts[i] !== "0") { i++; continue; }
    let end = i; while (end < 8 && parts[end] === "0") end++;
    if (end - i > length) { best = i; length = end - i; } i = end;
  }
  return best < 0 ? parts.join(":") : `${parts.slice(0, best).join(":")}::${parts.slice(best + length).join(":")}`;
}

function canonicalCidr(value: string, address?: string): string | null {
  const match = /^(.+)\/(\d{1,3})$/.exec(value.trim());
  if (!match) return null;
  const ip = ipParts(match[1]), prefix = Number(match[2]), host = address ? ipParts(address) : null;
  if (!ip || prefix > ip.bits || (address !== undefined && !host) || (host && host.bits !== ip.bits)) return null;
  const shift = BigInt(ip.bits - prefix), network = ip.value >> shift << shift;
  if (host && (host.value >> shift << shift) !== network) return null;
  return `${formatIp(network, ip.bits)}/${prefix}`;
}

export function getNodeGrouping(node: TopologyNode): { key: string; cidr: string; basis: GroupingBasis; context: string } {
  const legacyContext = node.network_context && node.sources.length > 0 && node.sources.every((source) => source.run !== node.network_context) ? node.network_context : "";
  const context = node.group_context ?? legacyContext;
  if (node.group_basis === "unassigned" && !node.group_cidr) return { key: JSON.stringify(["", ""]), cidr: "", basis: "unassigned", context };
  const address = node.ip || node.address;
  const provided = node.group_cidr ? canonicalCidr(node.group_cidr, address) : null;
  let cidr = provided ?? "", basis: GroupingBasis = provided ? (node.group_basis ?? "address_group") : "unassigned";
  if (!provided) {
    const subnet = node.subnet ? canonicalCidr(node.subnet, address) : null, ip = ipParts(address);
    if (subnet && !node.group_conflict) { cidr = subnet; basis = "recorded_subnet"; }
    else if (ip) {
      const prefix = ip.bits === 32 ? 24 : 64, shift = BigInt(ip.bits - prefix);
      cidr = `${formatIp(ip.value >> shift << shift, ip.bits)}/${prefix}`; basis = "address_group";
    }
  }
  // Context identifies observations; the canvas groups their display by CIDR.
  // Node IDs and context metadata stay intact even when address ranges overlap.
  return { key: JSON.stringify(["", cidr]), cidr, basis, context };
}

export function compareAddresses(a: string, b: string): number {
  const first = ipParts(a.split("/")[0]), second = ipParts(b.split("/")[0]);
  if (first && second) return first.bits - second.bits || (first.value < second.value ? -1 : first.value > second.value ? 1 : 0);
  return a.localeCompare(b, "en", { numeric: true });
}

export function groupTopologyNodes(nodes: TopologyNode[]): TopologyGroup[] {
  const groups = new Map<string, TopologyGroup>();
  for (const node of nodes) {
    const group = getNodeGrouping(node), existing = groups.get(group.key);
    if (existing) {
      existing.nodes.push(node);
      if (!existing.bases.includes(group.basis)) existing.bases.push(group.basis);
      if (!existing.contexts.includes(group.context)) existing.contexts.push(group.context);
    } else groups.set(group.key, { id: group.key, cidr: group.cidr, context: group.context, contexts: [group.context], bases: [group.basis], nodes: [node] });
  }
  return [...groups.values()].sort((a, b) => Number(!a.cidr) - Number(!b.cidr) || compareAddresses(a.cidr, b.cidr)
    || Number(a.cidr.split("/")[1]) - Number(b.cidr.split("/")[1]) || a.cidr.localeCompare(b.cidr, "en")).map((group) => ({
    ...group,
    context: group.contexts.length === 1 ? group.contexts[0] : "",
    contexts: group.contexts.sort((a, b) => a.localeCompare(b, "en")),
    bases: group.bases.sort(),
    nodes: group.nodes.sort((a, b) => compareAddresses(a.ip || a.address, b.ip || b.address) || a.id.localeCompare(b.id, "en")),
  }));
}

export function groupSize(group: TopologyGroup, collapsed: boolean): { width: number; height: number } {
  const columns = Math.min(4, Math.max(2, Math.ceil(Math.sqrt(group.nodes.length))));
  return { width: collapsed ? 360 : columns * CELL_WIDTH + GROUP_PADDING * 2 - (CELL_WIDTH - HOST_WIDTH), height: collapsed ? 154 : GROUP_HEADER + Math.ceil(group.nodes.length / columns) * CELL_HEIGHT + 8 };
}

export function defaultNodePosition(group: TopologyGroup, index: number): Point {
  const columns = Math.min(4, Math.max(2, Math.ceil(Math.sqrt(group.nodes.length))));
  return { x: GROUP_PADDING + index % columns * CELL_WIDTH, y: GROUP_HEADER + Math.floor(index / columns) * CELL_HEIGHT };
}

export function intersects(a: Rect, b: Rect, margin = 0): boolean {
  return a.x - margin < b.x + b.width && a.x + a.width + margin > b.x && a.y - margin < b.y + b.height && a.y + a.height + margin > b.y;
}

export function placeGroups(groups: TopologyGroup[], collapsed: Set<string>, positions: Record<string, Point>, compact = false): PlacedGroup[] {
  const columns = compact ? 1 : groups.length <= 3 ? Math.max(1, groups.length) : Math.ceil(Math.sqrt(groups.length));
  const placed: PlacedGroup[] = [];
  let rowY = 64, x = 48, rowHeight = 0;
  groups.forEach((group, index) => {
    if (index && index % columns === 0) { x = 48; rowY += rowHeight + 112; rowHeight = 0; }
    const size = groupSize(group, collapsed.has(group.id)), saved = positions[group.id];
    const next = { ...group, ...size, x: saved?.x ?? x, y: saved?.y ?? rowY };
    if (!saved) {
      // Only new groups move on refresh. Existing arrangements remain familiar.
      let blocker: PlacedGroup | undefined;
      while ((blocker = placed.find((other) => intersects(next, other, 64)))) next.y = blocker.y + blocker.height + 112;
    }
    placed.push(next); x += size.width + 112; rowHeight = Math.max(rowHeight, size.height);
  });
  return placed;
}

/** Keep saved positions unless a changed group footprint now overlaps them. */
export function settleGroupOverlaps(groups: PlacedGroup[], priority?: string): PlacedGroup[] {
  const ordered = priority ? [...groups.filter((group) => group.id === priority), ...groups.filter((group) => group.id !== priority)] : groups;
  const settled: PlacedGroup[] = [];
  for (const original of ordered) {
    const group = { ...original }; let blocker: PlacedGroup | undefined;
    while ((blocker = settled.find((other) => intersects(group, other, 48)))) group.y = blocker.y + blocker.height + 112;
    settled.push(group);
  }
  const byId = new Map(settled.map((group) => [group.id, group]));
  return groups.map((group) => byId.get(group.id)!);
}

export function graphBounds(groups: Rect[]): Rect {
  if (!groups.length) return { x: 0, y: 0, width: 400, height: 300 };
  const x = Math.min(...groups.map((group) => group.x)) - 32, y = Math.min(...groups.map((group) => group.y)) - 32;
  return { x, y, width: Math.max(...groups.map((group) => group.x + group.width)) - x + 32, height: Math.max(...groups.map((group) => group.y + group.height)) - y + 32 };
}

export function fitCamera(bounds: Rect, width: number, height: number): Camera {
  const zoom = Math.max(.025, Math.min(1.15, (width - 48) / bounds.width, (height - 120) / bounds.height));
  return { zoom, x: width / 2 - (bounds.x + bounds.width / 2) * zoom, y: (height - 34) / 2 - (bounds.y + bounds.height / 2) * zoom };
}

export function bundleEdges(edges: TopologyEdge[], groupForNode: Map<string, string>, collapsed: Set<string>): EdgeBundle[] {
  const bundles = new Map<string, EdgeBundle>();
  for (const edge of edges) {
    const sourceGroup = groupForNode.get(edge.source), targetGroup = groupForNode.get(edge.target);
    if (!sourceGroup || !targetGroup || (sourceGroup === targetGroup && collapsed.has(sourceGroup))) continue;
    const source = collapsed.has(sourceGroup) ? `group:${sourceGroup}` : `node:${edge.source}`;
    const target = collapsed.has(targetGroup) ? `group:${targetGroup}` : `node:${edge.target}`;
    const id = JSON.stringify([source, target]);
    const existing = bundles.get(id);
    if (existing) existing.edges.push(edge); else bundles.set(id, { id, source, target, edges: [edge] });
  }
  return [...bundles.values()];
}

const length = (points: Point[]) => points.slice(1).reduce((total, point, index) => total + Math.abs(point.x - points[index].x) + Math.abs(point.y - points[index].y), 0);
function clearSegment(a: Point, b: Point, obstacles: Rect[]): boolean {
  return obstacles.every((rect) => a.x === b.x
    ? a.x <= rect.x || a.x >= rect.x + rect.width || Math.max(a.y, b.y) <= rect.y || Math.min(a.y, b.y) >= rect.y + rect.height
    : a.y <= rect.y || a.y >= rect.y + rect.height || Math.max(a.x, b.x) <= rect.x || Math.min(a.x, b.x) >= rect.x + rect.width);
}
function clearPath(points: Point[], obstacles: Rect[]): boolean { return points.slice(1).every((point, index) => clearSegment(points[index], point, obstacles)); }

/** Rectilinear routing through free space: fast bend candidates, then a visibility grid. */
export function routeOrthogonal(start: Point, end: Point, obstacles: Rect[]): Point[] {
  const candidates: Point[][] = [
    [start, { x: end.x, y: start.y }, end], [start, { x: start.x, y: end.y }, end],
  ];
  const xs = [...new Set([start.x, end.x, ...obstacles.flatMap((rect) => [rect.x - 12, rect.x + rect.width + 12])])].sort((a, b) => a - b);
  const ys = [...new Set([start.y, end.y, ...obstacles.flatMap((rect) => [rect.y - 12, rect.y + rect.height + 12])])].sort((a, b) => a - b);
  for (const x of xs) candidates.push([start, { x, y: start.y }, { x, y: end.y }, end]);
  for (const y of ys) candidates.push([start, { x: start.x, y }, { x: end.x, y }, end]);
  candidates.sort((a, b) => length(a) - length(b));
  const simple = candidates.find((candidate) => clearPath(candidate, obstacles));
  if (simple) return simple;
  const columns = xs.length, startIndex = ys.indexOf(start.y) * columns + xs.indexOf(start.x), endIndex = ys.indexOf(end.y) * columns + xs.indexOf(end.x);
  const pointAt = (index: number): Point => ({ x: xs[index % columns], y: ys[Math.floor(index / columns)] });
  const distances = new Map<number, number>([[startIndex, 0]]), previous = new Map<number, number>(), visited = new Set<number>();
  const heap: { index: number; score: number }[] = [];
  function push(index: number, score: number) {
    heap.push({ index, score }); let child = heap.length - 1;
    while (child > 0) { const parent = Math.floor((child - 1) / 2); if (heap[parent].score <= score) break; [heap[child], heap[parent]] = [heap[parent], heap[child]]; child = parent; }
  }
  function pop() {
    const first = heap[0], last = heap.pop()!;
    if (heap.length) {
      heap[0] = last; let parent = 0;
      while (true) { let child = parent * 2 + 1; if (child >= heap.length) break; if (child + 1 < heap.length && heap[child + 1].score < heap[child].score) child++; if (heap[parent].score <= heap[child].score) break; [heap[parent], heap[child]] = [heap[child], heap[parent]]; parent = child; }
    }
    return first.index;
  }
  push(startIndex, 0);
  while (heap.length) {
    const index = pop(); if (visited.has(index)) continue;
    if (index === endIndex) { const result = [end]; let cursor = index; while (previous.has(cursor)) { cursor = previous.get(cursor)!; result.push(pointAt(cursor)); } return result.reverse(); }
    visited.add(index); const point = pointAt(index), column = index % columns, row = Math.floor(index / columns);
    const neighbors = [column ? index - 1 : -1, column + 1 < columns ? index + 1 : -1, row ? index - columns : -1, row + 1 < ys.length ? index + columns : -1];
    for (const next of neighbors) {
      if (next < 0 || visited.has(next)) continue;
      const nextPoint = pointAt(next); if (!clearSegment(point, nextPoint, obstacles)) continue;
      const distance = distances.get(index)! + Math.abs(point.x - nextPoint.x) + Math.abs(point.y - nextPoint.y);
      if (distance >= (distances.get(next) ?? Infinity)) continue;
      distances.set(next, distance); previous.set(next, index);
      push(next, distance + Math.abs(end.x - nextPoint.x) + Math.abs(end.y - nextPoint.y));
    }
  }
  // A dragged node may be enclosed by overlapping groups. A visible fallback retains the recorded relation.
  return [start, { x: start.x, y: end.y }, end];
}

export function pathFromPoints(points: Point[]): string {
  const compact = points.filter((point, index) => !index || point.x !== points[index - 1].x || point.y !== points[index - 1].y);
  return compact.map((point, index) => `${index ? "L" : "M"}${point.x},${point.y}`).join(" ");
}

export function pathLabel(points: Point[]): Point {
  let longest = 0, label = points[0];
  points.slice(1).forEach((point, index) => { const previous = points[index], distance = Math.abs(previous.x - point.x) + Math.abs(previous.y - point.y); if (distance > longest) { longest = distance; label = { x: (previous.x + point.x) / 2, y: (previous.y + point.y) / 2 - 10 }; } });
  return label;
}
