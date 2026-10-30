// Página de gráficos: ranking de kWh por medidor (SVG, cor do gateway) + gráfico por grandeza (uPlot, vendorizado).
// O período vale para os dois; clicar numa coluna do ranking coloca o medidor em foco no gráfico de baixo.
import { api, toast, fmt, h } from "./app.js";

const $ = (id) => document.getElementById(id);
const META = JSON.parse($("chart-meta").textContent);
const GW = new Map(META.gateways.map((g) => [g.id, g]));
const METERS = new Map(META.meters.map((m) => [m.id, m]));
const METRICS = new Map(META.metrics.map((m) => [m.key, m]));
const MAX = META.max_meters;
const GRID = "#e5e7eb", AXIS_INK = "#6b7280", INK = "#111827", NEUTRAL = "#9ca3af";
const CHART_H = 320;
const TYPES = ["line", "area", "bars", "step"];

const state = { range: "24h", from: null, to: null, metric: "p_total", meters: [], bucket: "auto", type: "line", focus: null };
let rankingData = null;   // última resposta de /api/energy/ranking
let lastData = null;      // última resposta de /api/history
let drawnType = "line";   // tipo efetivamente desenhado (barras com muitas séries caem para linhas)
const MAX_BAR_SERIES = 8; // acima disto cada barra teria ~1 px por bucket
let chart = null;         // uPlot do gráfico por grandeza
let pickDefault = false;  // sem medidores na URL: escolhe os maiores consumidores quando o ranking chegar

// ------------------------------------------------------------------ estado <-> URL
function readUrl() {
  const p = new URLSearchParams(location.search);
  const ids = (p.get("meters") || "").split(",").map(Number).filter((n) => METERS.has(n));
  state.meters = [...new Set(ids)].slice(0, MAX);
  if (p.get("from")) { state.range = ""; state.from = p.get("from"); state.to = p.get("to"); }
  else state.range = META.ranges.includes(p.get("range")) ? p.get("range") : "24h";
  state.metric = METRICS.has(p.get("metric")) ? p.get("metric") : "p_total";
  state.bucket = allowedBuckets().includes(p.get("bucket")) ? p.get("bucket") : "auto";
  state.type = TYPES.includes(p.get("type")) ? p.get("type") : state.metric === "kwh" ? "bars" : "line";
  const f = Number(p.get("focus"));
  state.focus = state.meters.includes(f) ? f : null;
  pickDefault = !p.has("meters");
}
function writeUrl() {
  const p = new URLSearchParams();
  if (state.range) p.set("range", state.range);
  else { p.set("from", state.from); if (state.to) p.set("to", state.to); }
  p.set("metric", state.metric);
  if (state.meters.length) p.set("meters", state.meters.join(","));
  if (state.bucket !== "auto") p.set("bucket", state.bucket);
  p.set("type", state.type);
  if (state.focus) p.set("focus", String(state.focus));
  history.replaceState(null, "", `?${p}`);
}

// ------------------------------------------------------------------ cores: gateway -> tons por medidor
function mix(hex, other, k) {
  const ch = (s) => [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16)];
  const a = ch(hex), b = ch(other);
  return "#" + a.map((v, i) => Math.round(v + (b[i] - v) * k).toString(16).padStart(2, "0")).join("");
}
const gwColor = (gid) => GW.get(gid)?.color || NEUTRAL;
/** Posição do medidor entre os selecionados do mesmo gateway (ordem por id): { i, n }. */
function siblingIndex(id) {
  const m = METERS.get(id);
  if (!m) return { i: 0, n: 1 };
  const siblings = state.meters.filter((x) => METERS.get(x)?.gateway_id === m.gateway_id).sort((a, b) => a - b);
  return { i: Math.max(0, siblings.indexOf(id)), n: siblings.length };
}
/** Acima disto por gateway os tons ficam indistinguíveis: o tom volta ao início e a linha passa a ser tracejada. */
const TONES = 6;
const DASH = [7, 4];
const isDashed = (id) => siblingIndex(id).i >= TONES;
/** Tom da cor do gateway: os medidores selecionados do mesmo gateway recebem até 6 tons do escuro ao claro. */
function colorOf(id) {
  const m = METERS.get(id);
  if (!m) return NEUTRAL;
  const base = gwColor(m.gateway_id);
  const { i, n } = siblingIndex(id);
  const slots = Math.min(n, TONES);
  if (slots <= 1) return base;
  const k = -0.35 + (0.7 * (i % TONES)) / (slots - 1);
  return k < 0 ? mix(base, "#000000", -k) : mix(base, "#ffffff", k);
}

