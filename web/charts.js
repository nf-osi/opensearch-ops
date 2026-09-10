// Chart kit for the benchmark dashboard — plain SVG, no dependencies (same constraint as
// the rest of web/: it has to run as static files off GitHub Pages).
//
// Five forms, each picked for the job its data does:
//   hbar       magnitude across recipes (one measure, emphasis on the two roles)
//   dumbbell   two-point comparison (platform default -> best recipe tested)
//   scatter    two measures per recipe (quality vs latency) + the trade-off frontier
//   rankstack  where the first correct result landed, as ordered rank bands
//   heatmap    every case x every recipe, rank as magnitude
//
// House rules baked in here so callers can't get them wrong: marks are thin (<=24px)
// with a 4px rounded data-end and a square baseline, gridlines are hairline and
// recessive, adjacent fills are separated by a 2px gap in the surface colour (never a
// stroke), dots carry a 2px surface ring, every mark has a hover AND focus tooltip whose
// hit target is larger than the mark, and text never wears a series colour. Colours come
// from CSS custom properties (style.css) — nothing here references a raw hex.
//
// Colour roles: a recipe's colour follows its ROLE (production default / best tested /
// everything else), never its row position, so re-sorting the leaderboard never repaints
// a recipe. Rank bands use the validated ordinal blue ramp; "missed" is neutral grey,
// not a status colour.

const NS = "http://www.w3.org/2000/svg";

/* Three reference points, not two. BASELINE is the platform default — the absolute
   control, the same query on every index. PRODUCTION is what this index's portal page
   actually sends today — the contextual control, present only where the portal ships a
   SearchQueryConfig. BEST is whatever won the run, which may or may not be the recipe
   production already implements. */
export const ROLE = { BASELINE: "baseline", PRODUCTION: "production", BEST: "best", OTHER: "other" };
/** Class for a category label that names a reference recipe, so the label carries the same
 *  colour as its mark. Only the two controls are tinted — tinting every row would make the
 *  colour meaningless. Used by hbar, rankstack and heatmap so all three agree. */
/** The role a row's LABEL should carry, which is not always its mark's role: a deployed
 *  arm that also won the run takes the best fill, and would otherwise lose the tint that
 *  says it is what production serves. Callers pass `labelRole` to keep the two apart. */
export function roleCls(role, base = "viz-cat") {
  if (role === ROLE.BASELINE) return `${base} is-baseline`;
  if (role === ROLE.PRODUCTION) return `${base} is-production`;
  return base;
}

const ROLE_FILL = {
  [ROLE.BASELINE]: "var(--series-2)",
  [ROLE.PRODUCTION]: "var(--series-3)",
  [ROLE.BEST]: "var(--series-1)",
  [ROLE.OTHER]: "var(--muted-mark)",
};

// Rank bands, best-first. Mirrors rank_bucket() in build_site.py — keep the two in step.
export const RANK_BANDS = [
  { key: "top", label: "Position 1", fill: "var(--rank-top)", ink: "#fff" },
  { key: "near", label: "Positions 2–3", fill: "var(--rank-near)", ink: "#fff" },
  { key: "deep", label: "Positions 4–{k}", fill: "var(--rank-deep)", ink: "var(--ink)" },
  { key: "miss", label: "Not in top {k}", fill: "var(--rank-miss)", ink: "var(--ink)" },
];

/** Which band a first-relevant rank falls in. A rank past k is a miss: the correct
 *  answer exists but sits off the page the run is scored on. */
export function rankBucket(rank, k) {
  if (!rank || rank > k) return "miss";
  if (rank === 1) return "top";
  if (rank <= 3) return "near";
  return "deep";
}

