// Página "Gateways e medidores": tabelas somente leitura + modais de criação/edição que salvam na API JSON.
import { api, goToLogin, toast, confirmDialog, formToJson, setFormValues, busy } from "./app.js";

const tables = document.getElementById("meters-tables");
const gdlg = document.getElementById("gateway-dlg");
const gform = document.getElementById("gateway-form");
const mdlg = document.getElementById("meter-dlg");
const mform = document.getElementById("meter-form");

function openDialog(dlg, form, data, titles) {
  form.reset();
  form.dataset.id = data.id ?? "";
  dlg.querySelector("[data-title]").textContent = data.id ? titles[1] : titles[0];
  setFormValues(form, data);
  dlg.showModal();
  form.querySelector("input[name=name]").focus();
}

const openGateway = (d = {}) => {
  // novo gateway: sugere a primeira cor da paleta ainda não usada pelos gateways da tabela
  if (!d.id && !d.color) {
    const used = new Set([...tables.querySelectorAll("tr[data-kind=gateway]")].map((tr) => tr.dataset.color));
    const free = [...gform.querySelectorAll(".swatch")].map((b) => b.dataset.swatch).find((c) => !used.has(c));
    d = { ...d, color: free || gform.querySelector(".swatch")?.dataset.swatch };
  }
  openDialog(gdlg, gform, d, ["Novo gateway", "Editar gateway"]);
};
gform.addEventListener("click", (e) => {
  const b = e.target.closest(".swatch");
  if (b) gform.elements.color.value = b.dataset.swatch;
});
const openMeter = (d = {}) => openDialog(mdlg, mform, d, ["Novo medidor", "Editar medidor"]);

document.getElementById("btn-new-gateway").addEventListener("click", () => openGateway());
document.getElementById("btn-new-meter").addEventListener("click", () => {
  if (!mform.querySelector("select[name=gateway_id] option")) {
    toast("Cadastre um gateway antes de adicionar medidores", "info");
    return openGateway();
  }
  openMeter();
});

async function refreshTables() {
  const res = await fetch("/partials/meters", { headers: { Accept: "text/html" } });
  // sem sessão o servidor redireciona (303) para /login e o fetch segue o redirecionamento
  if (res.status === 401 || res.redirected || new URL(res.url).pathname === "/login") return goToLogin();
  if (!res.ok) throw new Error("falha ao atualizar a lista");
  tables.innerHTML = await res.text();
  // o select de gateways do modal de medidor acompanha a tabela (sem nova requisição)
  const sel = mform.querySelector("select[name=gateway_id]");
  const current = sel.value;
  sel.replaceChildren(...[...tables.querySelectorAll("tr[data-kind=gateway]")].map((tr) => {
    const o = document.createElement("option");
    o.value = tr.dataset.id;
    o.textContent = tr.dataset.name;
    return o;
  }));
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
}

tables.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const tr = btn.closest("tr");
  const d = tr.dataset;
  const isGateway = d.kind === "gateway";
  const action = btn.dataset.action;

  if (action === "edit") {
    if (isGateway) openGateway({ id: d.id, name: d.name, host: d.host, port: d.port, fast_block: d.fastBlock, enabled: d.enabled, color: d.color || undefined });
    else openMeter({ id: d.id, name: d.name, gateway_id: d.gatewayId, unit_id: d.unitId, enabled: d.enabled });
    return;
  }

  if (action === "delete") {
    const ok = await confirmDialog({
      title: isGateway ? "Remover gateway" : "Remover medidor",
      text: isGateway
        ? `Remover o gateway "${d.name}" e todos os seus medidores? As leituras já gravadas são mantidas.`
        : `Remover o medidor "${d.name}"? As leituras já gravadas são mantidas.`,
      ok: "remover", danger: true,
    });
    if (!ok) return;
    const done = busy(btn, "removendo…");
    try {
      await api("DELETE", `/api/${isGateway ? "gateways" : "meters"}/${d.id}`);
      toast(`${isGateway ? "Gateway" : "Medidor"} "${d.name}" removido`);
      await refreshTables();
    } catch (err) {
      toast(err.message, "err");
      done();
    }
    return;
  }

  if (action === "identify") {
    const done = busy(btn, "lendo…");
    try {
      const m = await api("POST", `/api/meters/${d.id}/identify`);
      toast(`${m.name}: série ${m.serial_number || "?"}, firmware ${m.firmware || "?"}, TC ${m.ct_primary || "?"} A`);
      await refreshTables();
    } catch (err) {
      toast(`Falha ao identificar ${d.name}: ${err.message}`, "err");
      done();
    }
  }
});

function wireForm(form, dlg, resource, label) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = form.dataset.id;
    const body = formToJson(form);
    const done = busy(form.querySelector("button[type=submit]"), "salvando…");
    form.dataset.busy = "1";
    try {
      const saved = id ? await api("PUT", `/api/${resource}/${id}`, body) : await api("POST", `/api/${resource}`, body);
      toast(`${label} "${saved.name}" ${id ? "atualizado" : "criado"}; o coletor já usa a nova configuração`);
      dlg.close();
      await refreshTables();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      delete form.dataset.busy;
      done();
    }
  });
  // não deixa o Esc fechar o modal no meio de um salvamento
  dlg.addEventListener("cancel", (e) => { if (form.dataset.busy) e.preventDefault(); });
}
wireForm(gform, gdlg, "gateways", "Gateway");
wireForm(mform, mdlg, "meters", "Medidor");
