// Painel ao vivo: consulta /api/status a cada 2 s e renderiza KPIs, cards de gateway e a tabela de medidores
// no cliente. Filtros e o modal de detalhe ficam fora das áreas reconstruídas, então sobrevivem ao refresh.
import { api, toast, confirmDialog, fmt, fmtInt, fmtTime, h } from "./app.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;
const CYCLES_MS = 10000;

let snapshot = null;          // último /api/status
let cyclesByGw = {};          // gateway_id -> [{duration_ms, interval_ms}] (ordem cronológica)
let inflight = false;
let detailId = null;          // meter_id aberto no modal de detalhe
const filters = { q: "", gw: "", st: "" };

// ------------------------------------------------------------------ filtros (persistem na URL)
const params = new URLSearchParams(location.search);
$("f-q").value = params.get("q") || "";
$("f-status").value = params.get("st") || "";
const initialGw = params.get("gw") || "";

function readFilters() {
  filters.q = $("f-q").value.trim().toLowerCase();
  filters.gw = $("f-gw").value;
  filters.st = $("f-status").value;
  const p = new URLSearchParams();
  if ($("f-q").value.trim()) p.set("q", $("f-q").value.trim());
  if (filters.gw) p.set("gw", filters.gw);
  if (filters.st) p.set("st", filters.st);
  history.replaceState(null, "", p.toString() ? `?${p}` : location.pathname);
}
for (const id of ["f-q", "f-gw", "f-status"]) {
  $(id).addEventListener("input", () => { readFilters(); renderTable(); });
}

// ------------------------------------------------------------------ polling
async function poll() {
  if (inflight || document.hidden) return;
  inflight = true;
  try {
    snapshot = await api("GET", "/api/status");
    setPollState("ok", "ao vivo");
    $("updated-at").textContent = `atualizado às ${new Date().toLocaleTimeString("pt-BR")}`;
    render();
  } catch (err) {
    setPollState("bad", err.status ? `erro ${err.status}` : "sem conexão");
  } finally {
    inflight = false;
  }
}

async function pollCycles() {
  if (document.hidden) return;
  try {
    const rows = await api("GET", "/api/cycles/recent?minutes=10");
    const by = {};
    for (const r of rows) (by[r.gateway_id] ||= []).push(r);   // vem em ordem decrescente
    for (const k in by) by[k] = by[k].reverse().slice(-120);
    cyclesByGw = by;
    if (snapshot) renderGateways();
  } catch { /* o card fica sem sparkline até a próxima tentativa */ }
}

function setPollState(kind, text) {
  const el = $("poll-state");
  el.className = `pill ${kind}`;
  el.textContent = text;
}

setInterval(poll, POLL_MS);
setInterval(pollCycles, CYCLES_MS);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { poll(); pollCycles(); } });
poll();
pollCycles();

// ------------------------------------------------------------------ render
const statusOf = (m) => (m.online ? "online" : m.consecutive_failures ? "offline" : "waiting");
// O último erro fica guardado no banco para histórico, mas só interessa enquanto o medidor está offline.
const lastError = (m) => (statusOf(m) === "offline" && m.last_error) || "";
const gatewayOf = (id) => snapshot.gateways.find((g) => g.gateway_id === id);

function render() {
  renderKpis();
  renderGateways();
  syncGatewayFilter();
  renderTable();
  renderWriter();
  if ($("detail-dlg").open && detailId !== null) fillDetail(detailId);
}

