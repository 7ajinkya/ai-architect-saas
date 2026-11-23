/**
 * Pure adapter: DesignStages -> renderable per-stage pipeline state.
 *
 * Read-and-render only: every state shown here is derived from fields the
 * backend already serves (`DesignStages` artifact presence, `design_status`,
 * the requirements status, and whether a clarification is pending).
 * Nothing is inferred from elapsed time, and no new stage semantics are
 * introduced.
 *
 * Derivation rule (documented so the mapping stays honest):
 * - a stage with its artifact present is "complete" (critique and
 *   quality gate use the stored summaries the backend serves);
 * - requirements is "complete" when the requirements status is "ready",
 *   "waiting" while a clarification question is outstanding;
 * - when the design run failed, the earliest incomplete downstream stage
 *   is where the pipeline stopped ("failed"); later stages never started;
 * - when a design run exists and has not failed, the earliest incomplete
 *   downstream stage is "in progress" and later ones are "waiting";
 * - with no design run, incomplete stages are "not started".
 *
 * Deterministic and side-effect free so it can be unit tested without
 * rendering anything.
 */

import type { DesignStages } from "../types";

export type StageState =
  | "complete"
  | "in-progress"
  | "waiting"
  | "not-started"
  | "failed";

export interface PipelineStage {
  key: string;
  label: string;
  state: StageState;
}

export const STAGE_LABELS: Array<{ key: string; label: string }> = [
  { key: "requirements", label: "Requirements" },
  { key: "capacity", label: "Capacity" },
  { key: "selection", label: "Component Selection" },
  { key: "cost", label: "Cost" },
  { key: "architecture", label: "Architecture" },
  { key: "critic", label: "Design Critic" },
  { key: "quality-gate", label: "Quality Gate" },
];

export function computeStages(
  stages: DesignStages | null,
  requirementsStatus: string,
  hasPendingClarification: boolean,
): PipelineStage[] {
  const artifacts: Array<boolean> = [
    requirementsStatus === "ready",
    stages?.capacity != null,
    stages?.selection != null,
    stages?.cost != null,
    stages?.architecture != null,
    stages?.critique != null,
    stages?.quality_gate != null,
  ];
  const designStatus = stages?.design_status ?? "";
  const hasRun = stages?.has_design_run === true;

  // Index of the earliest incomplete stage.
  const firstOpen = artifacts.findIndex((present) => !present);

  return STAGE_LABELS.map(({ key, label }, index) => {
    if (artifacts[index]) {
      return { key, label, state: "complete" as StageState };
    }
    if (index === 0) {
      // Requirements has no artifact flag of its own: it is either done,
      // waiting on the user, or not started.
      return {
        key,
        label,
        state: (hasPendingClarification ? "waiting" : "not-started") as StageState,
      };
    }
    if (index !== firstOpen) {
      // Later stages are only "waiting" while a run is still alive; after
      // a terminal failure they never started and will not run.
      const waiting = hasRun && designStatus !== "failed";
      return {
        key,
        label,
        state: (waiting ? "waiting" : "not-started") as StageState,
      };
    }
    if (designStatus === "failed") {
      return { key, label, state: "failed" as StageState };
    }
    if (hasRun) {
      return { key, label, state: "in-progress" as StageState };
    }
    return { key, label, state: "not-started" as StageState };
  });
}
