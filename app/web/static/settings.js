import { api, toast, formToJson, busy } from "./app.js";

const form = document.getElementById("settings-form");
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const done = busy(form.querySelector("button[type=submit]"), "salvando…");
  try {
    await api("PUT", "/api/settings", formToJson(form));
    toast("Configurações salvas; o coletor já usa os novos valores");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    done();
  }
});
