export interface DiffLine {
  type: "same" | "add" | "del";
  text: string;
}

/** Line diff (longest common subsequence). Config files are small; this is quadratic. */
export function lineDiff(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  const rows = a.length + 1;
  const cols = b.length + 1;
  const lcs = new Uint32Array(rows * cols);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i * cols + j] =
        a[i] === b[j] ? lcs[(i + 1) * cols + j + 1]! + 1 : Math.max(lcs[(i + 1) * cols + j]!, lcs[i * cols + j + 1]!);
    }
  }
  const lines: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      lines.push({ type: "same", text: a[i]! });
      i++;
      j++;
    } else if (lcs[(i + 1) * cols + j]! >= lcs[i * cols + j + 1]!) {
      lines.push({ type: "del", text: a[i++]! });
    } else {
      lines.push({ type: "add", text: b[j++]! });
    }
  }
  while (i < a.length) lines.push({ type: "del", text: a[i++]! });
  while (j < b.length) lines.push({ type: "add", text: b[j++]! });
  return lines;
}

export type DiffRow = DiffLine | { type: "skip"; count: number };

/** Keep the changes with a few lines around them; long unchanged stretches become one row. */
export function collapseUnchanged(lines: DiffLine[], context = 3): DiffRow[] {
  const keep = new Array<boolean>(lines.length).fill(false);
  lines.forEach((line, index) => {
    if (line.type === "same") return;
    for (let i = Math.max(0, index - context); i <= Math.min(lines.length - 1, index + context); i++) keep[i] = true;
  });
  const rows: DiffRow[] = [];
  let skipped = 0;
  lines.forEach((line, index) => {
    if (keep[index]) {
      if (skipped) rows.push({ type: "skip", count: skipped });
      skipped = 0;
      rows.push(line);
    } else {
      skipped += 1;
    }
  });
  if (skipped) rows.push({ type: "skip", count: skipped });
  return rows;
}
