// Named aliases of the generated OpenAPI types (schema.d.ts: `npm run gen:api`).

import type { components } from "./schema";

type S = components["schemas"];

export type ActionRow = S["ActionRow"];
export type ActionState = ActionRow["state"];
export type BuildView = S["BuildView"];
export type BuildStatus = BuildView["status"];
export type BuildViewList = S["BuildViewList"];
export type DomainStats = S["DomainStats"];
export type StepStats = S["StepStats"];
export type PendingWait = S["PendingWait"];
export type ResultSummary = S["ResultSummary"];
export type ReleaseView = S["ReleaseView"];
export type RevalidationRecord = S["RevalidationRecord"];
export type ChannelMove = S["ChannelMove"];
export type ChannelView = S["ChannelView"];
export type ProvenanceGraph = S["ProvenanceGraph"];
export type ProvenanceAction = S["ProvenanceAction"];
export type Whoami = S["Whoami"];

/** A driver event as streamed (interfaces.md § 10); `data` depends on `type`. */
export interface EbsEvent {
  v: 1;
  ts: string;
  build: number;
  type: string;
  action_id?: string | null;
  data: Record<string, unknown>;
}

export interface StoredEvent {
  id: number;
  event: EbsEvent;
}
