"use client";

import * as React from "react";
import { ChevronDown, ChevronUp, Expand, Focus, Grip, KeyRound, Minus, Monitor, Network, Plus, RotateCcw, Server, ShieldAlert } from "lucide-react";
import type { TopologyEdge, TopologyNode } from "@/lib/topology";
import { bundleEdges, defaultNodePosition, fitCamera, getNodeGrouping, graphBounds, groupTopologyNodes, groupSize, settleGroupOverlaps, CELL_HEIGHT, CELL_WIDTH, GROUP_HEADER, GROUP_PADDING, HOST_HEIGHT, HOST_WIDTH, intersects, pathFromPoints, pathLabel, placeGroups, routeOrthogonal, type Camera, type EdgeBundle, type GraphPositions, type GroupingBasis, type PlacedGroup, type Point, type Rect, type TopologyGroup } from "@/lib/topology-graph";
import styles from "./TopologyCanvas.module.css";

const COPY = {
  "zh-CN": { canvas: "交互式网络拓扑", fit: "适应全图", reset: "重新布局", expand: "展开全部", collapse: "收合全部", expandGroup: "展开", collapseGroup: "收合", zoomIn: "放大", zoomOut: "缩小", fullscreen: "全屏画布", exitFullscreen: "退出全屏", minimap: "缩图导航：点击定位；方向键移动视图", drag: "拖动平移 · 滚轮缩放", touch: "拖动平移 · 双指缩放", hosts: "主机", groups: "网络分组", relations: "条主机关联", inside: "条组内关系", matched: "个匹配", none: "没有匹配的主机", unassigned: "未归属", defaultContext: "未指定网络环境", recorded_subnet: "已记录网段", scan_range: "扫描范围", address_group: "地址分组", unassignedBasis: "未归属", mixed: "混合分组依据", verified: "已验证关系", unverified: "含未验证关系", credentials: "凭据", vulns: "漏洞", findings: "发现", overview: "缩放以查看主机", keyboard: "方向键平移，+ / − 缩放，0 适应全图", known: "仅显示已记录的主机关联", ports: "展开区块以查看具体主机", stored: "布局保存在此浏览器", danger: "高风险", all: "全部主机" },
  en: { canvas: "Interactive network topology", fit: "Fit to view", reset: "Reset layout", expand: "Expand all", collapse: "Collapse all", expandGroup: "Expand", collapseGroup: "Collapse", zoomIn: "Zoom in", zoomOut: "Zoom out", fullscreen: "Fullscreen canvas", exitFullscreen: "Exit fullscreen", minimap: "Minimap: click to navigate; arrow keys move the view", drag: "Drag to pan · Scroll to zoom", touch: "Drag to pan · Pinch to zoom", hosts: "hosts", groups: "network groups", relations: "host relationships", inside: "internal relationships", matched: "matches", none: "No matching hosts", unassigned: "Unassigned", defaultContext: "Network context unspecified", recorded_subnet: "Recorded subnet", scan_range: "Scan range", address_group: "Address group", unassignedBasis: "Unassigned", mixed: "Mixed grouping evidence", verified: "Verified relationship", unverified: "Includes unverified relationships", credentials: "Credentials", vulns: "Vulnerabilities", findings: "Findings", overview: "Zoom in to see hosts", keyboard: "Arrow keys to pan, + / − to zoom, 0 to fit", known: "Only recorded host relationships are shown", ports: "Expand groups to inspect individual hosts", stored: "Layout saved in this browser", danger: "High risk", all: "All hosts" },
} as const;

type Props = {
  projectId: string; nodes: TopologyNode[]; edges: TopologyEdge[]; locale: "zh-CN" | "en";
  selectedId: string | null; onSelect: (node: TopologyNode, element: HTMLButtonElement) => void;
  onSelectEdges: (edges: TopologyEdge[]) => void; query?: string; highlightIds?: string[]; selectedEdgeIds?: string[];
};
type Drag = { pointer: number; type: "pan" | "node" | "group"; id: string; start: Point; origin: Point; moved: boolean; button?: HTMLButtonElement };
type SavedView = { version: 1; positions: GraphPositions; collapsed: string[]; camera: Camera };
type EdgeRoute = EdgeBundle & { points: Point[]; path: string; label: Point; bounds: Rect };
const EMPTY_IDS: string[] = [];
const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
const validPoint = (value: unknown): value is Point => !!value && typeof value === "object" && ["x", "y"].every((key) => typeof (value as Record<string, unknown>)[key] === "number" && Number.isFinite((value as Record<string, number>)[key]) && Math.abs((value as Record<string, number>)[key]) < 10_000_000);
function readView(key: string): SavedView | null {
  try {
    const raw = localStorage.getItem(key); if (!raw || raw.length > 8_000_000) return null;
    const value = JSON.parse(raw) as SavedView;
    if (value.version !== 1 || !value.positions || !value.positions.groups || !value.positions.nodes || !Array.isArray(value.collapsed) || !validPoint(value.camera) || !Number.isFinite(value.camera.zoom)) return null;
    const clean = (points: Record<string, Point>) => Object.fromEntries(Object.entries(points).filter(([id, point]) => id.length < 2048 && validPoint(point)));
    return { version: 1, positions: { groups: clean(value.positions.groups), nodes: clean(value.positions.nodes) }, collapsed: value.collapsed.filter((id) => typeof id === "string"), camera: { ...value.camera, zoom: clamp(value.camera.zoom, .025, 2.5) } };
  } catch { return null; }
}