// ------------------------------------------------------------------ small helpers
const svgEl = (tag, attrs = {}) => {
  const n = document.createElementNS(NS, tag);
  for (const [key, v] of Object.entries(attrs)) if (v != null) n.setAttribute(key, v);
  return n;
};
const txt = (x, y, s, cls, extra = {}) => {
  const n = svgEl("text", { x, y, class: cls, ...extra });
  n.textContent = s;                       // labels are untrusted data — never innerHTML
  return n;
};
// Measure text the way the browser will: SVG text doesn't clip, so a label that is too
// long for its gutter spills across the plot instead of being cut off. Everything that
// puts a data label in a fixed-width gutter trims through fitLabel first, and keeps the
// full string on a <title> (plus the figure's table view).
let measureCtx = null;
const CAT_FONT = "500 12.5px 'Public Sans', sans-serif";
function textWidth(s, font) {
  if (measureCtx === null) {
    try { measureCtx = document.createElement("canvas").getContext("2d") || false; } catch { measureCtx = false; }
  }
  if (!measureCtx) return String(s).length * 6.6;        // no canvas (e.g. a test shim)
  measureCtx.font = font;
  return measureCtx.measureText(String(s)).width;
}
function fitLabel(s, maxPx, font = CAT_FONT) {
  s = String(s ?? "");
  if (!(maxPx > 0) || textWidth(s, font) <= maxPx) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (textWidth(`${s.slice(0, mid)}\u2026`, font) <= maxPx) lo = mid; else hi = mid - 1;
  }
  return `${s.slice(0, lo).trimEnd()}\u2026`;
}
/** A category label in a gutter: trimmed to fit, full text on hover. */
function catLabel(x, y, s, maxPx, cls = "viz-cat", font = CAT_FONT, anchor = "end") {
  const shown = fitLabel(s, maxPx, font);
  const n = txt(x, y, shown, cls, anchor ? { "text-anchor": anchor } : {});
  if (shown !== String(s)) {
    const t = svgEl("title");
    t.textContent = String(s);
    n.appendChild(t);
  }
  return n;
}

export const fmtNum = (v, dp = 3) => (typeof v === "number" ? v.toFixed(dp) : "—");
export const fmtMs = (v) => (typeof v === "number" ? `${Math.round(v)} ms` : "—");

/** Clean axis ticks spanning [0, max] — 0/0.25/0.5… rather than 0/0.31/0.62… */
function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0];
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let t = 0; t <= max + step / 2; t += step) out.push(Number(t.toFixed(10)));
  return out;
}

/** A bar with a 4px rounded data-end and a square baseline (never a fully rounded rect:
 *  the baseline end must read as anchored). `w` may be < r on tiny values. */
function barPath(x, y, w, h, r = 4) {
  const rr = Math.max(0, Math.min(r, w));
  return `M${x},${y}h${w - rr}a${rr},${rr} 0 0 1 ${rr},${rr}v${h - 2 * rr}` +
         `a${rr},${rr} 0 0 1 ${-rr},${rr}h${-(w - rr)}z`;
}

// ------------------------------------------------------------------ tooltip layer
let tipNode = null;
function tip() {
  if (!tipNode) {
    tipNode = document.createElement("div");
    tipNode.className = "viz-tip";
    tipNode.setAttribute("role", "status");
    document.body.appendChild(tipNode);
  }
  return tipNode;
}
/** rows: [{key: cssColorOrNull, value: string, label: string}] — value leads (strong),
 *  label follows, keyed by a short stroke of the mark's colour. */
function showTip(target, title, rows) {
  const t = tip();
  t.replaceChildren();
  if (title) {
    const h = document.createElement("p");
    h.className = "viz-tip-title";
    h.textContent = title;
    t.appendChild(h);
  }
  for (const r of rows) {
    const line = document.createElement("p");
    line.className = "viz-tip-row";
    if (r.key) {
      const k = document.createElement("span");
      k.className = "viz-tip-key";
      k.style.background = r.key;
      line.appendChild(k);
    }
    const v = document.createElement("b");
    v.textContent = r.value;
    line.appendChild(v);
    if (r.label) {
      const l = document.createElement("span");
      l.textContent = r.label;
      line.appendChild(l);
    }
    t.appendChild(line);
  }
  const box = target.getBoundingClientRect();
  t.classList.add("is-on");
  const w = t.offsetWidth, h = t.offsetHeight;
  const left = Math.min(Math.max(8, box.left + box.width / 2 - w / 2), window.innerWidth - w - 8);
  const above = box.top - h - 10;
  t.style.left = `${left}px`;
  t.style.top = `${above > 8 ? above : box.bottom + 10}px`;
}
const hideTip = () => tipNode?.classList.remove("is-on");