function renderKpis() {
  const gws = snapshot.gateways || [];
  const meters = gws.flatMap((g) => g.meters);
  const online = meters.filter((m) => m.online);
  const kw = online.reduce((s, m) => s + (Number(m.last_fast?.p_total) || 0), 0);
  const overrun = gws.filter((g) => g.overrun || (g.cycles > 10 && g.avg_cycle_ms > g.interval_ms));
  const connected = gws.filter((g) => g.connected).length;
  const w = snapshot.writer || {};

  const tiles = [
    kpi("Medidores online", `${online.length}`, `de ${meters.length}`, meters.length && online.length < meters.length ? "bad" : "ok",
        meters.length - online.length ? `${meters.length - online.length} sem leitura` : "todos respondendo"),
    kpi("Potência ativa total", fmt(kw, 1), "kW", "", "soma dos medidores online"),
    kpi("Gateways conectados", `${connected}`, `de ${gws.length}`, overrun.length ? "bad" : connected === gws.length && gws.length ? "ok" : "",
        overrun.length ? `${overrun.length} com ciclo acima do intervalo` : "ciclos dentro do intervalo"),
    kpi("Gravação no banco", fmtInt(w.rows_written), "linhas", w.last_error ? "bad" : "",
        w.last_error ? `erro: ${w.last_error}` : `${fmtInt(w.pending)} pendentes`),
  ];
  $("kpis").replaceChildren(...tiles);
}

function kpi(label, value, unit, cls, sub) {
  return h("div", { class: `kpi ${cls}` },
    h("div", { class: "kpi-label" }, label),
    h("div", { class: "kpi-value" }, value, h("small", {}, unit)),
    h("div", { class: "kpi-sub" }, sub));
}

function renderGateways() {
  const host = $("gateways");
  if (!snapshot.poller_enabled) {
    host.replaceChildren(h("div", { class: "card" }, h("span", { class: "pill bad" }, "coletor desabilitado"), " ",
      h("span", { class: "muted" }, "POLLER_ENABLED=false — só o painel e a API estão ativos.")));
    return;
  }
  if (!snapshot.gateways.length) {
    host.replaceChildren(h("div", { class: "card empty-state" }, "Nenhum gateway cadastrado. Vá em ",
      h("a", { href: "/meters" }, "Gateways e medidores"), "."));
    return;
  }
  host.replaceChildren(...snapshot.gateways.map((g) => {
    const unreachable = g.cycles > 10 && g.avg_cycle_ms > g.interval_ms;
    return h("div", { class: "card gw-card" },
      h("div", { class: "gw-head" },
        h("div", { class: "gw-title" },
          h("h2", {}, g.name),
          h("small", {}, `${g.host}:${g.port}`)),
        h("span", { class: `pill ${g.connected ? "ok" : "bad"}` }, g.connected ? "conectado" : "sem conexão")),
      h("div", { class: "row" },
        h("span", { class: `pill ${g.overrun ? "bad" : "ok"}` }, `ciclo ${fmtInt(g.last_cycle_ms)} ms / ${fmtInt(g.interval_ms)} ms`),
        h("span", { class: "muted" }, `bloco ${g.fast_block} · ${g.meters.length} medidores`)),
      h("div", { class: "spark" }, sparklineSvg(cyclesByGw[g.gateway_id] || [], g.interval_ms)),
      h("p", { class: "help", style: "margin:0" },
        `média ${fmtInt(g.avg_cycle_ms)} ms · máx ${fmtInt(g.max_cycle_ms)} ms · ${fmtInt(g.cycles)} ciclos · ${fmtInt(g.overruns)} acima do intervalo`),
      unreachable ? h("p", { class: "help", style: "color:var(--bad)" },
        "O intervalo configurado não é alcançável neste gateway: ciclo médio maior que o intervalo. Reduza o número de medidores, use o bloco \"compact\" ou aumente o intervalo.") : null);
  }));
}