function retainNodePositions(groups: TopologyGroup[], saved: Record<string, Point>): Record<string, Point> {
  const result: Record<string, Point> = {};
  for (const group of groups) {
    const size = groupSize(group, false), occupied = new Map<string, Rect[]>();
    const cell = (point: Point) => ({ x: Math.floor(point.x / CELL_WIDTH), y: Math.floor(point.y / CELL_HEIGHT) });
    const overlap = (rect: Rect) => { const position = cell(rect); for (let x = position.x - 1; x <= position.x + 1; x++) for (let y = position.y - 1; y <= position.y + 1; y++) if (occupied.get(`${x},${y}`)?.some((other) => intersects(rect, other, 4))) return true; return false; };
    const occupy = (rect: Rect) => { const position = cell(rect), key = `${position.x},${position.y}`; occupied.set(key, [...(occupied.get(key) ?? []), rect]); };
    for (const node of group.nodes) {
      if (!saved[node.id]) continue;
      const point = { x: clamp(saved[node.id].x, GROUP_PADDING, size.width - GROUP_PADDING - HOST_WIDTH), y: clamp(saved[node.id].y, GROUP_HEADER, size.height - HOST_HEIGHT - 18) };
      const rect = { ...point, width: HOST_WIDTH, height: HOST_HEIGHT };
      if (!overlap(rect)) { result[node.id] = point; occupy(rect); }
    }
    let nextCell = 0;
    for (const node of group.nodes) {
      if (result[node.id]) continue;
      let point: Point, rect: Rect;
      do { point = defaultNodePosition(group, nextCell++); rect = { ...point, width: HOST_WIDTH, height: HOST_HEIGHT }; } while (overlap(rect));
      result[node.id] = point; occupy(rect);
    }
  }
  return result;
}

function routeBundle(bundle: EdgeBundle, groupMap: Map<string, PlacedGroup>, nodeRects: Map<string, Rect>, groupForNode: Map<string, string>, collapsed: Set<string>, lane = 0): Point[] {
  const edge = bundle.edges[0], sourceGroup = groupMap.get(groupForNode.get(edge.source)!)!, targetGroup = groupMap.get(groupForNode.get(edge.target)!)!;
  const sourceRect = nodeRects.get(edge.source)!, targetRect = nodeRects.get(edge.target)!;
  const header = (group: PlacedGroup): Rect => ({ x: group.x, y: group.y, width: group.width, height: GROUP_HEADER - 24 });
  const nodeObstacles = (group: PlacedGroup, skip: Set<string>) => group.nodes.filter((node) => !skip.has(node.id)).map((node) => { const rect = nodeRects.get(node.id)!; return { x: rect.x - 5, y: rect.y - 5, width: rect.width + 10, height: rect.height + 10 }; });
  const sourceAnchor = { x: sourceRect.x + HOST_WIDTH / 2 + lane * 7, y: sourceRect.y }, targetAnchor = { x: targetRect.x + HOST_WIDTH / 2 + lane * 7, y: targetRect.y };
  if (sourceGroup.id === targetGroup.id) {
    if (edge.source === edge.target) return [sourceAnchor, { x: sourceAnchor.x, y: sourceAnchor.y - 16 }, { x: sourceAnchor.x + 52, y: sourceAnchor.y - 16 }, { x: sourceAnchor.x + 52, y: sourceAnchor.y + 24 }, { x: sourceAnchor.x + 25, y: sourceAnchor.y + 24 }];
    const start = { x: sourceAnchor.x, y: sourceAnchor.y - 14 + lane * 6 }, end = { x: targetAnchor.x, y: targetAnchor.y - 14 + lane * 6 };
    return [sourceAnchor, ...routeOrthogonal(start, end, [header(sourceGroup), ...nodeObstacles(sourceGroup, new Set())]), targetAnchor];
  }
  const right = targetGroup.x + targetGroup.width / 2 >= sourceGroup.x + sourceGroup.width / 2;
  function endpoint(group: PlacedGroup, nodeId: string, anchor: Point, exitsRight: boolean): Point[] {
    const x = exitsRight ? group.x + group.width : group.x, direction = exitsRight ? 1 : -1;
    if (collapsed.has(group.id)) return [{ x, y: group.y + group.height - 35 + lane * 8 }, { x: x + direction * (24 + lane * 6), y: group.y + group.height - 35 + lane * 8 }];
    const start = { x: anchor.x, y: anchor.y - 14 + lane * 6 }, end = { x: x + direction * (24 + lane * 6), y: start.y };
    return [anchor, ...routeOrthogonal(start, end, [header(group), ...nodeObstacles(group, new Set())])];
  }
  const start = endpoint(sourceGroup, edge.source, sourceAnchor, right), end = endpoint(targetGroup, edge.target, targetAnchor, !right);
  const outside = routeOrthogonal(start[start.length - 1], end[end.length - 1], [...groupMap.values()].map((group) => ({ x: group.x - 8, y: group.y - 8, width: group.width + 16, height: group.height + 16 })));
  return [...start, ...outside.slice(1), ...end.slice(0, -1).reverse()];
}