// ------------------------------------------------------------------ seleção de medidores / gateways / foco
function addMeter(id, quiet = false) {
  if (state.meters.includes(id)) return true;
  if (state.meters.length >= MAX) {
    if (!quiet) toast(`Selecione no máximo ${MAX} medidores por vez`, "info");
    return false;
  }
  state.meters.push(id);
  return true;
}
function removeMeter(id) {
  state.meters = state.meters.filter((m) => m !== id);
  if (state.focus === id) state.focus = null;
}
/** Um gateway está "marcado" quando todos os seus medidores estão selecionados. */
const metersOf = (gid) => META.meters.filter((m) => m.gateway_id === gid).map((m) => m.id);
const gatewayOn = (gid) => { const mine = metersOf(gid); return mine.length > 0 && mine.every((id) => state.meters.includes(id)); };
/** Marcar um gateway seleciona todos os seus medidores (até o limite); desmarcar tira todos. */
function toggleGateway(gid) {
  const mine = metersOf(gid);
  if (gatewayOn(gid)) mine.forEach(removeMeter);
  else {
    let refused = 0;
    for (const id of mine) if (!addMeter(id, true)) refused++;
    if (refused) toast(`${refused} medidor(es) do gateway ficaram de fora: máximo de ${MAX} por vez`, "info");
  }
  writeUrl(); syncControls(); renderTiles(); renderRanking(); loadGeneric();
}
/** Foco vindo do ranking: entra na seleção se preciso (tirando o mais antigo quando já há 8) e destaca a série. */
function setFocus(id) {
  if (state.focus === id) state.focus = null;
  else {
    if (!state.meters.includes(id)) {
      if (state.meters.length >= MAX) removeMeter(state.meters[0]);
      state.meters.push(id);
      state.focus = id;
      writeUrl(); syncControls(); renderTiles(); renderRanking(); loadGeneric();
      return;
    }
    state.focus = id;
  }
  writeUrl(); syncControls(); renderRanking(); applyFocus();
}
function applyFocus() {
  if (!chart) return;
  const idx = state.focus ? shownIds().indexOf(state.focus) : -1;
  if (idx >= 0) chart.setSeries(idx + 1, { focus: true });
  else chart.setSeries(null, { focus: false });
}
const shownIds = () => (lastData ? state.meters.filter((id) => lastData.series[String(id)]) : []);

// ------------------------------------------------------------------ controles
/** Resoluções que fazem sentido para a grandeza atual (o servidor impõe o mesmo piso: min_step_s do catálogo). */
function allowedBuckets() {
  const min = (METRICS.get(state.metric) || {}).min_step_s || 1;
  return META.buckets.filter((b) => bucketSeconds(b) >= min);
}

/** "5min" -> 300 (chaves de BUCKET_SECONDS: Ns, Nmin, Nh, Nd). */
function bucketSeconds(b) {
  if (META.bucket_seconds && META.bucket_seconds[b]) return META.bucket_seconds[b];
  const m = /^(\d+)(s|min|h|d)$/.exec(b || "");
  return m ? Number(m[1]) * { s: 1, min: 60, h: 3600, d: 86400 }[m[2]] : 0;
}

function syncBuckets() {
  const ok = allowedBuckets();
  if (state.bucket !== "auto" && !ok.includes(state.bucket)) state.bucket = "auto";
  for (const o of $("bucket").options) o.hidden = o.value !== "auto" && !ok.includes(o.value);
  $("bucket").value = state.bucket;
}

function syncControls() {
  for (const b of $("range-seg").querySelectorAll("button")) b.classList.toggle("active", b.dataset.range === state.range);
  if (!state.range) { $("from").value = toLocalInput(state.from); $("to").value = toLocalInput(state.to); }
  for (const b of $("type-seg").querySelectorAll("button")) b.classList.toggle("active", b.dataset.type === state.type);
  $("metric").value = state.metric;
  syncBuckets();
  updateLive();

  $("gw-picker").replaceChildren(...META.gateways.map((g) =>
    h("label", { class: `chip${gatewayOn(g.id) ? " on" : ""}`, dataset: { gw: g.id }, title: "marcar/desmarcar todos os medidores deste gateway" },
      h("input", { type: "checkbox", value: g.id, ...(gatewayOn(g.id) ? { checked: true } : {}) }),
      h("span", { class: "sw", style: `background:${g.color || NEUTRAL}` }), g.name)));

  const nodes = [];
  for (const g of META.gateways) {
    nodes.push(h("span", { class: "gw-label" }, g.name));
    const mine = META.meters.filter((m) => m.gateway_id === g.id);
    if (!mine.length) nodes.push(h("span", { class: "muted", style: "font-size:12px" }, "sem medidores"));
    for (const m of mine) {
      const on = state.meters.includes(m.id);
      nodes.push(h("label", { class: `chip${on ? " on" : ""}${state.focus === m.id ? " focus" : ""}`, dataset: { id: m.id }, title: m.enabled ? null : "medidor desativado" },
        h("input", { type: "checkbox", value: m.id, ...(on ? { checked: true } : {}) }),
        h("span", { class: "sw", style: `background:${on ? colorOf(m.id) : g.color || NEUTRAL};opacity:${on ? 1 : 0.45}` }),
        m.name));
    }
  }
  $("meter-picker").replaceChildren(...nodes);
}
const toLocalInput = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

