// Failure signatures (R2): the first error line of a log, normalized so that failures differing
// only in paths, numbers or hex values cluster together. A port of
// ebs.rules.questa.transcript (parse_transcript's first error, error_signature): keep in sync,
// signatureCluster.test.ts runs the same cases as the Python tests.

export const MAX_LINE = 512;
export const NO_SIGNATURE = "(no error line recorded)";

const PREFIX = /^#\s?/;
const UVM_SUMMARY = /^UVM_(ERROR|FATAL)\s*:\s*(\d+)\s*$/;
const UVM_REPORT = /^UVM_(ERROR|FATAL)\s+[^\s:]/;
const ERROR = /^\*\* Error\b/;
const FATAL = /^\*\* Fatal\b/;

const PATH = /[\w.$~+-]*(?:\/[\w.$~+-]*)+/g;
const HEX = new RegExp(
  String.raw`\b0[xX][0-9a-fA-F_]+\b` +
    String.raw`|(?<![\w'])\d*'[sS]?` +
    String.raw`(?:[hH][0-9a-fA-F_xXzZ?]+|[bB][01_xXzZ?]+|[oO][0-7_xXzZ?]+|[dD][0-9_xXzZ?]+)(?!\w)`,
  "g",
);
const MESSAGE_ID = /(\([a-z]+-\d+\))/; // (vlog-1001): kept, it names the diagnostic
const NUMBER = /(?<![\w<])\d+(?:\.\d+)?/g;
const SPACE = /\s+/g;

function strip(line: string): string {
  return line.replace(/[\r\n]+$/, "").replace(PREFIX, "").trim();
}

/** The first UVM error/fatal report or `** Error` / `** Fatal` line of `log`, or null. */
export function firstErrorLine(log: string): string | null {
  for (const raw of log.split("\n")) {
    const line = strip(raw);
    if (!line || UVM_SUMMARY.test(line)) continue;
    if (UVM_REPORT.test(line) || ERROR.test(line) || FATAL.test(line)) {
      return line.slice(0, MAX_LINE);
    }
  }
  return null;
}

/** `line` with paths, hex/based literals and numbers replaced by <path>, <hex>, <n>. */
export function errorSignature(line: string): string {
  const parts = strip(line).split(MESSAGE_ID);
  for (let i = 0; i < parts.length; i += 2) {
    let text = parts[i]!.replace(PATH, (m) => (m.replace(/\//g, "") === "" ? m : "<path>"));
    text = text.replace(HEX, "<hex>");
    parts[i] = text.replace(NUMBER, "<n>");
  }
  return parts.join("").replace(SPACE, " ").trim().slice(0, MAX_LINE);
}

export interface Failure {
  action_id: string;
  signature?: string | null; // recorded by the rule (ResultManifest.summary.signature)
  firstError?: string | null; // else normalized here
}

export interface Cluster {
  signature: string;
  actions: string[];
}

/** Failures grouped by signature: the biggest cluster first, then by signature. */
export function clusterFailures(failures: Failure[]): Cluster[] {
  const by = new Map<string, string[]>();
  for (const f of failures) {
    const sig = f.signature || (f.firstError ? errorSignature(f.firstError) : NO_SIGNATURE);
    const list = by.get(sig) ?? [];
    list.push(f.action_id);
    by.set(sig, list);
  }
  return [...by.entries()]
    .map(([signature, actions]) => ({ signature, actions: actions.sort() }))
    .sort((x, y) => {
      if ((x.signature === NO_SIGNATURE) !== (y.signature === NO_SIGNATURE)) {
        return x.signature === NO_SIGNATURE ? 1 : -1;
      }
      return y.actions.length - x.actions.length || (x.signature < y.signature ? -1 : 1);
    });
}