export default function TopologyCanvas({ projectId, nodes, edges, locale, selectedId, onSelect, onSelectEdges, query = "", highlightIds, selectedEdgeIds = EMPTY_IDS }: Props) {
  const copy = COPY[locale], canvasId = React.useId().replace(/:/g, ""), storageKey = `strixops:topology:view:v2:${projectId}`;
  const groups = React.useMemo(() => groupTopologyNodes(nodes), [nodes]);
  const [positions, setPositions] = React.useState<GraphPositions>({ groups: {}, nodes: {} });
  const [collapsed, setCollapsed] = React.useState<Set<string>>(new Set());
  const [camera, setCamera] = React.useState<Camera>({ x: 0, y: 0, zoom: 1 });
  const [size, setSize] = React.useState({ width: 0, height: 600 });
  const [ready, setReady] = React.useState(false), [dragging, setDragging] = React.useState(false);
  const [hovered, setHovered] = React.useState<string | null>(null);
  const stage = React.useRef<HTMLDivElement>(null), initialized = React.useRef("");
  const drag = React.useRef<Drag | null>(null), suppressClick = React.useRef(false), pointers = React.useRef(new Map<number, Point>());
  const pinch = React.useRef<{ distance: number; center: Point; camera: Camera } | null>(null);
  const cameraRef = React.useRef(camera); cameraRef.current = camera;
  const positionsRef = React.useRef(positions); positionsRef.current = positions;
  const settledGeometry = React.useRef("");
  const dimensions = React.useRef(size); dimensions.current = size;
  const groupForNode = React.useMemo(() => new Map(groups.flatMap((group) => group.nodes.map((node) => [node.id, group.id] as const))), [groups]);
  const placed = React.useMemo(() => placeGroups(groups, collapsed, positions.groups, size.width < 600), [groups, collapsed, positions.groups, size.width]);
  const groupMap = React.useMemo(() => new Map(placed.map((group) => [group.id, group])), [placed]);
  const nodeRects = React.useMemo(() => {
    const result = new Map<string, Rect>();
    for (const group of placed) group.nodes.forEach((node, index) => {
      const local = positions.nodes[node.id] ?? defaultNodePosition(group, index);
      result.set(node.id, { x: group.x + clamp(local.x, GROUP_PADDING, group.width - GROUP_PADDING - HOST_WIDTH), y: group.y + clamp(local.y, GROUP_HEADER, Math.max(GROUP_HEADER, group.height - HOST_HEIGHT - 18)), width: HOST_WIDTH, height: HOST_HEIGHT });
    });
    return result;
  }, [placed, positions.nodes]);
  const bounds = React.useMemo(() => graphBounds(placed), [placed]);
  const geometry = React.useMemo(() => JSON.stringify(groups.map((group) => [group.id, group.nodes.length, collapsed.has(group.id)])), [groups, collapsed]);
  const overview = camera.zoom < .3;
  const edgeCollapsed = React.useMemo(() => overview || !ready ? new Set(groups.map((group) => group.id)) : collapsed, [overview, ready, groups, collapsed]);
  const bundles = React.useMemo(() => bundleEdges(edges, groupForNode, edgeCollapsed), [edges, groupForNode, edgeCollapsed]);
  const bundleIds = React.useMemo(() => new Set(bundles.map((bundle) => bundle.id)), [bundles]);
  // A view change reuses paths. Only newly visible relationships need routing.
  const routeCache = React.useMemo(() => new Map<string, EdgeRoute>(), [bundles, groupMap, nodeRects, groupForNode, edgeCollapsed]);
  const viewport = React.useMemo(() => ({ x: -camera.x / camera.zoom - 180, y: -camera.y / camera.zoom - 180, width: size.width / camera.zoom + 360, height: size.height / camera.zoom + 360 }), [camera, size]);
  const routes = React.useMemo(() => {
    if (!ready) return [];
    const result: EdgeRoute[] = [];
    for (const bundle of bundles) {
      const edge = bundle.edges[0];
      const sourceGroup = groupMap.get(groupForNode.get(edge.source)!)!, targetGroup = groupMap.get(groupForNode.get(edge.target)!)!;
      const source = edgeCollapsed.has(sourceGroup.id) ? sourceGroup : nodeRects.get(edge.source)!;
      const target = edgeCollapsed.has(targetGroup.id) ? targetGroup : nodeRects.get(edge.target)!;
      if (!intersects(graphBounds([source, target]), viewport)) continue;
      let route = routeCache.get(bundle.id);
      if (!route) {
        const reciprocal = bundleIds.has(JSON.stringify([bundle.target, bundle.source])), lane = reciprocal ? bundle.source < bundle.target ? -1 : 1 : 0;
        const points = routeBundle(bundle, groupMap, nodeRects, groupForNode, edgeCollapsed, lane), label = pathLabel(points);
        route = { ...bundle, points, path: pathFromPoints(points), label: { ...label, y: label.y + lane * 8 }, bounds: graphBounds(points.map((point) => ({ ...point, width: 0, height: 0 }))) }; routeCache.set(bundle.id, route);
      }
      result.push(route);
    }
    return result;
  }, [ready, bundles, bundleIds, groupMap, groupForNode, edgeCollapsed, nodeRects, viewport, routeCache]);
  const selectedEdges = React.useMemo(() => new Set(selectedEdgeIds), [selectedEdgeIds]);
  const focusIds = React.useMemo(() => {
    if (selectedId) { const ids = new Set([selectedId]); for (const edge of edges) if (edge.source === selectedId || edge.target === selectedId) { ids.add(edge.source); ids.add(edge.target); } return ids; }
    if (selectedEdgeIds.length) return new Set(edges.filter((edge) => selectedEdges.has(edge.id)).flatMap((edge) => [edge.source, edge.target]));
    return highlightIds ? new Set(highlightIds) : null;
  }, [selectedId, edges, selectedEdgeIds, selectedEdges, highlightIds]);
  const internalCounts = React.useMemo(() => { const counts = new Map<string, number>(); for (const edge of edges) { const group = groupForNode.get(edge.source); if (group && group === groupForNode.get(edge.target)) counts.set(group, (counts.get(group) ?? 0) + 1); } return counts; }, [edges, groupForNode]);

  React.useEffect(() => {
    if (!stage.current) return;
    const observer = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(stage.current); return () => observer.disconnect();
  }, []);
  React.useEffect(() => {
    if (!size.width || initialized.current === storageKey) return;
    initialized.current = storageKey;
    const saved = readView(storageKey), expandedLayout = placeGroups(groups, new Set(), {}, size.width < 600);
    const savedGroups = { ...saved?.positions.groups }, savedCollapsed = new Set(saved?.collapsed);
    let regrouped = false;
    for (const group of groups) {
      const previousIds = group.contexts.map((context) => JSON.stringify([context, group.cidr]))
        .filter((id) => id !== group.id && savedGroups[id]);
      if (!previousIds.length) continue;
      regrouped = true;
      const previous = savedGroups[group.id] ? [group.id, ...previousIds] : previousIds;
      // Preserve one prior anchor, then resolve combined node positions below.
      // Keep a combined group open if any of its former groups was open.
      savedGroups[group.id] ??= savedGroups[previous[0]];
      if (previous.every((id) => savedCollapsed.has(id))) savedCollapsed.add(group.id);
      else savedCollapsed.delete(group.id);
    }
    const expandedFit = fitCamera(graphBounds(expandedLayout), size.width, size.height);
    const initialCollapsed = saved ? new Set(groups.filter((group) => savedCollapsed.has(group.id)).map((group) => group.id)) : new Set(groups.filter((group) => size.width < 600 || expandedFit.zoom < .6 || nodes.length > 150 || group.nodes.length > 60).map((group) => group.id));
    const layout = settleGroupOverlaps(placeGroups(groups, initialCollapsed, savedGroups, size.width < 600));
    setCollapsed(initialCollapsed); setPositions({ groups: Object.fromEntries(layout.map((group) => [group.id, { x: group.x, y: group.y }])), nodes: retainNodePositions(groups, saved?.positions.nodes ?? {}) });
    const bounds = graphBounds(layout), initialCamera = fitCamera(bounds, size.width, size.height);
    if ((!saved || regrouped) && initialCamera.zoom < .65) {
      initialCamera.zoom = Math.min(.8, (size.width - 32) / 360);
      initialCamera.x = 16 - bounds.x * initialCamera.zoom; initialCamera.y = 52 - bounds.y * initialCamera.zoom;
    }
    setCamera(saved && !regrouped ? saved.camera : initialCamera); setReady(true);
  }, [storageKey, size, groups, nodes.length]);
  React.useEffect(() => {
    if (!ready || initialized.current !== storageKey) return;
    // Freeze new groups once placed; saved node offsets survive source refreshes.
    if (placed.some((group) => !positions.groups[group.id]) || nodes.some((node) => !positions.nodes[node.id])) setPositions((current) => ({ groups: Object.fromEntries(placed.map((group) => [group.id, current.groups[group.id] ?? { x: group.x, y: group.y }])), nodes: retainNodePositions(groups, current.nodes) }));
  }, [placed, positions, ready, storageKey, nodes, groups]);
  React.useEffect(() => {
    if (!ready || dragging || settledGeometry.current === geometry) return;
    settledGeometry.current = geometry;
    const settled = settleGroupOverlaps(placed);
    if (settled.some((group, index) => group.x !== placed[index].x || group.y !== placed[index].y)) setPositions((current) => ({ ...current, groups: Object.fromEntries(settled.map((group) => [group.id, { x: group.x, y: group.y }])) }));
  }, [geometry, placed, ready, dragging]);
  React.useEffect(() => {
    if (!ready || dragging) return;
    const timer = window.setTimeout(() => {
      const ids = new Set(nodes.map((node) => node.id)), groupIds = new Set(groups.map((group) => group.id));
      const value: SavedView = { version: 1, camera, collapsed: [...collapsed].filter((id) => groupIds.has(id)), positions: { groups: Object.fromEntries(Object.entries(positions.groups).filter(([id]) => groupIds.has(id))), nodes: Object.fromEntries(Object.entries(positions.nodes).filter(([id]) => ids.has(id))) } };
      try { localStorage.setItem(storageKey, JSON.stringify(value)); } catch { /* Layout persistence is optional. */ }
    }, 350);
    return () => window.clearTimeout(timer);
  }, [camera, positions, collapsed, ready, dragging, storageKey, nodes, groups]);
  React.useEffect(() => {
    const clear = () => { try { localStorage.removeItem(storageKey); } catch { /* Storage can be disabled. */ } initialized.current = ""; setReady(false); };
    window.addEventListener("strixops:auth-cleared", clear); return () => window.removeEventListener("strixops:auth-cleared", clear);
  }, [storageKey]);

  const zoomAt = React.useCallback((factor: number, point?: Point) => {
    setCamera((current) => { const zoom = clamp(current.zoom * factor, .025, 2.5), center = point ?? { x: dimensions.current.width / 2, y: dimensions.current.height / 2 }; return { zoom, x: center.x - (center.x - current.x) * zoom / current.zoom, y: center.y - (center.y - current.y) * zoom / current.zoom }; });
  }, []);
  React.useEffect(() => {
    const element = stage.current; if (!element) return;
    const wheel = (event: WheelEvent) => { if ((event.target as HTMLElement).closest("[data-canvas-control]")) return; event.preventDefault(); const rect = element.getBoundingClientRect(); zoomAt(Math.exp(-clamp(event.deltaY, -120, 120) * .004), { x: event.clientX - rect.left, y: event.clientY - rect.top }); };
    element.addEventListener("wheel", wheel, { passive: false }); return () => element.removeEventListener("wheel", wheel);
  }, [zoomAt]);

  function fit() { setCamera(fitCamera(bounds, size.width, size.height)); }
  function settleGroups(nextCollapsed: Set<string>, priority?: string) {
    const layout = placeGroups(groups, nextCollapsed, positionsRef.current.groups, size.width < 600);
    const settled = settleGroupOverlaps(layout, priority);
    setPositions((current) => ({ ...current, groups: Object.fromEntries(settled.map((group) => [group.id, { x: group.x, y: group.y }])) }));
    setCollapsed(nextCollapsed); return settled;
  }
  function toggleGroup(group: PlacedGroup) {
    const next = new Set(collapsed); if (next.has(group.id)) next.delete(group.id); else next.add(group.id);
    const settled = settleGroups(next, group.id), changed = settled.find((candidate) => candidate.id === group.id)!;
    if (!next.has(group.id)) setCamera(fitCamera(graphBounds([changed]), size.width, size.height));
  }
  function toggleAll() {
    const next = collapsed.size === groups.length ? new Set<string>() : new Set(groups.map((group) => group.id));
    const layout = placeGroups(groups, next, {}, size.width < 600);
    setPositions((current) => ({ ...current, groups: Object.fromEntries(layout.map((group) => [group.id, { x: group.x, y: group.y }])) }));
    setCollapsed(next); setCamera(fitCamera(graphBounds(layout), size.width, size.height));
  }
  function resetLayout() {
    const layout = placeGroups(groups, collapsed, {}, size.width < 600);
    setPositions({ groups: Object.fromEntries(layout.map((group) => [group.id, { x: group.x, y: group.y }])), nodes: retainNodePositions(groups, {}) });
    setCamera(fitCamera(graphBounds(layout), size.width, size.height));
  }
  const focusNode = React.useCallback((id: string) => {
    const groupId = groupForNode.get(id), group = groupId ? groupMap.get(groupId) : null; if (!group) return;
    const next = new Set(collapsed); next.delete(group.id);
    const settled = settleGroups(next, group.id), moved = settled.find((candidate) => candidate.id === group.id)!;
    const index = group.nodes.findIndex((node) => node.id === id), local = positionsRef.current.nodes[id] ?? defaultNodePosition(group, index);
    const zoom = 1;
    setCamera({ zoom, x: size.width / 2 - (moved.x + local.x + HOST_WIDTH / 2) * zoom, y: size.height * .43 - (moved.y + local.y + 28) * zoom });
  // Settle from current grouping and collapse state; it never changes host membership.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groupForNode, groupMap, collapsed, size.width, size.height]);
  const lastFocused = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (selectedId && selectedId !== lastFocused.current && ready) focusNode(selectedId);
    lastFocused.current = selectedId;
  }, [selectedId, ready, focusNode]);
  const searchRef = React.useRef("");
  React.useEffect(() => {
    if (!ready || !query.trim() || searchRef.current === query) { if (!query.trim()) searchRef.current = ""; return; }
    const timer = window.setTimeout(() => {
      const term = query.trim().toLowerCase(), first = nodes.find((node) => [node.hostname, node.address, node.ip, getNodeGrouping(node).cidr, getNodeGrouping(node).context].some((value) => value.toLowerCase().includes(term)));
      searchRef.current = query; if (first) focusNode(first.id);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [query, nodes, ready, focusNode]);

  function startDrag(event: React.PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || (event.target as HTMLElement).closest("[data-canvas-control], [data-topology-edge]")) return;
    const rect = event.currentTarget.getBoundingClientRect(), point = { x: event.clientX, y: event.clientY };
    pointers.current.set(event.pointerId, point); event.currentTarget.setPointerCapture(event.pointerId);
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()]; pinch.current = { distance: Math.hypot(a.x - b.x, a.y - b.y), center: { x: (a.x + b.x) / 2 - rect.left, y: (a.y + b.y) / 2 - rect.top }, camera: cameraRef.current }; drag.current = null; setDragging(true); return;
    }
    const node = (event.target as HTMLElement).closest<HTMLElement>("[data-topology-node]"), head = (event.target as HTMLElement).closest<HTMLElement>("[data-group-drag]");
    const type = node ? "node" : head ? "group" : "pan", id = node?.dataset.topologyNode ?? head?.dataset.groupDrag ?? "";
    let origin: Point = cameraRef.current;
    if (type === "node") { const group = groupMap.get(groupForNode.get(id)!)!, rect = nodeRects.get(id)!; origin = { x: rect.x - group.x, y: rect.y - group.y }; }
    else if (type === "group") origin = groupMap.get(id)!;
    drag.current = { pointer: event.pointerId, type, id, start: point, origin, moved: false, button: node as HTMLButtonElement | undefined }; setHovered(null);
  }
  function moveDrag(event: React.PointerEvent<HTMLDivElement>) {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pinch.current && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()], rect = event.currentTarget.getBoundingClientRect(), initial = pinch.current;
      const zoom = clamp(initial.camera.zoom * Math.hypot(a.x - b.x, a.y - b.y) / Math.max(1, initial.distance), .025, 2.5);
      const center = { x: (a.x + b.x) / 2 - rect.left, y: (a.y + b.y) / 2 - rect.top };
      setCamera({ zoom, x: center.x - (initial.center.x - initial.camera.x) * zoom / initial.camera.zoom, y: center.y - (initial.center.y - initial.camera.y) * zoom / initial.camera.zoom }); suppressClick.current = true; return;
    }
    const current = drag.current; if (!current || current.pointer !== event.pointerId) return;
    const dx = event.clientX - current.start.x, dy = event.clientY - current.start.y;
    if (Math.hypot(dx, dy) > 4) { current.moved = true; setDragging(true); }
    if (!current.moved) return;
    if (current.type === "pan") setCamera((value) => ({ ...value, x: current.origin.x + dx, y: current.origin.y + dy }));
    else {
      const point = { x: current.origin.x + dx / cameraRef.current.zoom, y: current.origin.y + dy / cameraRef.current.zoom };
      if (current.type === "node") {
        const group = groupMap.get(groupForNode.get(current.id)!)!;
        point.x = clamp(point.x, GROUP_PADDING, group.width - HOST_WIDTH - GROUP_PADDING); point.y = clamp(point.y, GROUP_HEADER, group.height - HOST_HEIGHT - 18);
      }
      setPositions((value) => ({ ...value, [current.type === "node" ? "nodes" : "groups"]: { ...value[current.type === "node" ? "nodes" : "groups"], [current.id]: point } }));
    }
  }
  function stopDrag(event: React.PointerEvent<HTMLDivElement>) {
    const current = drag.current; pointers.current.delete(event.pointerId);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (current?.type === "node" && !current.moved && current.button && event.type === "pointerup") {
      const node = nodes.find((item) => item.id === current.id);
      if (node) { suppressClick.current = true; setHovered(null); onSelect(node, current.button); }
    }
    if (current?.moved) {
      suppressClick.current = true;
      if (current.type === "node") {
        const group = groupMap.get(groupForNode.get(current.id)!)!, rect = nodeRects.get(current.id)!;
        if (group.nodes.some((node) => node.id !== current.id && intersects(rect, nodeRects.get(node.id)!, 8))) setPositions((value) => ({ ...value, nodes: { ...value.nodes, [current.id]: current.origin } }));
      } else if (current.type === "group") settleGroups(new Set(collapsed), current.id);
    }
    drag.current = null; pinch.current = null; setDragging(false);
    window.setTimeout(() => { suppressClick.current = false; }, 0);
  }
  function keyboard(event: React.KeyboardEvent<HTMLElement | SVGSVGElement>) {
    if (event.target !== event.currentTarget) return;
    const delta: Record<string, Point> = { ArrowLeft: { x: 70, y: 0 }, ArrowRight: { x: -70, y: 0 }, ArrowUp: { x: 0, y: 70 }, ArrowDown: { x: 0, y: -70 } };
    if (delta[event.key]) { event.preventDefault(); setCamera((value) => ({ ...value, x: value.x + delta[event.key].x, y: value.y + delta[event.key].y })); }
    else if (event.key === "+" || event.key === "=") { event.preventDefault(); zoomAt(1.25); }
    else if (event.key === "-") { event.preventDefault(); zoomAt(.8); }
    else if (event.key === "0") { event.preventDefault(); fit(); }
  }
  const miniScale = Math.min(160 / bounds.width, 86 / bounds.height), miniX = (176 - bounds.width * miniScale) / 2 - bounds.x * miniScale, miniY = (102 - bounds.height * miniScale) / 2 - bounds.y * miniScale;
  const hoverNode = hovered ? nodes.find((node) => node.id === hovered) : null, hoverRect = hovered ? nodeRects.get(hovered) : null;
  function basisLabel(basis: GroupingBasis) { return basis === "unassigned" ? copy.unassignedBasis : copy[basis]; }
  return <div className={styles.root}>
    <div className={styles.tools}>
      <span className={styles.readout}><Network size={14} aria-hidden />{groups.length} {copy.groups}<i />{nodes.length} {copy.hosts}</span>
      <div className={styles.actions}>
        <button type="button" onClick={toggleAll} title={collapsed.size === groups.length ? copy.expand : copy.collapse}><Expand size={14} aria-hidden /><span>{collapsed.size === groups.length ? copy.expand : copy.collapse}</span></button>
        <button type="button" onClick={resetLayout} title={copy.stored}><RotateCcw size={14} aria-hidden /><span>{copy.reset}</span></button>
      </div>
    </div>
    <div className={styles.stage} ref={stage} tabIndex={0} role="region" aria-label={`${copy.canvas}. ${copy.keyboard}`} data-topology-stage data-zoom={camera.zoom.toFixed(4)} data-camera={`${camera.x.toFixed(1)},${camera.y.toFixed(1)}`} data-dragging={dragging} data-overview={overview} onKeyDown={keyboard} onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={stopDrag} onPointerCancel={stopDrag} onPointerLeave={() => setHovered(null)}>
      <div className={styles.world} style={{ transform: `translate(${camera.x}px, ${camera.y}px) scale(${camera.zoom})`, visibility: ready ? "visible" : "hidden" }}>
        {placed.filter((group) => intersects(group, viewport)).map((group) => {
          const closed = collapsed.has(group.id), dim = focusIds && !group.nodes.some((node) => focusIds.has(node.id));
          const contexts = group.contexts.map((context) => context || copy.defaultContext).join(" / ");
          return <section key={group.id} className={styles.group} data-topology-group={group.id} data-cidr={group.cidr} data-context-count={group.contexts.length} data-collapsed={closed} data-basis={group.bases.length === 1 ? group.bases[0] : "mixed"} data-dimmed={dim || undefined} style={{ left: group.x, top: group.y, width: group.width, height: group.height }} aria-label={`${group.cidr || copy.unassigned} · ${group.nodes.length} ${copy.hosts} · ${contexts}`}>
            <div className={styles.groupHead} data-group-drag={group.id}>
              <Grip size={14} aria-hidden /><div><strong title={group.cidr || copy.unassigned}>{group.cidr || copy.unassigned}</strong><span title={contexts}>{contexts}</span></div>
              <button type="button" data-canvas-control data-toggle-group={group.id} onClick={() => toggleGroup(group)} aria-label={`${closed ? copy.expandGroup : copy.collapseGroup} ${group.cidr || copy.unassigned}`} aria-expanded={!closed}>{closed ? <ChevronDown size={17} /> : <ChevronUp size={17} />}</button>
            </div>
            <div className={styles.groupMeta}><span title={group.bases.map(basisLabel).join(" / ")}>{group.bases.length === 1 ? basisLabel(group.bases[0]) : copy.mixed}</span><span>{group.nodes.length} {copy.hosts}</span></div>
            {(closed || overview) && <button type="button" className={styles.summary} data-canvas-control onClick={() => closed ? toggleGroup(group) : setCamera(fitCamera(graphBounds([group]), size.width, size.height))} aria-label={`${copy.expandGroup} ${group.cidr || copy.unassigned}`}><Server size={24} aria-hidden /><strong>{group.nodes.length}</strong><span>{copy.hosts}<small>{overview && !closed ? copy.overview : `${internalCounts.get(group.id) ?? 0} ${copy.inside}`}</small></span><ChevronDown size={16} aria-hidden /></button>}
          </section>;
        })}
        <svg className={styles.edges} aria-label={copy.known}>
          <defs>{["normal", "hot", "pending"].map((tone) => <marker key={tone} id={`${canvasId}-${tone}`} viewBox="0 0 9 8" refX="8" refY="4" markerWidth="8" markerHeight="7" orient="auto-start-reverse" markerUnits="userSpaceOnUse"><path d="M0 0 L8 4 L0 8 Z" className={styles[`marker_${tone}`]} /></marker>)}</defs>
          {routes.filter((route) => intersects(route.bounds, viewport)).map((route) => {
            const hot = route.edges.some((edge) => selectedEdges.has(edge.id) || (!!selectedId && (edge.source === selectedId || edge.target === selectedId))), dim = (!!selectedId || selectedEdges.size > 0) && !hot;
            const pending = route.edges.some((edge) => !edge.verified), tone = hot ? "hot" : pending ? "pending" : "normal";
            const label = route.edges.length > 1 ? `${route.edges.length} ${copy.relations}` : "";
            return <g key={route.id} className={styles.edgePair} data-topology-edge={route.id} data-source={route.source} data-target={route.target} data-edge-ids={JSON.stringify(route.edges.map((edge) => edge.id))} data-hot={hot || undefined} data-dimmed={dim || undefined}>
              <path className={styles.edge} d={route.path} data-pending={pending || undefined} markerEnd={`url(#${canvasId}-${tone})`} />
              <path className={styles.edgeHit} d={route.path} role="button" tabIndex={0} aria-label={`${route.edges.length} ${copy.relations} · ${pending ? copy.unverified : copy.verified}`} onClick={() => onSelectEdges(route.edges)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelectEdges(route.edges); } }}><title>{pending ? copy.unverified : copy.verified}</title></path>
              {label && <text className={styles.edgeLabel} x={route.label.x} y={route.label.y} textAnchor="middle">{label}</text>}
            </g>;
          })}
        </svg>
        {!overview && placed.filter((group) => !collapsed.has(group.id)).flatMap((group) => group.nodes.filter((node) => intersects(nodeRects.get(node.id)!, viewport) || node.id === selectedId).map((node) => {
          const rect = nodeRects.get(node.id)!, risk = node.severity.toLowerCase(), isServer = !/workstation|desktop|laptop/i.test(node.role), HostIcon = isServer ? Server : Monitor;
          const context = getNodeGrouping(node).context || copy.defaultContext;
          return <button type="button" key={node.id} className={styles.host} data-topology-node={node.id} data-risk={risk} data-dimmed={!!focusIds && !focusIds.has(node.id) || undefined} data-neighbor={!!selectedId && selectedId !== node.id && focusIds?.has(node.id) || undefined} aria-pressed={selectedId === node.id} aria-label={`${node.hostname || node.ip || node.address} · ${node.ip || node.address} · ${context} · ${node.vulnerabilities.length} ${copy.vulns} · ${node.findings.length} ${copy.findings} · ${node.credentials.length} ${copy.credentials}`} style={{ left: rect.x, top: rect.y }} onClick={(event) => { if (!suppressClick.current) { setHovered(null); onSelect(node, event.currentTarget); } }} onPointerEnter={() => { if (!dragging) setHovered(node.id); }} onPointerLeave={() => setHovered(null)} onFocus={() => setHovered(node.id)} onBlur={() => setHovered(null)}>
            <span className={styles.hostIcon}><HostIcon size={25} aria-hidden />{node.vulnerabilities.length > 0 && <span className={styles.riskBadge}>{node.vulnerabilities.length}</span>}{node.credentials.length > 0 && <span className={styles.keyBadge}><KeyRound size={12} aria-hidden /></span>}<i data-reachable={node.status === "reachable"} /></span>
            <strong>{node.hostname || node.ip || node.address}</strong><code>{node.ip || node.address}</code>
            {group.contexts.length > 1 && <small className={styles.hostContext} title={context}>{context}</small>}
          </button>;
        }))}
      </div>
      <div className={styles.status} aria-live="polite"><i /><span>{highlightIds ? highlightIds.length ? `${highlightIds.length} ${copy.matched}` : copy.none : copy.all}</span>{overview && <span> · {copy.overview}</span>}</div>
      <div className={styles.navigation} data-canvas-control>
        <button type="button" onClick={() => zoomAt(1.25)} aria-label={copy.zoomIn} title={copy.zoomIn}><Plus size={16} /></button><span>{Math.round(camera.zoom * 100)}%</span><button type="button" onClick={() => zoomAt(.8)} aria-label={copy.zoomOut} title={copy.zoomOut}><Minus size={16} /></button><i /><button type="button" onClick={fit} aria-label={copy.fit} title={copy.fit}><Focus size={17} /></button>
      </div>
      <svg className={styles.minimap} data-canvas-control viewBox="0 0 176 102" tabIndex={0} role="button" aria-label={copy.minimap} onKeyDown={keyboard} onPointerDown={(event) => { event.preventDefault(); event.stopPropagation(); const rect = event.currentTarget.getBoundingClientRect(); const x = ((event.clientX - rect.left) / rect.width * 176 - miniX) / miniScale, y = ((event.clientY - rect.top) / rect.height * 102 - miniY) / miniScale; setCamera((value) => ({ ...value, x: size.width / 2 - x * value.zoom, y: size.height / 2 - y * value.zoom })); }}>
        {placed.map((group) => <rect key={group.id} className={styles.miniGroup} x={group.x * miniScale + miniX} y={group.y * miniScale + miniY} width={group.width * miniScale} height={group.height * miniScale} rx="2" />)}
        {bundles.map((bundle) => { const source = groupMap.get(groupForNode.get(bundle.edges[0].source)!)!, target = groupMap.get(groupForNode.get(bundle.edges[0].target)!)!; if (source.id === target.id) return null; return <path key={bundle.id} className={styles.miniEdge} d={`M${(source.x + source.width / 2) * miniScale + miniX},${(source.y + source.height / 2) * miniScale + miniY} L${(target.x + target.width / 2) * miniScale + miniX},${(target.y + target.height / 2) * miniScale + miniY}`} />; })}
        <rect className={styles.miniCamera} x={-camera.x / camera.zoom * miniScale + miniX} y={-camera.y / camera.zoom * miniScale + miniY} width={size.width / camera.zoom * miniScale} height={size.height / camera.zoom * miniScale} />
      </svg>
      {hoverNode && hoverRect && !dragging && <div className={styles.tooltip} role="tooltip" style={{ left: clamp((hoverRect.x + HOST_WIDTH / 2) * camera.zoom + camera.x, 120, Math.max(120, size.width - 120)), top: clamp(hoverRect.y * camera.zoom + camera.y - 12, 118, size.height - 95) }}><strong>{hoverNode.hostname || hoverNode.address}</strong><code>{hoverNode.ip || hoverNode.address}</code><span>{getNodeGrouping(hoverNode).context || copy.defaultContext}</span><span><ShieldAlert size={12} />{hoverNode.vulnerabilities.length} {copy.vulns} · {hoverNode.findings.length} {copy.findings}</span><span><KeyRound size={12} />{hoverNode.credentials.length} {copy.credentials}</span></div>}
    </div>
    <div className={styles.footer}><span><i className={styles.solid} />{copy.verified}</span><span><i className={styles.dashed} />{copy.unverified}</span><span className={styles.hint}>{copy.drag}</span><span className={styles.touchHint}>{copy.touch}</span></div>
  </div>;
}
