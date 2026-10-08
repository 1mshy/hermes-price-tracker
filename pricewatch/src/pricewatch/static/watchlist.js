"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
let watches = [];
let filter = "all";
let refreshing = false;
let refreshRequested = false;
let adding = false;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function detailText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(row => row.msg || "Check your input.").join(" ");
  return detail?.error || detail?.note || "Could not save this change. Please try again.";
}
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options, headers: {"Content-Type": "application/json", ...options.headers},
    signal: AbortSignal.timeout(20000)
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) throw new Error(detailText(data.detail || data));
  return data;
}
function money(value, currency) {
  if (value == null) return "—";
  try { return new Intl.NumberFormat(undefined, {style: "currency", currency: currency || "USD", currencyDisplay: "code"}).format(value); }
  catch { return `${currency || ""} ${Number(value).toFixed(2)}`; }
}
function time(value) {
  if (!value) return "Not checked yet";
  // SQLite timestamps are UTC but can be serialized without an offset.
  const date = new Date(/[Zz]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return "Check time unavailable";
  return date.toLocaleString(undefined, {month:"short", day:"numeric", hour:"numeric", minute:"2-digit"});
}
function safeLink(url, label) {
  try {
    const parsed = new URL(url);
    if (!["https:", "http:"].includes(parsed.protocol)) return el("span", "", label);
    const a = el("a", "", label); a.href = parsed.href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
  } catch { return el("span", "", label); }
}
function ruleText(watch) {
  const rules = [];
  if (watch.target_price != null) rules.push(`At or below ${money(watch.target_price, watch.currency)}`);
  if (watch.drop_pct != null) rules.push(`${watch.drop_pct}% price drop`);
  if (watch.alert_on_restock) rules.push("Back in stock");
  return rules.join(" · ") || "No alert rule set";
}
function rowButton(text, onClick) {
  const button = el("button", "item-action", text); button.type = "button"; button.addEventListener("click", onClick); return button;
}
function numericInput(label, value, max) {
  const wrapper = el("label", "", label);
  const input = document.createElement("input"); input.type = "number"; input.min = "0.01"; input.step = "any";
  if (max) input.max = String(max);
  input.value = value == null ? "" : value; wrapper.append(input); return {wrapper, input};
}
function render() {
  const list = $("#watch-list"); list.replaceChildren();
  $("#watch-count").textContent = `${watches.length} ${watches.length === 1 ? "item" : "items"}`;
  const selected = watches.filter(w => filter === "all" || (filter === "paused" ? !w.active : w.active));
  if (!selected.length) list.append(el("p", "empty", watches.length ? "No items in this view." : "Your next good find starts here. Add a link or a product name above."));
  for (const watch of selected) {
    const card = el("article", "watch"); card.dataset.id = watch.tracker_id;
    const top = el("div", "watch-top"), info = el("div");
    info.append(el("h3", "", watch.label || watch.title));
    const offers = watch.offers || [];
    const state = !watch.active ? "paused" : watch.status || (offers.some(o => o.price != null && !o.error) ? "watching" : "pending");
    const meta = el("div", "meta");
    const stateLabel = {watching:"Watching", pending:"Finding prices", retrying:"Retrying quietly", paused:"Paused"}[state] || state;
    meta.append(el("span", `badge ${state}`, stateLabel), el("span", "", `${offers.length} ${offers.length === 1 ? "listing" : "listings"}`)); info.append(meta);
    const best = watch.current_best;
    const price = el("div", "price", best ? money(best.price, best.currency) : "—");
    const bestOffer = best && offers.find(o => o.url === best.url);
    price.append(el("small", "", best ? `${best.store}${bestOffer?.error ? " · last known price" : bestOffer?.in_stock === false ? " · out of stock" : ""}` : "Waiting for a price"));
    if (bestOffer?.last_checked) price.append(el("small", "", time(bestOffer.last_checked)));
    top.append(info, price); card.append(top);
    const bottom = el("div", "watch-bottom"); bottom.append(el("span", "rule", ruleText(watch)));
    const details = el("div", "offer-list"); details.hidden = true;
    if (!offers.length) details.append(el("p", "", "Saved. Hermes will look for matching listings in the background."));
    for (const offer of offers) {
      const row = el("div", "offer-row"), store = el("div"), quote = el("div");
      store.append(safeLink(offer.url, offer.store || "Open listing"), el("span", "", `Checked ${time(offer.last_checked)}`));
      quote.append(el("div", "", money(offer.price, offer.currency)), el("span", "", offer.error ? "Last attempt unsuccessful · retrying" : offer.in_stock === false ? "Out of stock" : offer.in_stock === true ? "In stock" : "Stock unconfirmed"));
      row.append(store, quote); details.append(row);
    }
    const detailsButton = rowButton("Listings", () => {details.hidden = !details.hidden; detailsButton.setAttribute("aria-expanded", String(!details.hidden));});
    detailsButton.setAttribute("aria-expanded", "false"); bottom.append(detailsButton);
    const edit = el("form", "edit-form"); edit.hidden = true;
    const target = numericInput(`Target (${watch.currency || "listing currency"})`, watch.target_price);
    const drop = numericInput("Price drop (%)", watch.drop_pct, 99);
    const restockLabel = el("label", "check-label"), restock = document.createElement("input"); restock.type = "checkbox"; restock.checked = watch.alert_on_restock;
    restockLabel.append(restock, document.createTextNode(" Restock alert"));
    const save = el("button", "", "Save rule"); save.type = "submit";
    edit.append(target.wrapper, drop.wrapper, restockLabel, save, rowButton("Cancel", () => {edit.hidden = true;}));
    const message = el("p", "row-message"); message.setAttribute("role", "status");
    edit.addEventListener("submit", async event => {
      event.preventDefault();
      if (!target.input.value && !drop.input.value && !restock.checked) {message.textContent = "Choose a target, price drop, or restock alert."; message.classList.add("error"); return;}
      save.disabled = true;
      try {await api(`/trackers/${watch.tracker_id}`, {method:"PATCH", body:JSON.stringify({target_price:target.input.value ? Number(target.input.value) : null, drop_pct:drop.input.value ? Number(drop.input.value) : null, alert_on_restock:restock.checked})}); await load();}
      catch (error) {message.textContent = error.message; message.classList.add("error");}
      finally {save.disabled = false;}
    });
    bottom.append(rowButton("Edit rule", () => {edit.hidden = !edit.hidden; if (!edit.hidden) target.input.focus();}));
    const pause = rowButton(watch.active ? "Pause" : "Resume", async () => {
      pause.disabled = true;
      try {await api(`/trackers/${watch.tracker_id}`, {method:"PATCH", body:JSON.stringify({active:!watch.active})}); await load();}
      catch (error) {message.textContent = error.message; message.classList.add("error");}
      finally {pause.disabled = false;}
    }); bottom.append(pause); card.append(bottom);
    if (watch.status_note && state !== "watching") card.append(el("p", "status-note", watch.status_note));
    card.append(details, edit, message); list.append(card);
  }
}
async function load() {
  if (refreshing) {refreshRequested = true; return;}
  refreshing = true; $("#refresh-view").disabled = true;
  try {
    const data = await api("/trackers"); watches = data.trackers; render(); $("#list-error").hidden = true;
  } catch (error) {
    $("#list-error").textContent = `Could not refresh your watchlist. ${error.message}`; $("#list-error").hidden = false;
    if (!watches.length) $("#watch-list").replaceChildren();
  }
  try {
    const health = await api("/health"), sweep = health.sweep || {};
    $("#engine-status").textContent = sweep.sweep_running ? "Checking your saved products now" : sweep.last_sweep_at ? `Last price check ${time(sweep.last_sweep_at)}` : "Ready to check your saved products";
    const channels = Object.entries(health.channels || {}).filter(([, enabled]) => enabled).map(([name]) => name);
    $("#delivery-status").textContent = channels.length ? `Alerts via ${channels.join(", ")}` : "No alert channel configured yet";
  } catch {$("#engine-status").textContent = "Price engine temporarily unreachable"; $("#delivery-status").textContent = "Alert delivery status unavailable";}
  refreshing = false; $("#refresh-view").disabled = false;
  if (refreshRequested) {refreshRequested = false; await load();}
}
$("#add-form").addEventListener("submit", async event => {
  event.preventDefault(); if (adding) return;
  const input = $("#product-input"), entries = [...new Set(input.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean))];
  if (!entries.length) {input.focus(); return;}
  if (entries.length > 25) {$("#add-result").replaceChildren(el("p", "error", "Please add up to 25 products at a time.")); return;}
  const options = {alert_on_restock:$("#restock").checked};
  if ($("#target-price").value) options.target_price = Number($("#target-price").value);
  if ($("#drop-pct").value) options.drop_pct = Number($("#drop-pct").value);
  const result = $("#add-result"); result.replaceChildren();
  adding = true; $("#add-button").disabled = true; input.readOnly = true;
  const failed = [];
  for (let i = 0; i < entries.length; i++) {
    $("#add-button").textContent = `Saving ${i + 1} of ${entries.length}…`;
    try {
      const saved = await api("/trackers", {method:"POST", body:JSON.stringify({input:entries[i], ...options})});
      result.append(el("p", "", `${saved.existing ? "Already saved" : "Saved"}: ${saved.title || entries[i]}. ${saved.note || "Looking for prices in the background."}`));
    } catch (error) {failed.push(entries[i]); result.append(el("p", "error", `${entries[i]}: ${error.message}`));}
  }
  input.value = failed.join("\n"); input.readOnly = false; adding = false;
  $("#add-button").disabled = false; $("#add-button").textContent = "Add to watchlist ↗";
  await load();
});
document.querySelectorAll(".filter").forEach(button => button.addEventListener("click", () => {
  filter = button.dataset.filter;
  document.querySelectorAll(".filter").forEach(b => {b.classList.toggle("selected", b === button); b.setAttribute("aria-pressed", String(b === button));}); render();
}));
$("#refresh-view").addEventListener("click", load);
api("/locale").then(locale => {$("#currency-hint").textContent = locale.preferred_currency ? `(${locale.preferred_currency} for names)` : "";}).catch(() => {});
load();
setInterval(() => {
  if (!document.hidden && !adding && !$(".watch input:focus") && !$(".watch button:focus") && ![...document.querySelectorAll(".edit-form, .offer-list")].some(panel => !panel.hidden)) load();
}, 30000);