$("meter-picker").addEventListener("change", (e) => {
  const input = e.target.closest("input[type=checkbox]");
  if (!input) return;
  const id = Number(input.value);
  if (input.checked) { if (!addMeter(id)) input.checked = false; } else removeMeter(id);
  writeUrl(); syncControls(); renderTiles(); renderRanking(); loadGeneric();
});
$("gw-picker").addEventListener("change", (e) => {
  const input = e.target.closest("input[type=checkbox]");
  if (input) toggleGateway(Number(input.value));
});
$("range-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]");
  if (!b) return;
  state.range = b.dataset.range; state.from = state.to = null;
  $("from").value = $("to").value = "";
  writeUrl(); syncControls(); loadAll();
});
$("btn-apply").addEventListener("click", () => {
  const f = $("from").value, t = $("to").value;
  if (!f) return toast("Informe a data/hora inicial", "info");
  const from = new Date(f), to = t ? new Date(t) : new Date();
  if (from >= to) return toast("O início deve ser anterior ao fim", "info");
  state.range = ""; state.from = from.toISOString(); state.to = t ? to.toISOString() : null;
  writeUrl(); syncControls(); loadAll();
});
$("type-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-type]");
  if (!b) return;
  state.type = b.dataset.type;
  writeUrl(); syncControls();
  if (lastData) drawGeneric(lastData);   // só redesenha: mesmos dados
});
$("metric").addEventListener("change", () => {
  state.metric = $("metric").value;
  syncBuckets();   // consumo: mínimo 5 min; grandezas do ciclo lento: mínimo 1 min
  writeUrl(); loadGeneric();
});
$("bucket").addEventListener("change", () => {
  state.bucket = $("bucket").value;
  writeUrl(); loadGeneric();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.focus && !document.querySelector("dialog[open]")) setFocus(state.focus);
});

// ------------------------------------------------------------------ carga
function periodParams() {
  const p = new URLSearchParams();
  if (state.range) p.set("range", state.range);
  else { p.set("from", state.from); if (state.to) p.set("to", state.to); }
  return p;
}
function loadAll() { loadRanking(); loadGeneric(); }

