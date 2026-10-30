// Utilidades compartilhadas por todas as páginas: chamadas à API JSON, toasts, diálogo de confirmação,
// formulários <-> JSON e formatação pt-BR. Sem dependências e sem build.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function describeDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // erro de validação do pydantic: [{loc: ["body", "port"], msg: "..."}]
    return detail.map((e) => {
      const loc = (e.loc || []).filter((p) => p !== "body" && p !== "query");
      return (loc.length ? loc.join(".") + ": " : "") + e.msg;
    }).join("; ");
  }
  return JSON.stringify(detail);
}

/** Manda para a página de login guardando a URL atual para voltar depois. */
export function goToLogin() {
  location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
}

/** fetch same-origin com JSON. Lança ApiError com a mensagem (detail) do servidor. */
export async function api(method, url, body) {
  const opts = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    throw new ApiError(0, "sem conexão com o servidor");
  }
  if (res.status === 401) {
    // sessão expirou ou foi encerrada: vai para a página de login e volta para cá depois
    goToLogin();
    throw new ApiError(401, "não autenticado");
  }
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = data && typeof data === "object" && data.detail !== undefined ? describeDetail(data.detail) : `erro ${res.status}`;
    throw new ApiError(res.status, msg);
  }
  return data;
}

/** Aviso não bloqueante no canto da tela. kind: ok | err | info */
export function toast(text, kind = "ok", ms = 4500) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  el.textContent = text;
  host.append(el);
  const remove = () => { el.classList.add("out"); setTimeout(() => el.remove(), 260); };
  el.addEventListener("click", remove);
  setTimeout(remove, kind === "err" ? Math.max(ms, 7000) : ms);
}

/** Diálogo de confirmação. Resolve true se o usuário confirmou. */
export function confirmDialog({ title = "Confirmar", text = "", ok = "confirmar", danger = false } = {}) {
  return new Promise((resolve) => {
    const dlg = document.getElementById("confirm-dlg");
    dlg.querySelector("[data-title]").textContent = title;
    dlg.querySelector("[data-text]").textContent = text;
    const okBtn = dlg.querySelector("[data-ok]");
    okBtn.textContent = ok;
    okBtn.className = danger ? "btn danger" : "btn primary";
    dlg.returnValue = "";
    const onClose = () => { dlg.removeEventListener("close", onClose); resolve(dlg.returnValue === "ok"); };
    dlg.addEventListener("close", onClose);
    dlg.showModal();
    okBtn.focus();
  });
}

/** Lê um <form> como objeto JSON: checkbox -> boolean (mesmo desmarcado), number -> Number, texto -> trim. */
export function formToJson(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name || el.disabled) continue;
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = el.value === "" ? null : Number(el.value);
    else out[el.name] = el.value.trim();
  }
  return out;
}

/** Preenche um <form> a partir de um objeto (aceita strings "true"/"false" vindas de data-*). */
export function setFormValues(form, obj) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in obj)) continue;
    const v = obj[el.name];
    if (el.type === "checkbox") el.checked = v === true || v === "true" || v === "True" || v === 1;
    else el.value = v ?? "";
  }
}

const formatters = new Map();
/** Número em pt-BR (1.234,5). null/undefined/NaN -> "–". */
export function fmt(v, digits = 1) {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "–";
  let f = formatters.get(digits);
  if (!f) {
    f = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    formatters.set(digits, f);
  }
  return f.format(Number(v));
}
export const fmtInt = (v) => fmt(v, 0);

export function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "–" : d.toLocaleTimeString("pt-BR");
}
export function fmtDateTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "–" : d.toLocaleString("pt-BR");
}

/** Cria elementos sem innerHTML (nomes de máquina são texto, nunca HTML). */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

/** Desabilita um botão durante uma operação; devolve a função que restaura. */
export function busy(btn, label = "aguarde…") {
  if (!btn) return () => {};
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  return () => { btn.disabled = false; btn.textContent = prev; };
}

// Qualquer botão [data-close] fecha o <dialog> em que está.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-close]");
  if (b) b.closest("dialog")?.close();
});