/** Wire hover + keyboard focus to the same readout, and make the mark respond. */
function interactive(node, title, rows) {
  node.classList.add("viz-mark");
  node.setAttribute("tabindex", "0");
  node.setAttribute("role", "img");
  node.setAttribute("aria-label", `${title}: ${rows.map((r) => `${r.value} ${r.label}`).join(", ")}`);
  const on = () => showTip(node, title, rows);
  node.addEventListener("pointerenter", on);
  node.addEventListener("focus", on);
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("blur", hideTip);
}

/** Mount an <svg> sized to its host, re-rendering on resize so labels never collide at
 *  a width they weren't measured for. Returns the draw handle. */
function mount(host, draw, { height, minWidth = 320 }) {
  host.replaceChildren();
  const paint = () => {
    const w = Math.max(minWidth, host.clientWidth || minWidth);
    const svg = svgEl("svg", { class: "viz", width: w, height, viewBox: `0 0 ${w} ${height}` });
    draw(svg, w, height);
    host.replaceChildren(svg);
  };
  paint();
  if (!host.dataset.vizObserved) {
    host.dataset.vizObserved = "1";
    let raf = 0;
    new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => host.__vizPaint?.());
    }).observe(host);
  }
  host.__vizPaint = paint;
}

// ------------------------------------------------------------------ legend & twin
/** A legend is always present for two or more series. items: [{fill, label}] */
export function legend(items, note) {
  const wrap = document.createElement("div");
  wrap.className = "viz-legend";
  for (const it of items) {
    const chip = document.createElement("span");
    chip.className = "viz-legend-item";
    const sw = document.createElement("span");
    sw.className = "viz-swatch";
    sw.style.background = it.fill;
    const lb = document.createElement("span");
    lb.textContent = it.label;
    chip.append(sw, lb);
    wrap.appendChild(chip);
  }
  if (note) {
    const n = document.createElement("span");
    n.className = "viz-legend-note";
    n.textContent = note;
    wrap.appendChild(n);
  }
  return wrap;
}