// ------------------------------------------------------------------ atualização automática
// Períodos relativos (15m, 1h, ...) mudam a cada segundo: recarrega os dois gráficos a cada 10 s, sem o texto
// "carregando…" e sem recriar o gráfico quando só os dados mudam. Pausa com a aba oculta ou o mouse sobre um
// gráfico (senão o tooltip "foge"). Período fixo (de/até) só recarrega pelo botão "atualizar".
const REFRESH_MS = 10_000;
let hovering = false;
function updateLive() {
  const el = $("live-state");
  if (!state.range) { el.className = "pill neutral"; el.textContent = "período fixo"; return; }
  el.className = `pill ${hovering ? "neutral" : "ok"}`;
  el.textContent = hovering ? "pausado" : "ao vivo · 10 s";
}
function tick() {
  if (!state.range || document.hidden || hovering) return;
  loadRanking(true); loadGeneric(true);
}
setInterval(tick, REFRESH_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
for (const id of ["generic-chart", "ranking-chart"]) {
  $(id).addEventListener("mouseenter", () => { hovering = true; updateLive(); });
  $(id).addEventListener("mouseleave", () => { hovering = false; updateLive(); });
}
$("btn-refresh").addEventListener("click", loadAll);

let rankingSeq = 0;
async function loadRanking(silent = false) {
  const seq = ++rankingSeq;
  if (!silent) $("ranking-sub").textContent = "carregando…";
  try {
    const data = await api("GET", `/api/energy/ranking?${periodParams()}`);
    if (seq !== rankingSeq) return;
    rankingData = data;
    $("ranking-sub").textContent = `kWh no período, do maior para o menor (buckets de ${bucketLabel(data.bucket)})`;
    if (pickDefault && !state.meters.length) {
      // primeira visita: mostra os maiores consumidores no gráfico de baixo
      data.meters.slice(0, 4).forEach((m) => addMeter(m.meter_id, true));
      pickDefault = false;
      writeUrl(); syncControls(); loadGeneric();
    }
    renderTiles(); renderRanking();
  } catch (err) {
    if (seq !== rankingSeq) return;
    $("ranking-sub").textContent = "";
    toast(err.message, "err");
    $("ranking-chart").replaceChildren(h("div", { class: "empty" }, "Falha ao carregar o consumo."));
  }
}

let genericSeq = 0;
async function loadGeneric(silent = false) {
  const seq = ++genericSeq;
  if (!state.meters.length) {
    lastData = null;
    $("btn-csv").disabled = true;
    $("chart-info").textContent = "";
    return showEmpty("Selecione ao menos um medidor (ou clique numa coluna do ranking).");
  }
  const p = periodParams();
  p.set("metric", state.metric);
  p.set("meter_ids", state.meters.join(","));
  if (state.bucket !== "auto") p.set("bucket", state.bucket);
  if (!silent) $("chart-info").textContent = "carregando…";
  try {
    const data = await api("GET", `/api/history?${p}`);
    if (seq !== genericSeq) return;
    const sameShape = silent && lastData && chart && lastData.metric === data.metric && lastData.step_s === data.step_s && state.type !== "bars"
      && Object.keys(lastData.series).join() === Object.keys(data.series).join() && data.t.length && chart.series.length === Object.keys(data.series).length + 1;
    lastData = data;
    $("btn-csv").disabled = false;
    $("chart-info").textContent = `${data.label}${data.unit ? ` (${data.unit})` : ""} · ${bucketLabel(data.bucket)} por ponto · ${sourceLabel(data.source)}`;
    if (sameShape) chart.setData([data.t, ...state.meters.filter((id) => data.series[String(id)]).map((id) => data.series[String(id)].v)]);
    else drawGeneric(data);
  } catch (err) {
    if (seq !== genericSeq) return;
    $("chart-info").textContent = "";
    toast(err.message, "err");
    showEmpty("Falha ao carregar.");
  }
}
const sourceLabel = (s) => ({ reading_fast: "leituras brutas (1 s)", reading_slow: "leituras brutas (30 s)", agg_fast_1min: "agregado de 1 min",
  agg_slow_1h: "agregado de 1 h", agg_energy_1h: "agregado de 1 h", agg_energy_1d: "agregado de 1 dia" }[s] || s);
const bucketLabel = (b) => ({ "1s": "1 s", "5s": "5 s", "30s": "30 s", "1min": "1 min", "5min": "5 min", "15min": "15 min", "1h": "1 h", "1d": "1 dia" }[b] || b);

// ------------------------------------------------------------------ ranking (SVG)
const svgEl = (tag, attrs = {}, ...children) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  for (const c of children) el.append(c instanceof Node ? c : String(c));
  return el;
};
const kwhDigits = (v) => (v >= 100 ? 0 : v >= 10 ? 1 : 2);
const fmtKwh = (v) => fmt(v, kwhDigits(v));
function niceStep(max, n) {
  const raw = max / n, mag = 10 ** Math.floor(Math.log10(raw || 1));
  return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
}

function renderTiles() {
  const data = rankingData;
  if (!data) return;
  $("ranking-total").textContent = data.meters.length ? `${fmtKwh(data.total_kwh)} kWh no total` : "";
  $("gw-tiles").replaceChildren(...data.gateways.map((g) =>
    h("button", { type: "button", class: `gw-tile${gatewayOn(g.gateway_id) ? " on" : ""}`, style: `border-left-color:${g.color || NEUTRAL}`,
      dataset: { gw: g.gateway_id }, title: "selecionar os medidores deste gateway no gráfico abaixo", onclick: () => toggleGateway(g.gateway_id) },
      h("span", { class: "name" }, h("span", { class: "sw", style: `background:${g.color || NEUTRAL}` }), g.name),
      h("span", { class: "kwh" }, fmtKwh(g.kwh), h("small", {}, "kWh")),
      h("span", { class: "sub" }, `${g.meters} medidor${g.meters === 1 ? "" : "es"}${data.total_kwh > 0 ? ` · ${fmt((100 * g.kwh) / data.total_kwh, 0)} %` : ""}`))));
}

