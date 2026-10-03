// The open flow document, its unsaved edits, and its validation state.
//
// A design decision worth stating: edits live here in the store, not in the
// canvas component. The canvas is a pure view over `draft`; every mutation goes
// through a named action so undo, validation and save all read the same object
// and there is no second copy to drift.

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, ApiError } from "@shared/api/client";
import type {
  Flow,
  FlowEdge,
  FlowGraph,
  FlowNode,
  FlowSummary,
  LocatePath,
  NodeCatalogue,
  NodeSpec,
  Position,
  ValidationIssue,
} from "@shared/types";

function emptyGraph(): FlowGraph {
  return { version: 1, entry: "", default_path: null, variables: {}, nodes: [], edges: [] };
}

let idCounter = 0;
function freshId(prefix: string): string {
  idCounter += 1;
  return `${prefix}_${Date.now().toString(36)}${idCounter.toString(36)}`;
}

export const useFlowStore = defineStore("flow", () => {
  const catalogue = ref<NodeCatalogue | null>(null);
  const flows = ref<FlowSummary[]>([]);
  const currentId = ref<string | null>(null);
  const currentName = ref("");
  const draft = ref<FlowGraph>(emptyGraph());
  const issues = ref<ValidationIssue[]>([]);
  const dirty = ref(false);
  const loading = ref(false);
  const saving = ref(false);
  const lastError = ref("");

  let validateTimer: number | null = null;

  const specFor = computed(() => {
    const map = new Map<string, NodeSpec>();
    for (const group of catalogue.value?.categories ?? []) {
      for (const spec of group.nodes) map.set(spec.type, spec);
    }
    return (type: string) => map.get(type);
  });

  const nodeById = computed(() => {
    const map = new Map<string, FlowNode>();
    for (const node of draft.value.nodes) map.set(node.id, node);
    return (id: string) => map.get(id);
  });

  const errors = computed(() => issues.value.filter((i) => i.severity === "error"));
  const warnings = computed(() => issues.value.filter((i) => i.severity === "warning"));

  const issuesByNode = computed(() => {
    const map = new Map<string, ValidationIssue[]>();
    for (const issue of issues.value) {
      if (!issue.node_id) continue;
      const list = map.get(issue.node_id) ?? [];
      list.push(issue);
      map.set(issue.node_id, list);
    }
    return (id: string) => map.get(id) ?? [];
  });

  /** Nodes whose declared path (or the flow default) has not been decided. */
  const undecidedPathNodes = computed(() =>
    draft.value.nodes.filter(
      (node) =>
        specFor.value(node.type)?.path_aware &&
        !node.path &&
        !draft.value.default_path &&
        !node.disabled,
    ),
  );

  async function loadCatalogue(): Promise<void> {
    if (catalogue.value) return;
    catalogue.value = await api.nodes();
  }

  async function loadFlows(): Promise<void> {
    const result = await api.flows();
    flows.value = result.flows;
  }

  async function open(flowId: string): Promise<void> {
    loading.value = true;
    lastError.value = "";
    try {
      const flow: Flow = await api.flow(flowId);
      currentId.value = flow.id;
      currentName.value = flow.name;
      // Deep copy: the editor mutates freely, and a shared reference to the
      // response object would make "unsaved" undetectable.
      draft.value = structuredClone(toGraph(flow.graph));
      issues.value = flow.issues ?? [];
      dirty.value = false;
    } catch (error) {
      lastError.value = error instanceof ApiError ? error.message : String(error);
    } finally {
      loading.value = false;
    }
  }

  /**
   * The single place a graph crossing the API boundary becomes canvas state.
   *
   * Nodes are normalised here rather than defended against at the ~8 sites
   * that read `node.position`. A graph written before `position` was required,
   * or one arriving from an external caller, has nodes without it — and the
   * backend accepts such a graph (it defaults the position to the origin). The
   * first unguarded `node.position.x` then threw during render and took the
   * whole page to a white screen, which reads as "the app is broken" rather
   * than "this graph is old".
   *
   * Nodes missing a position are laid out on a grid rather than dropped: the
   * backend put them all at (0,0), so they would otherwise stack invisibly on
   * top of each other with no way to tell them apart or grab one.
   */
  function toGraph(graph: FlowGraph): FlowGraph {
    let placed = 0;
    const nodes = (graph.nodes ?? []).map((node) => {
      if (isPosition(node?.position)) return node;
      placed += 1;
      return { ...node, position: { x: 40 + (placed % 6) * 230, y: 40 + Math.floor((placed - 1) / 6) * 120 } };
    });
    return {
      version: 1,
      entry: graph.entry ?? "",
      default_path: graph.default_path ?? null,
      description: graph.description ?? "",
      variables: graph.variables ?? {},
      nodes,
      edges: graph.edges ?? [],
    };
  }

  function isPosition(value: unknown): value is Position {
    return (
      typeof value === "object" &&
      value !== null &&
      Number.isFinite((value as Position).x) &&
      Number.isFinite((value as Position).y)
    );
  }

  // ── mutations ──

  function addNode(type: string, x: number, y: number): FlowNode | null {
    const spec = specFor.value(type);
    if (!spec) return null;
    const params: Record<string, unknown> = {};
    for (const param of spec.params) {
      if (param.default !== null && param.default !== undefined) params[param.name] = param.default;
    }
    const node: FlowNode = {
      id: freshId(spec.type.slice(0, 10)),
      type,
      name: spec.label,
      position: { x: Math.round(x), y: Math.round(y) },
      params,
      retry: { max: 0, delay: 0 },
      timeout: null,
      on_error: "fail",
      outputs: spec.outputs,
      disabled: false,
      // A path-aware node starts undecided on purpose: the designer must choose,
      // and the validator will say so until they do.
      path: null,
      target: null,
    };
    draft.value.nodes.push(node);
    if (!draft.value.entry) draft.value.entry = node.id;
    touch();
    return node;
  }

  function removeNode(nodeId: string): void {
    draft.value.nodes = draft.value.nodes.filter((n) => n.id !== nodeId);
    draft.value.edges = draft.value.edges.filter((e) => e.source !== nodeId && e.target !== nodeId);
    if (draft.value.entry === nodeId) draft.value.entry = draft.value.nodes[0]?.id ?? "";
    touch();
  }

  function moveNode(nodeId: string, x: number, y: number): void {
    const node = nodeById.value(nodeId);
    if (!node) return;
    node.position.x = Math.round(x);
    node.position.y = Math.round(y);
    touch();
  }

  function updateNode(nodeId: string, patch: Partial<FlowNode>): void {
    const node = nodeById.value(nodeId);
    if (!node) return;
    Object.assign(node, patch);
    touch();
  }

  /** The path a node will actually use — its own, or the flow default. */
  function effectivePath(node: FlowNode): LocatePath | null {
    return node.path ?? draft.value.default_path ?? null;
  }

  function setDefaultPath(path: LocatePath | null): void {
    draft.value.default_path = path;
    touch();
  }

  function setEntry(nodeId: string): void {
    draft.value.entry = nodeId;
    touch();
  }

  function connect(source: string, sourcePort: string, target: string): FlowEdge | null {
    if (source === target) return null;
    const exists = draft.value.edges.some(
      (e) => e.source === source && e.source_port === sourcePort && e.target === target,
    );
    if (exists) return null;
    const edge: FlowEdge = {
      id: freshId("e"),
      source,
      source_port: sourcePort,
      target,
      condition: null,
      label: "",
    };
    draft.value.edges.push(edge);
    touch();
    return edge;
  }

  function removeEdge(edgeId: string): void {
    draft.value.edges = draft.value.edges.filter((e) => e.id !== edgeId);
    touch();
  }

  function updateEdge(edgeId: string, patch: Partial<FlowEdge>): void {
    const edge = draft.value.edges.find((e) => e.id === edgeId);
    if (!edge) return;
    Object.assign(edge, patch);
    touch();
  }

  function setVariable(name: string, value: unknown): void {
    draft.value.variables = { ...draft.value.variables, [name]: value };
    touch();
  }

  function removeVariable(name: string): void {
    const next = { ...draft.value.variables };
    delete next[name];
    draft.value.variables = next;
    touch();
  }

  function touch(): void {
    dirty.value = true;
    scheduleValidate();
  }

  function scheduleValidate(): void {
    if (validateTimer !== null) window.clearTimeout(validateTimer);
    if (!currentId.value) return;
    const id = currentId.value;
    // Debounced: the canvas fires this on every drag frame, and a round trip per
    // frame would flood the API while the designer is still dragging.
    validateTimer = window.setTimeout(async () => {
      try {
        const result = await api.validate(id, structuredClone(draft.value));
        if (currentId.value === id) issues.value = result.issues;
      } catch {
        /* validation is advisory; a failure must not interrupt editing */
      }
    }, 400);
  }

  async function save(): Promise<boolean> {
    if (!currentId.value) return false;
    saving.value = true;
    lastError.value = "";
    try {
      const flow = await api.saveFlow(currentId.value, {
        name: currentName.value,
        graph: structuredClone(draft.value),
      });
      issues.value = flow.issues ?? [];
      dirty.value = false;
      await loadFlows();
      return true;
    } catch (error) {
      lastError.value = error instanceof ApiError ? error.message : String(error);
      return false;
    } finally {
      saving.value = false;
    }
  }

  function reset(graph: FlowGraph, name: string): void {
    draft.value = toGraph(graph);
    currentName.value = name;
    dirty.value = false;
  }

  return {
    catalogue,
    flows,
    currentId,
    currentName,
    draft,
    issues,
    dirty,
    loading,
    saving,
    lastError,
    specFor,
    nodeById,
    errors,
    warnings,
    issuesByNode,
    undecidedPathNodes,
    loadCatalogue,
    loadFlows,
    open,
    addNode,
    removeNode,
    moveNode,
    updateNode,
    effectivePath,
    setDefaultPath,
    setEntry,
    connect,
    removeEdge,
    updateEdge,
    setVariable,
    removeVariable,
    save,
    reset,
  };
});