/** The table view every figure carries, so no value is gated behind a hover. */
export function tableTwin({ summary, head, rows, align = [] }) {
  const d = document.createElement("details");
  d.className = "viz-twin";
  const s = document.createElement("summary");
  s.textContent = summary || "Table view";
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  head.forEach((h, i) => {
    const th = document.createElement("th");
    th.textContent = h;
    if (align[i] === "num") th.className = "num";
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    row.forEach((cell, i) => {
      const td = document.createElement("td");
      td.textContent = cell == null ? "—" : String(cell);
      if (align[i] === "num") td.className = "num";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.append(thead, tbody);
  const scroll = document.createElement("div");
  scroll.className = "viz-twin-scroll";
  scroll.appendChild(table);
  d.append(s, scroll);
  return d;
}

/** The glossary twin: the same disclosure affordance as tableTwin, for the short-hand
 *  labels a figure uses. items: [{term, tag, definition, note, role}] — `role` optionally
 *  tags a term as the control or the winner so the list ties back to the legend. */
export function defsTwin({ summary, items }) {
  const d = document.createElement("details");
  d.className = "viz-twin viz-defs";
  const s = document.createElement("summary");
  s.textContent = summary || "What these names mean";
  const scroll = document.createElement("div");
  scroll.className = "viz-twin-scroll";
  const dl = document.createElement("dl");
  for (const it of items) {
    const dt = document.createElement("dt");
    dt.textContent = it.term;
    if (it.tag) {
      const chip = document.createElement("span");
      chip.className = "defs-tag";
      chip.textContent = it.tag;
      dt.appendChild(chip);
    }
    for (const role of it.roles || (it.role ? [it.role] : [])) {
      const r = document.createElement("span");
      r.className = `defs-role defs-role-${role}`;
      // `promoted` is badge-only: no mark wears it (see bench.js), it says the row is
      // what the index actually serves today
      r.textContent = { baseline: "platform default", production: "portal today",
                        promoted: "in production", superseded: "pre-promotion",
                        best: "best" }[role] || role;
      dt.appendChild(r);
    }
    const dd = document.createElement("dd");
    dd.textContent = it.definition || "";
    if (it.note) {
      const n = document.createElement("span");
      n.className = "defs-note";
      n.textContent = it.note;
      dd.appendChild(n);
    }
    dl.append(dt, dd);
  }
  scroll.appendChild(dl);
  d.append(s, scroll);
  return d;
}

// ------------------------------------------------------------------ hbar
/** One measure across recipes. rows: [{label, value, role, sub}]. Single series, so no
 *  legend box — emphasis carries the two roles and every bar is directly labelled. */
export function hbar(host, { rows, max, fmt = (v) => fmtNum(v), unit = "" }) {
  const ROW = 30, BAR = 14, PAD_T = 8, PAD_B = 22;
  const height = PAD_T + rows.length * ROW + PAD_B;
  const top = Math.max(max ?? Math.max(...rows.map((r) => r.value || 0)), 1e-9);
  mount(host, (svg, w) => {
    const labelW = Math.min(190, Math.max(120, w * 0.3));
    const plotW = w - labelW - 54;
    const ticks = niceTicks(top);
    for (const t of ticks) {                                   // recessive hairline grid
      const x = labelW + (t / top) * plotW;
      svg.appendChild(svgEl("line", { x1: x, y1: PAD_T, x2: x, y2: PAD_T + rows.length * ROW, class: "viz-grid" }));
      svg.appendChild(txt(x, height - 6, String(t), "viz-tick", { "text-anchor": "middle" }));
    }
    rows.forEach((r, i) => {
      const y = PAD_T + i * ROW + (ROW - BAR) / 2;
      const bw = Math.max(1, ((r.value || 0) / top) * plotW);
      const bar = svgEl("path", { d: barPath(labelW, y, bw, BAR), fill: ROLE_FILL[r.role] || ROLE_FILL.other });
      interactive(bar, r.label, [{ key: ROLE_FILL[r.role], value: fmt(r.value), label: unit }]);
      svg.appendChild(bar);
      svg.appendChild(catLabel(labelW - 10, y + BAR - 2, r.label, labelW - 16,
                               roleCls(r.labelRole ?? r.role)));
      svg.appendChild(txt(labelW + bw + 8, y + BAR - 2, fmt(r.value), "viz-val"));
    });
  }, { height });
}

// ------------------------------------------------------------------ dumbbell
/** Two-point comparison per row: `from` (production default) -> `to` (best tested). */
export function dumbbell(host, { rows, fmt = (v) => fmtNum(v), fromLabel, toLabel, max }) {
  const ROW = 34, PAD_T = 10, PAD_B = 24, R = 5;
  const height = PAD_T + rows.length * ROW + PAD_B;
  const top = Math.max(max ?? Math.max(...rows.flatMap((r) => [r.from || 0, r.to || 0])), 1e-9);
  mount(host, (svg, w) => {
    const labelW = Math.min(200, Math.max(120, w * 0.3));
    const plotW = w - labelW - 62;
    const at = (v) => labelW + ((v || 0) / top) * plotW;
    for (const t of niceTicks(top)) {
      const x = at(t);
      svg.appendChild(svgEl("line", { x1: x, y1: PAD_T, x2: x, y2: PAD_T + rows.length * ROW, class: "viz-grid" }));
      svg.appendChild(txt(x, height - 6, String(t), "viz-tick", { "text-anchor": "middle" }));
    }
    rows.forEach((r, i) => {
      const y = PAD_T + i * ROW + ROW / 2;
      const [x1, x2] = [at(r.from), at(r.to)];
      svg.appendChild(svgEl("line", { x1, y1: y, x2, y2: y, class: "viz-connect" }));
      svg.appendChild(catLabel(labelW - 10, y + 4, r.label, labelW - 16));
      const dot = (x, role, label) => {
        const c = svgEl("circle", { cx: x, cy: y, r: R, fill: ROLE_FILL[role], class: "viz-dot" });
        const hit = svgEl("circle", { cx: x, cy: y, r: 14, fill: "transparent" });  // >=24px target
        interactive(hit, r.label, [
          { key: ROLE_FILL[ROLE.BASELINE], value: fmt(r.from), label: fromLabel },
          { key: ROLE_FILL[ROLE.BEST], value: fmt(r.to), label: toLabel },
        ]);
        hit.setAttribute("aria-label", `${r.label}, ${label}: ${fmt(role === ROLE.BEST ? r.to : r.from)}`);
        svg.append(c, hit);
      };
      dot(x1, ROLE.BASELINE, fromLabel);
      dot(x2, ROLE.BEST, toLabel);
      // label only the end the eye should land on — the improved value
      svg.appendChild(txt(Math.max(x1, x2) + 12, y + 4, fmt(r.to), "viz-val"));
    });
  }, { height });
}

// ------------------------------------------------------------------ scatter
/** Quality vs latency. One point per recipe, coloured by role, every point directly
 *  labelled (the relief the contrast WARN on light hues requires). */
export function scatter(host, { points, xLabel, yLabel, fmtX = fmtMs, fmtY = (v) => fmtNum(v) }) {
  const height = 300, PAD = { t: 14, r: 18, b: 44, l: 52 };
  mount(host, (svg, w) => {
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const xMax = Math.max(...xs) * 1.12, xMin = Math.min(0, Math.min(...xs));
    const yMin = Math.max(0, Math.min(...ys) - 0.06), yMax = Math.min(1, Math.max(...ys) + 0.06);
    const px = (v) => PAD.l + ((v - xMin) / (xMax - xMin || 1)) * (w - PAD.l - PAD.r);
    const py = (v) => height - PAD.b - ((v - yMin) / (yMax - yMin || 1)) * (height - PAD.t - PAD.b);
    for (const t of niceTicks(yMax)) {
      if (t < yMin) continue;
      svg.appendChild(svgEl("line", { x1: PAD.l, y1: py(t), x2: w - PAD.r, y2: py(t), class: "viz-grid" }));
      svg.appendChild(txt(PAD.l - 8, py(t) + 4, t.toFixed(2), "viz-tick", { "text-anchor": "end" }));
    }
    for (const t of niceTicks(xMax)) {
      if (t < xMin) continue;
      svg.appendChild(txt(px(t), height - PAD.b + 16, String(Math.round(t)), "viz-tick", { "text-anchor": "middle" }));
    }
    svg.appendChild(svgEl("line", { x1: PAD.l, y1: height - PAD.b, x2: w - PAD.r, y2: height - PAD.b, class: "viz-axis" }));
    svg.appendChild(txt(w - PAD.r, height - 8, xLabel, "viz-axis-label", { "text-anchor": "end" }));
    svg.appendChild(txt(PAD.l - 8, PAD.t + 2, yLabel, "viz-axis-label"));
    for (const p of points) {
      const fill = ROLE_FILL[p.role] || ROLE_FILL.other;
      svg.appendChild(svgEl("circle", { cx: px(p.x), cy: py(p.y), r: 6, fill, class: "viz-dot" }));
      const hit = svgEl("circle", { cx: px(p.x), cy: py(p.y), r: 14, fill: "transparent" });
      interactive(hit, p.label, [
        { key: fill, value: fmtY(p.y), label: yLabel },
        { key: null, value: fmtX(p.x), label: xLabel },
      ]);
      svg.appendChild(hit);
      // keep labels inside the frame: flip past the right third, and trim to the room left
      const flip = px(p.x) > w * 0.72;
      const room = (flip ? px(p.x) - PAD.l : w - PAD.r - px(p.x)) - 14;
      svg.appendChild(catLabel(px(p.x) + (flip ? -12 : 12), py(p.y) + 4, p.label, room,
        "viz-point-label", "500 11.5px 'Public Sans', sans-serif", flip ? "end" : "start"));
    }
  }, { height });
}

// ------------------------------------------------------------------ rankstack
/** The signature view: for each recipe, where the first correct result landed, as a
 *  proportional bar of ordered rank bands. rows: [{label, role, buckets:{top,…}}] */
export function rankstack(host, { rows, k }) {
  const ROW = 34, BAR = 18, PAD_T = 6, GAP = 2;             // GAP = surface gap, not a stroke
  const height = PAD_T + rows.length * ROW + 6;
  const bands = RANK_BANDS.map((b) => ({ ...b, label: b.label.replace("{k}", String(k)) }));
  mount(host, (svg, w) => {
    const labelW = Math.min(200, Math.max(120, w * 0.3));
    const plotW = w - labelW - 44;
    rows.forEach((r, i) => {
      const y = PAD_T + i * ROW + (ROW - BAR) / 2;
      const total = bands.reduce((s, b) => s + (r.buckets[b.key] || 0), 0) || 1;
      let x = labelW;
      const drawn = bands.map((b) => ({ b, n: r.buckets[b.key] || 0 })).filter((s) => s.n > 0);
      drawn.forEach((seg, j) => {
        const full = (seg.n / total) * plotW;
        const last = j === drawn.length - 1;
        const wSeg = Math.max(1, full - (last ? 0 : GAP));
        const d = last ? barPath(x, y, wSeg, BAR) : `M${x},${y}h${wSeg}v${BAR}h${-wSeg}z`;
        const path = svgEl("path", { d, fill: seg.b.fill });
        const pct = Math.round((seg.n / total) * 100);
        interactive(path, r.label, [{ key: seg.b.fill, value: `${seg.n} of ${total}`, label: `${seg.b.label} · ${pct}%` }]);
        svg.appendChild(path);
        // interior segments have no free end — label inline only when the count fits
        if (wSeg > 26) {
          svg.appendChild(txt(x + wSeg / 2, y + BAR - 5, String(seg.n), "viz-inseg",
            { "text-anchor": "middle", fill: seg.b.ink }));
        }
        x += full;
      });
      svg.appendChild(catLabel(labelW - 10, y + BAR - 5, r.label, labelW - 16,
                               roleCls(r.labelRole ?? r.role)));
    });
  }, { height });
  return legend(bands.map((b) => ({ fill: b.fill, label: b.label })));
}

// ------------------------------------------------------------------ heatmap
/** Every case x every recipe; the cell carries the rank of the first correct result as
 *  magnitude (dark = near the top). cells(rowKey, colKey) -> rank|null. */
export function heatmap(host, { rowLabels, rowKeys, colLabels, colKeys, colRoles,
                                colLabelRoles, cell, k }) {
  const CELL = 22, GAP = 2, HEAD = 92, PAD_L = 8;
  const height = HEAD + rowKeys.length * CELL + 8;
  const fillFor = (rank) => `var(--rank-${rankBucket(rank, k)})`;
  mount(host, (svg, w) => {
    const labelW = Math.min(260, Math.max(150, w * 0.34));
    const colW = Math.max(CELL, Math.min(46, (w - labelW - PAD_L) / colKeys.length));
    colKeys.forEach((ck, j) => {
      const x = labelW + j * colW + colW / 2;
      const t = catLabel(x, HEAD - 12, colLabels[j], HEAD - 14,
        roleCls(colLabelRoles?.[j] ?? colRoles?.[j], "viz-colhead"),
        "500 11px 'Public Sans', sans-serif", "start");
      t.setAttribute("transform", `rotate(-52 ${x} ${HEAD - 12})`);
      svg.appendChild(t);
    });
    rowKeys.forEach((rk, i) => {
      const y = HEAD + i * CELL;
      svg.appendChild(catLabel(labelW - 10, y + CELL - 7, rowLabels[i], labelW - 16));
      colKeys.forEach((ck, j) => {
        const rank = cell(rk, ck);
        const x = labelW + j * colW;
        const box = svgEl("rect", {
          x, y, width: colW - GAP, height: CELL - GAP, rx: 2,
          fill: rank == null ? "var(--rank-absent)" : fillFor(rank),
        });
        interactive(box, rowLabels[i], [{
          key: rank == null ? "var(--rank-absent)" : fillFor(rank),
          value: rank == null ? "not run" : rank <= k ? `position ${rank}` : `position ${rank} — below top ${k}`,
          label: colLabels[j],
        }]);
        svg.appendChild(box);
      });
    });
  }, { height, minWidth: 480 });
  return legend([
    ...RANK_BANDS.map((b) => ({ fill: b.fill, label: b.label.replace("{k}", String(k)) })),
    { fill: "var(--rank-absent)", label: "Not in this run" },
  ]);
}