function renderRanking() {
  const host = $("ranking-chart");
  const data = rankingData;
  if (!data) return;
  const rows = data.meters;
  if (!rows.length) return host.replaceChildren(h("div", { class: "empty" }, "Nenhum medidor cadastrado."));
  const W = Math.max(host.clientWidth || 800, 320);
  host.classList.toggle("has-focus", state.focus !== null);
  const max = Math.max(...rows.map((r) => r.kwh), 0);
  const step = niceStep(max || 1, 4);
  const top = max > 0 ? Math.ceil(max / step) * step : 1;
  const n = rows.length;
  const horizontal = W / n < 44;
  const svg = horizontal ? horizontalBars(rows, W, top, step) : verticalBars(rows, W, top, step);
  const tip = h("div", { class: "tip", style: "display:none" });
  host.replaceChildren(svg, tip);

  svg.addEventListener("mousemove", (e) => {
    const bar = e.target.closest("[data-id]");
    if (!bar) { tip.style.display = "none"; return; }
    const r = rows.find((x) => x.meter_id === Number(bar.dataset.id));
    const pct = data.total_kwh > 0 ? ` · ${fmt((100 * r.kwh) / data.total_kwh, 1)} % do total` : "";
    tip.replaceChildren(h("b", {}, r.name), ` · ${GW.get(r.gateway_id)?.name ?? "?"}`, h("br"), `${fmtKwh(r.kwh)} kWh${pct}`);
    tip.style.display = "";
    const box = host.getBoundingClientRect();
    const x = Math.min(e.clientX - box.left + 12, box.width - tip.offsetWidth - 4);
    tip.style.left = `${Math.max(0, x)}px`;
    tip.style.top = `${e.clientY - box.top - tip.offsetHeight - 10}px`;
  });
  svg.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  svg.addEventListener("click", (e) => {
    const bar = e.target.closest("[data-id]");
    if (bar) setFocus(Number(bar.dataset.id));
  });
}

function barPath(x, y, w, hgt, r) {
  // retângulo com os dois cantos de cima arredondados, base reta na linha zero
  r = Math.min(r, w / 2, hgt);
  return `M${x},${y + hgt} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${y + hgt} Z`;
}
const trunc = (s, n) => (s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s);

function verticalBars(rows, W, top, step) {
  const n = rows.length, slot = (W - 52) / n;
  const rotate = slot < 92;
  const mL = 44, mT = 26, mB = rotate ? 86 : 40, H = 300 + mB;
  const plotH = H - mT - mB, plotW = W - mL - 8;
  const y = (v) => mT + plotH - (v / top) * plotH;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "consumo em kWh por medidor" });
  for (let v = 0; v <= top + 1e-9; v += step) {
    svg.append(svgEl("line", { class: "grid", x1: mL, x2: mL + plotW, y1: y(v), y2: y(v) }));
    svg.append(svgEl("text", { class: "axis", x: mL - 6, y: y(v) + 4, "text-anchor": "end" }, fmt(v, step < 1 ? 1 : 0)));
  }
  const gap = Math.max(2, slot * 0.22), bw = Math.max(3, slot - gap);
  rows.forEach((r, i) => {
    const x = mL + i * slot + gap / 2;
    const hgt = Math.max(r.kwh > 0 ? 2 : 0, (r.kwh / top) * plotH);
    const cls = `bar${state.focus === r.meter_id ? " focus" : ""}`;
    const g = svgEl("g", { class: cls, "data-id": r.meter_id, fill: gwColor(r.gateway_id) });
    g.append(svgEl("title", {}, `${r.name}: ${fmtKwh(r.kwh)} kWh`));
    g.append(svgEl("rect", { x: x - gap / 2, y: mT, width: slot, height: plotH, fill: "transparent", stroke: "none" }));  // alvo de clique maior que a barra
    if (hgt > 0) g.append(svgEl("path", { d: barPath(x, y(0) - hgt, bw, hgt, 4) }));
    const label = slot < 48 ? fmt(r.kwh, 0) : fmtKwh(r.kwh);
    g.append(svgEl("text", { class: "val", x: x + bw / 2, y: y(0) - hgt - 5, "text-anchor": "middle", fill: INK, stroke: "none" }, label));
    const name = rotate
      ? svgEl("text", { class: "lbl", x: x + bw / 2, y: y(0) + 10, "text-anchor": "end", transform: `rotate(-40 ${x + bw / 2} ${y(0) + 10})`, fill: AXIS_INK }, trunc(r.name, 18))
      : svgEl("text", { class: "lbl", x: x + bw / 2, y: y(0) + 16, "text-anchor": "middle", fill: AXIS_INK }, trunc(r.name, Math.floor(slot / 6.5)));
    g.append(name);
    svg.append(g);
  });
  svg.append(svgEl("line", { class: "grid", x1: mL, x2: mL + plotW, y1: y(0), y2: y(0), stroke: AXIS_INK }));
  return svg;
}

