// Live execution trace, fed by the SSE stream.
//
// Spans arrive twice per attempt: `phase: "started"` and `phase: "finished"`,
// both carrying the same `node_id`/`attempt`. They are merged by that key rather
// than appended, so a node appears the instant it starts and fills in when it
// finishes — which is what makes a run stuck on a permission prompt visible
// instead of silent.

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api, subscribeTrace } from "../api/client";
import type { Run, Span, SpanStatus } from "../types";

export interface TraceNode {
  key: string;
  nodeId: string;
  nodeType: string;
  name: string;
  attempt: number;
  status: SpanStatus;
  startedAt: number;
  endedAt: number | null;
  durationMs: number | null;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  error: string | null;
  /** True while the node has started but not settled. */
  pending: boolean;
}

export const useTraceStore = defineStore("trace", () => {
  const runId = ref<string | null>(null);
  const run = ref<Run | null>(null);
  const nodes = ref<TraceNode[]>([]);
  const connected = ref(false);
  const live = computed(() => run.value?.status === "running");

  let unsubscribe: (() => void) | null = null;

  function keyFor(nodeId: string, attempt: number): string {
    return `${nodeId}#${attempt}`;
  }

  function merge(span: Partial<Span> & { phase?: string }): void {
    if (!span.node_id) return;
    const key = keyFor(span.node_id, span.attempt ?? 1);
    const existing = nodes.value.find((n) => n.key === key);
    if (existing) {
      Object.assign(existing, {
        status: span.status ?? existing.status,
        endedAt: span.ended_at ?? existing.endedAt,
        durationMs: span.duration_ms ?? existing.durationMs,
        inputs: span.inputs ?? existing.inputs,
        outputs: span.outputs ?? existing.outputs,
        error: span.error ?? existing.error,
        pending: span.phase !== "finished",
      });
    } else {
      nodes.value.push({
        key,
        nodeId: span.node_id,
        nodeType: span.node_type ?? "",
        name: span.name ?? span.node_id,
        attempt: span.attempt ?? 1,
        status: span.status ?? "running",
        startedAt: span.started_at ?? Date.now() / 1000,
        endedAt: span.ended_at ?? null,
        durationMs: span.duration_ms ?? null,
        inputs: span.inputs ?? {},
        outputs: span.outputs ?? {},
        error: span.error ?? null,
        pending: span.phase !== "finished",
      });
    }
  }

  function handleEvent(event: unknown): void {
    const payload = event as Record<string, unknown>;
    if (payload.type === "span") {
      merge(payload as never);
      return;
    }
    if (payload.type === "run.finished") {
      void pollRun();
    }
  }

  async function pollRun(): Promise<void> {
    if (!runId.value) return;
    try {
      run.value = await api.run_detail(runId.value);
    } catch {
      /* the stream will deliver the next event */
    }
  }

  async function attach(targetRunId: string): Promise<void> {
    detach();
    runId.value = targetRunId;
    nodes.value = [];
    run.value = null;
    // Seed from history first so a reattach to a finished run is complete, then
    // stream live events on top.
    try {
      const [detail, history] = await Promise.all([
        api.run_detail(targetRunId),
        api.spans(targetRunId),
      ]);
      run.value = detail;
      for (const span of history.spans) merge({ ...span, phase: "finished" });
    } catch {
      /* an unknown run id just means an empty trace */
    }
    unsubscribe = subscribeTrace(targetRunId, handleEvent, () => {
      connected.value = false;
      void pollRun();
    });
    connected.value = true;
  }

  function detach(): void {
    unsubscribe?.();
    unsubscribe = null;
    connected.value = false;
  }

  function reset(): void {
    detach();
    runId.value = null;
    run.value = null;
    nodes.value = [];
  }

  const failed = computed(() => nodes.value.find((n) => n.status === "error"));
  const totalMs = computed(() => nodes.value.reduce((sum, n) => sum + (n.durationMs ?? 0), 0));

  return {
    runId,
    run,
    nodes,
    connected,
    live,
    failed,
    totalMs,
    attach,
    detach,
    reset,
  };
});
