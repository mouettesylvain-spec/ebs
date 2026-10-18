// The log viewer's lines (R3): bytes arrive in chunks (a multi-byte character may be split
// between two), are decoded once and kept as lines; search and the first error work on them.

import { firstErrorLine } from "./signatureCluster";

export class LogLines {
  readonly lines: string[] = [""];
  private decoder = new TextDecoder("utf-8");
  bytes = 0;

  append(chunk: Uint8Array): void {
    this.bytes += chunk.length;
    this.push(this.decoder.decode(chunk, { stream: true }));
  }

  /** The log is complete: flush a trailing partial character. */
  end(): void {
    this.push(this.decoder.decode());
  }

  private push(text: string): void {
    if (!text) return;
    const parts = text.split("\n");
    this.lines[this.lines.length - 1] += parts[0]!;
    for (let i = 1; i < parts.length; i++) this.lines.push(parts[i]!);
  }

  /** Line count, without the empty line after a final newline. */
  get count(): number {
    const n = this.lines.length;
    return n > 1 && this.lines[n - 1] === "" ? n - 1 : n;
  }

  /** Indexes of the lines containing `query` (case-insensitive). */
  search(query: string): number[] {
    if (!query) return [];
    const q = query.toLowerCase();
    const out: number[] = [];
    for (let i = 0; i < this.count; i++) {
      if (this.lines[i]!.toLowerCase().includes(q)) out.push(i);
    }
    return out;
  }

  /** The index of the first error line (as the Questa transcript parser picks it), or -1. */
  firstError(): number {
    for (let i = 0; i < this.count; i++) {
      if (firstErrorLine(this.lines[i]!) !== null) return i;
    }
    return -1;
  }
}

/** The rows a virtualized list draws: those in the viewport plus `overscan` on each side. */
export function visibleRange(
  scrollTop: number,
  viewport: number,
  rowHeight: number,
  count: number,
  overscan = 20,
): [number, number] {
  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const last = Math.min(count, Math.ceil((scrollTop + viewport) / rowHeight) + overscan);
  return [first, Math.max(first, last)];
}

/** A live log shorter than what was already read was restarted (the runner truncates it on a
 * retry): read it again from the start. */
export function restarted(bytesRead: number, size: number): boolean {
  return size < bytesRead;
}