function horizontalBars(rows, W, top, step) {
  const rowH = 26, mL = Math.min(150, Math.max(90, W * 0.3)), mR = 64, mT = 8;
  const H = mT + rows.length * rowH + 20, plotW = W - mL - mR;
  const x = (v) => mL + (v / top) * plotW;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": "consumo em kWh por medidor" });
  for (let v = 0; v <= top + 1e-9; v += step) {
    svg.append(svgEl("line", { class: "grid", x1: x(v), x2: x(v), y1: mT, y2: H - 20 }));
    svg.append(svgEl("text", { class: "axis", x: x(v), y: H - 6, "text-anchor": "middle" }, fmt(v, step < 1 ? 1 : 0)));
  }
  rows.forEach((r, i) => {
    const yTop = mT + i * rowH + 4, bh = rowH - 8;
    const w = Math.max(r.kwh > 0 ? 2 : 0, (r.kwh / top) * plotW);
    const g = svgEl("g", { class: `bar${state.focus === r.meter_id ? " focus" : ""}`, "data-id": r.meter_id, fill: gwColor(r.gateway_id) });
    g.append(svgEl("title", {}, `${r.name}: ${fmtKwh(r.kwh)} kWh`));
    g.append(svgEl("rect", { x: 0, y: yTop - 4, width: W, height: rowH, fill: "transparent", stroke: "none" }));
    g.append(svgEl("text", { class: "lbl", x: mL - 8, y: yTop + bh / 2 + 4, "text-anchor": "end", fill: AXIS_INK, stroke: "none" }, trunc(r.name, Math.floor(mL / 6.5))));
    if (w > 0) g.append(svgEl("rect", { x: mL, y: yTop, width: w, height: bh, rx: 4 }));
    g.append(svgEl("text", { class: "val", x: mL + w + 6, y: yTop + bh / 2 + 4, fill: INK, stroke: "none" }, fmtKwh(r.kwh)));
    svg.append(g);
  });
  return svg;
}

// ------------------------------------------------------------------ gráfico por grandeza (uPlot)
function showEmpty(text) {
  destroyChart();
  $("generic-chart").replaceChildren(h("div", { class: "empty" }, text));
}
function destroyChart() {
  if (chart) { chart.destroy(); chart = null; }
}

/** Geometria de uma barra (série `slot` de `n`, ponto `i`), em pixels do canvas (canvas=true) ou CSS. */
function barGeom(u, sidx, i, slot, n, stepS, canvas = true) {
  const xs = u.data[0], v = u.data[sidx][i];
  const x0 = u.valToPos(xs[0], "x", canvas), x1 = u.valToPos(xs[0] + stepS, "x", canvas);
  const groupW = Math.max(2, (x1 - x0) * 0.8), bw = Math.max(1, groupW / n - 1);
  const top0 = canvas ? u.bbox.top : 0, hgt = canvas ? u.bbox.height : u.over.clientHeight;
  const clamp = (y) => Math.min(top0 + hgt, Math.max(top0, y));
  const base = clamp(u.valToPos(0, "y", canvas));
  const y = clamp(u.valToPos(v, "y", canvas));
  const left = u.valToPos(xs[i], "x", canvas) + (x1 - x0) * 0.1 + slot * (groupW / n);
  return { left, bw, top: Math.min(y, base), height: Math.max(1, Math.abs(base - y)), y };
}

/** Barras agrupadas por bucket: cada série ocupa uma fatia do intervalo [t, t + step). */
function groupedBars(slot, n, stepS) {
  return (u, sidx, i0, i1) => {
    const ys = u.data[sidx];
    const path = new Path2D();
    for (let i = i0; i <= i1; i++) {
      if (ys[i] == null) continue;
      const g = barGeom(u, sidx, i, slot, n, stepS);
      path.rect(g.left, g.top, g.bw, g.height);
    }
    return { stroke: path, fill: path };
  };
}

/** Rótulos de valor: em barras, sobre cada barra que tiver largura para o texto; em linhas/área/degraus, em cada
 * ponto quando os pontos são esparsos o bastante para não se sobrepor, senão só o último valor de cada série. */