/** Mini-gráfico dos últimos ciclos: linha laranja, tracejado no intervalo, marcas vermelhas nos estouros. */
function sparklineSvg(rows, interval) {
  const W = 240, H = 34, PAD = 2;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  if (rows.length < 2) {
    const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
    t.setAttribute("x", "0"); t.setAttribute("y", "22"); t.setAttribute("fill", "#9ca3af"); t.setAttribute("font-size", "11");
    t.textContent = "sem histórico de ciclos ainda";
    svg.append(t);
    return svg;
  }
  const max = Math.max(interval, ...rows.map((r) => r.duration_ms)) * 1.1;
  const x = (i) => PAD + (i / (rows.length - 1)) * (W - 2 * PAD);
  const y = (v) => H - PAD - (v / max) * (H - 2 * PAD);
  const el = (tag, attrs) => { const n = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
  svg.append(el("line", { x1: 0, x2: W, y1: y(interval), y2: y(interval), stroke: "#9ca3af", "stroke-width": 1, "stroke-dasharray": "4 3", "vector-effect": "non-scaling-stroke" }));
  svg.append(el("polyline", { points: rows.map((r, i) => `${x(i)},${y(r.duration_ms)}`).join(" "), fill: "none", stroke: "#f26b1d", "stroke-width": 2, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
  rows.forEach((r, i) => {
    if (r.duration_ms > r.interval_ms) svg.append(el("line", { x1: x(i), x2: x(i), y1: y(r.duration_ms) - 3, y2: y(r.duration_ms) + 3, stroke: "#b91c1c", "stroke-width": 3, "vector-effect": "non-scaling-stroke" }));
  });
  return svg;
}

function syncGatewayFilter() {
  const sel = $("f-gw");
  const wanted = sel.value || initialGw;
  const names = snapshot.gateways.map((g) => [String(g.gateway_id), g.name]);
  const current = [...sel.options].slice(1).map((o) => [o.value, o.textContent]);
  if (JSON.stringify(names) !== JSON.stringify(current)) {
    sel.replaceChildren(h("option", { value: "" }, "todos os gateways"), ...names.map(([v, n]) => h("option", { value: v }, n)));
  }
  if (sel.value !== wanted) sel.value = names.some(([v]) => v === wanted) ? wanted : "";
  filters.gw = sel.value;
}

function renderTable() {
  if (!snapshot) return;
  const rows = [];
  let total = 0, shown = 0;
  for (const g of snapshot.gateways) {
    for (const m of g.meters) {
      total++;
      const st = statusOf(m);
      if (filters.q && !m.name.toLowerCase().includes(filters.q)) continue;
      if (filters.gw && String(g.gateway_id) !== filters.gw) continue;
      if (filters.st && st !== filters.st) continue;
      const lf = m.last_fast || {}, ls = m.last_slow || {};
      shown++;
      rows.push(h("tr", { class: "clickable", dataset: { meterId: m.meter_id }, title: "clique para ver todas as grandezas" },
        h("td", {}, h("strong", {}, m.name)),
        h("td", { class: "muted" }, g.name),
        h("td", { class: "num" }, m.unit_id),
        h("td", {}, statusPill(m)),
        h("td", { class: "num" }, fmt(lf.p_total, 2)),
        h("td", { class: "num" }, fmt(lf.i_avg, 1)),
        h("td", { class: "num" }, fmt(lf.vln_avg, 1)),
        h("td", { class: "num" }, fmt(lf.pf_total, 3)),
        h("td", { class: "num" }, fmt(ls.kwh_import, 1)),
        h("td", { class: "num" }, fmtInt(m.last_latency_ms)),
        h("td", { class: "muted" }, fmtTime(m.last_fast_at)),
        h("td", { class: "err", title: lastError(m) }, lastError(m)),
        h("td", { class: "actions" }, h("button", { type: "button", class: "btn sm", dataset: { detail: m.meter_id } }, "detalhes"))));
    }
  }
  if (!rows.length) {
    rows.push(h("tr", {}, h("td", { colspan: 13, class: "empty-state" },
      total ? "Nenhum medidor corresponde aos filtros." : "Nenhum medidor cadastrado.")));
  }
  $("meters-body").replaceChildren(...rows);
  $("f-count").textContent = total ? `${shown} de ${total}` : "";
}

function statusPill(m) {
  const st = statusOf(m);
  if (st === "online") return h("span", { class: "pill ok" }, "online");
  if (st === "offline") return h("span", { class: "pill bad" }, `offline (${m.consecutive_failures})`);
  return h("span", { class: "pill warn" }, "aguardando");
}

function renderWriter() {
  const w = snapshot.writer;
  const line = $("writer-line");
  if (!w) { line.textContent = ""; return; }
  line.replaceChildren(`Gravação: ${fmtInt(w.rows_written)} linhas gravadas · ${fmtInt(w.pending)} pendentes`);
  if (w.last_error) line.append(h("span", { style: "color:var(--bad)" }, ` · erro no banco: ${w.last_error}`));
}

// ------------------------------------------------------------------ detalhe do medidor
const detailDlg = $("detail-dlg");
const D = (k) => detailDlg.querySelector(`[data-d="${k}"]`);

$("meters-body").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-meter-id]");
  if (!tr) return;
  openDetail(Number(tr.dataset.meterId));
});

function findMeter(id) {
  for (const g of snapshot.gateways) for (const m of g.meters) if (m.meter_id === id) return [m, g];
  return [null, null];
}

function openDetail(id) {
  detailId = id;
  fillDetail(id);
  detailDlg.showModal();
}

function fillDetail(id) {
  const [m, g] = findMeter(id);
  if (!m) { detailDlg.close(); return; }
  const lf = m.last_fast || {}, ls = m.last_slow || {};
  D("name").textContent = m.name;
  D("sub").textContent = `${g.name} · unit ${m.unit_id}`;
  const pill = D("status");
  const st = statusOf(m);
  pill.className = `pill ${st === "online" ? "ok" : st === "offline" ? "bad" : "warn"}`;
  pill.textContent = st === "online" ? "online" : st === "offline" ? `offline (${m.consecutive_failures})` : "aguardando";
  D("compact-hint").hidden = g.fast_block !== "compact";
  const err = lastError(m);
  D("error").hidden = !err;
  D("error").textContent = err ? `último erro: ${err}` : "";
  for (const dd of detailDlg.querySelectorAll("dd[data-f]")) {
    const key = dd.dataset.f;
    const v = key in lf ? lf[key] : ls[key];
    dd.textContent = fmt(v, Number(dd.dataset.d || 1));
  }
  D("fast-at").textContent = fmtTime(m.last_fast_at);
  D("slow-at").textContent = fmtTime(m.last_slow_at);
  D("ok-at").textContent = fmtTime(m.last_ok_at);
  D("latency").textContent = `${fmtInt(m.last_latency_ms)} ms`;
  D("charts-link").href = `/charts?meters=${m.meter_id}&range=1h`;
}

const CMD_TEXT = {
  "reset-energy": ["Zerar energia acumulada", "zera a contagem de kWh/kvarh/kVAh", "zerar energia"],
  "reset-demand": ["Zerar demanda", "zera a demanda atual, anterior e o pico", "zerar demanda"],
  "reset-minmax": ["Zerar mínimos e máximos", "zera os registros de mín/máx do medidor", "zerar mín/máx"],
};
detailDlg.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-cmd]");
  if (!btn || detailId === null) return;
  const [m] = findMeter(detailId);
  if (!m) return;
  const cmd = btn.dataset.cmd;
  const [title, what, okLabel] = CMD_TEXT[cmd];
  const ok = await confirmDialog({ title, text: `${title} de "${m.name}"? Isto ${what} no próprio medidor e não pode ser desfeito.`, ok: okLabel, danger: cmd === "reset-energy" });
  if (!ok) return;
  const buttons = [...detailDlg.querySelectorAll("button[data-cmd]")];
  buttons.forEach((b) => (b.disabled = true));
  const prev = btn.textContent;
  btn.textContent = "enviando…";
  try {
    const r = await api("POST", `/api/meters/${m.meter_id}/${cmd}`);
    toast(`${m.name}: ${r.detail}`, r.success ? "ok" : "err");
  } catch (err) {
    toast(`${m.name}: ${err.message}`, "err");
  } finally {
    btn.textContent = prev;
    buttons.forEach((b) => (b.disabled = false));
  }
});