function drawValueLabels(u, data, ids) {
  const ctx = u.ctx, dpr = devicePixelRatio || 1;
  const digits = data.digits ?? 1, n = ids.length, xs = u.data[0];
  ctx.save();
  ctx.font = `${11 * dpr}px system-ui, sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.lineWidth = 3 * dpr;
  ctx.strokeStyle = "rgba(255,255,255,.9)";
  const xMin = u.scales.x.min, xMax = u.scales.x.max;
  // Um rótulo só é desenhado se não cobrir outro já desenhado. Os valores finais de cada série têm prioridade
  // (são os mais úteis); os intermediários entram depois, onde ainda houver espaço.
  const placed = [];
  const fits = (x, y, w) => {
    const r = { l: x - w / 2 - 2 * dpr, r: x + w / 2 + 2 * dpr, t: y - 12 * dpr, b: y + 1 * dpr };
    if (placed.some((p) => r.l < p.r && r.r > p.l && r.t < p.b && r.b > p.t)) return false;
    placed.push(r);
    return true;
  };
  const label = (text, x, y, color) => {
    if (!fits(x, y, ctx.measureText(text).width)) return;
    ctx.strokeText(text, x, y); ctx.fillStyle = color; ctx.fillText(text, x, y);
  };
  const later = [];
  for (let s = 1; s < u.series.length; s++) {
    const ser = u.series[s], ys = u.data[s];
    if (!ser.show || (state.focus && ids[s - 1] !== state.focus)) continue;
    const color = typeof ser.stroke === "function" ? ser.stroke(u, s) : ser.stroke;
    const idx = [];
    for (let i = 0; i < xs.length; i++) if (ys[i] != null && xs[i] >= xMin && xs[i] <= xMax) idx.push(i);
    if (!idx.length) continue;
    const last = idx[idx.length - 1];
    if (drawnType === "bars") {
      for (const i of idx) {
        const g = barGeom(u, s, i, s - 1, n, data.step_s);
        const text = fmt(ys[i], digits);
        if (ctx.measureText(text).width + 4 * dpr > g.bw) continue;
        const args = [text, g.left + g.bw / 2, g.top - 3 * dpr, INK];
        if (i === last) label(...args); else later.push(args);
      }
      continue;
    }
    const widest = Math.max(...idx.map((i) => ctx.measureText(fmt(ys[i], digits)).width));
    // valores intermediários só com poucas séries e pontos espaçados; com muitas séries, só o valor final
    const sparse = n <= 4 && u.bbox.width / idx.length >= widest + 8 * dpr;
    const show = sparse ? idx : [last];
    for (const i of show) {
      const x = u.valToPos(xs[i], "x", true), y = u.valToPos(ys[i], "y", true);
      const cx = Math.min(Math.max(x, u.bbox.left + widest / 2), u.bbox.left + u.bbox.width - widest / 2);
      const args = [fmt(ys[i], digits), cx, Math.max(u.bbox.top + 12 * dpr, y - 6 * dpr), color];
      if (i === last) label(...args); else later.push(args);
    }
  }
  for (const args of later) label(...args);
  ctx.restore();
}

function drawGeneric(data) {
  const body = $("generic-chart");
  const metric = METRICS.get(data.metric) || { digits: 1, unit: "" };
  const ids = state.meters.filter((id) => data.series[String(id)]);
  const t = data.t;
  if (!t.length || !ids.length) return showEmpty("Sem dados neste período para os medidores selecionados.");

  const spanS = (t[t.length - 1] - t[0]) || 1;
  const n = ids.length;
  const unit = data.unit || metric.unit || "";
  const type = state.type === "bars" && n > MAX_BAR_SERIES ? "line" : state.type;
  drawnType = type;
  const series = [
    { label: "hora", value: (u, v) => (v == null ? "" : fmtTick(v, 0, true)) },
    ...ids.map((id, i) => {
      const color = colorOf(id);
      const s = {
        label: data.series[String(id)].name, stroke: color, width: 2, points: { show: false },
        value: (u, v) => (v == null ? "–" : `${fmt(v, data.digits ?? metric.digits)} ${unit}`.trim()),
      };
      const dashed = isDashed(id);
      if (type === "area") s.fill = color + "2e";
      else if (type === "step") s.paths = uPlot.paths.stepped({ align: 1 });
      else if (type === "bars") { s.paths = groupedBars(i, n, data.step_s); s.fill = color + (dashed ? "66" : "cc"); s.width = 1; }
      if (dashed && type !== "bars") s.dash = DASH;
      return s;
    }),
  ];
  const yRange = data.range ? { range: (u, min, max) => fitRange(min, max, data.range) }
    : data.from_zero ? { range: (u, min, max) => [0, max <= 0 ? 1 : max * 1.08] } : {};
  const bars = type === "bars";
  const opts = {
    width: body.clientWidth, height: CHART_H,
    cursor: {
      drag: { x: true, y: false },
      // barras: o cursor escolhe a barra que contém o mouse (não o ponto mais próximo) e a bolinha fica no topo dela
      dataIdx: bars ? (u, sidx, hovered, xVal) => { const xs = u.data[0]; let k = 0; for (let i = 0; i < xs.length; i++) if (xs[i] <= xVal) k = i; return k; } : undefined,
      points: {
        size: 8, width: 2, fill: "#fff",
        bbox: bars ? (u, sidx) => {
          const i = u.cursor.idxs[sidx];
          if (i == null || u.data[sidx][i] == null) return { left: -10, top: -10, width: 0, height: 0 };
          const g = barGeom(u, sidx, i, sidx - 1, n, data.step_s, false);
          return { left: g.left + g.bw / 2 - 4, top: g.y - 4, width: 8, height: 8 };
        } : undefined,
      },
    },
    hooks: { draw: [(u) => drawValueLabels(u, data, ids)] },
    focus: { alpha: 0.25 },
    legend: { show: true, live: true, markers: { dash: (u, i) => (u.series[i].dash ? "dashed" : "solid") } },
    // barras ocupam [t, t + step): o eixo precisa ir até o fim do último bucket, senão a última barra é cortada
    scales: { x: bars ? { time: true, range: [t[0], t[t.length - 1] + (data.step_s || 0)] } : { time: true }, y: yRange },
    axes: [
      { stroke: AXIS_INK, grid: { stroke: GRID, width: 1 }, ticks: { stroke: GRID, width: 1 }, font: "11px system-ui", space: 70,
        values: (u, splits) => splits.map((v) => fmtTick(v, spanS)) },
      { stroke: AXIS_INK, grid: { stroke: GRID, width: 1 }, ticks: { show: false }, font: "11px system-ui", size: 62,
        values: (u, splits) => splits.map((v) => fmt(v, axisDigits(splits, data.digits ?? metric.digits))) },
    ],
    series,
  };
  destroyChart();
  body.replaceChildren();
  chart = new uPlot(opts, [t, ...ids.map((id) => data.series[String(id)].v)], body);
  if (state.type === "bars" && type !== "bars")
    body.append(h("p", { class: "help", style: "margin:4px 0 0" },
      `Barras ficam ilegíveis com mais de ${MAX_BAR_SERIES} medidores: mostrando linhas. Reduza a seleção para ver as barras.`));
  applyFocus();
}

/** Grandezas com faixa física fixa (ex.: FP em 0..1): o eixo aproxima dos dados em vez de mostrar a faixa inteira,
 *  com folga de 12% e um mínimo de 10% da faixa, sem nunca sair dela. Sem dados, mostra a faixa inteira. */
function fitRange(min, max, [lo, hi]) {
  if (min == null || max == null || !isFinite(min) || !isFinite(max)) return [lo, hi];
  const minSpan = (hi - lo) * 0.1;
  let span = max - min;
  if (span < minSpan) { const c = (min + max) / 2; min = c - minSpan / 2; max = c + minSpan / 2; span = minSpan; }
  const pad = span * 0.12;
  return [Math.max(lo, min - pad), Math.min(hi, max + pad)];
}

function axisDigits(splits, max) {
  const step = Math.abs((splits[1] ?? 1) - (splits[0] ?? 0));
  if (step >= 10) return 0;
  if (step >= 1) return Math.min(max, 1);
  return max;
}

const tf = {
  time: new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }),
  timeS: new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  dayTime: new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }),
  day: new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit" }),
  full: new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }),
};
function fmtTick(epochS, spanS, cursor = false) {
  const d = new Date(epochS * 1000);
  if (cursor) return tf.full.format(d);
  if (spanS <= 20 * 60) return tf.timeS.format(d);
  if (spanS <= 26 * 3600) return tf.time.format(d);
  if (spanS <= 8 * 86400) return tf.dayTime.format(d);
  return tf.day.format(d);
}

// redimensiona os dois gráficos com a página
let lastW = 0;
new ResizeObserver(() => {
  const w = $("ranking-chart").clientWidth;
  if (w && w !== lastW) { lastW = w; renderRanking(); }
  const body = $("generic-chart");
  if (chart && body.clientWidth && body.clientWidth !== chart.width) chart.setSize({ width: body.clientWidth, height: CHART_H });
}).observe($("ranking"));

// ------------------------------------------------------------------ CSV (visão em tabela dos mesmos dados)
$("btn-csv").addEventListener("click", () => {
  if (!lastData) return;
  const sep = ";";
  const iso = (s) => new Date(s * 1000).toISOString();
  const lines = [["ts", "medidor", "gateway", `${lastData.metric}${lastData.unit ? `_${lastData.unit}` : ""}`].join(sep)];
  for (const id of state.meters) {
    const s = lastData.series[String(id)];
    if (!s) continue;
    const gw = GW.get(s.gateway_id)?.name ?? "";
    lastData.t.forEach((t, i) => lines.push([iso(t), s.name, gw, s.v[i]].map(csvCell).join(sep)));
  }
  const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `bic-${lastData.metric}-${lastData.bucket}-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});
function csvCell(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return String(v).replace(".", ",");
  const s = String(v);
  return /[;"\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// ------------------------------------------------------------------ início
readUrl();
syncControls();
loadAll();
