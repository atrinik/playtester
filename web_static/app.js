"use strict";
// Atrinik observer dashboard. Polls a small live slice of /api/state every
// second and the heavy lists less often; renders the ground-level map from the
// runtime face images, statically (no animation), like the graphical client.

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clean = v => String(v ?? "").replace(/\[\/?[a-z]+(=[^\]]*)?\]/gi, "");

const LIVE_KEYS = "phase,connected,character,stats,level_progress,target,current_action,task,last_task,map,inventory,inventory_ready,ground,messages,interface,economy,conditions,protections,skills,party";
const SLOW_KEYS = "decisions,actions,quests,quest_journal,farm_spots,navigation_spots";
// Server inventory also carries objects the real client keeps out of the bag.
const HIDDEN_TYPES = new Set([29, 43, 105, 114]); // spell, skill, poisoning, force
const CATEGORIES = [
  ["all", "All items", () => true],
  ["equipped", "Equipped", i => i.equipped || i.applied],
  ["weapons", "Weapons & armour", i => [14, 15, 16, 33, 34, 35, 87, 99, 100, 104, 113, 39, 70].includes(i.type)],
  ["consumables", "Food, potions & scrolls", i => [5, 6, 54, 111, 109].includes(i.type)],
  ["missiles", "Missiles", i => i.type === 13],
  ["valuables", "Money & valuables", i => [36, 59, 60, 116, 117].includes(i.type)],
  ["containers", "Containers & keys", i => [21, 122].includes(i.type)],
  ["books", "Books & notes", i => [8, 80, 85].includes(i.type)],
];

let live = {}, slow = {}, selectedTag = null, unreadChat = 0, lastMessageTime = null, lastMessageSig = "";

// ---------- feedback ----------
let toastTimer = 0;
function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = error ? "show error" : "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), 3200);
}
async function post(path, data) {
  const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(data) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw Error(out.error || r.statusText);
  return out;
}
// Run a request from a button: disable it, show progress, report the outcome.
async function act(button, label, path, data) {
  const original = button ? button.textContent : "";
  if (button) { button.disabled = true; button.textContent = "…"; }
  try {
    await post(path, data);
    toast(label);
    setTimeout(pollLive, 250);
    return true;
  } catch (e) {
    toast(`${label} failed: ${e.message}`, true);
    return false;
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}
const task = (button, label, data) => act(button, label, "/api/task", data);
const action = (button, label, data) => act(button, label, "/api/action", data);

// ---------- face images ----------
const faces = new Map();
let redrawQueued = false;
function face(name) {
  if (!name) return null;
  let img = faces.get(name);
  if (!img) {
    img = new Image();
    img.onload = queueRedraw;
    img.onerror = () => (img.failed = true);
    img.src = `/api/face/${encodeURIComponent(name)}`;
    faces.set(name, img);
  }
  return img.complete && !img.failed && img.naturalWidth ? img : null;
}
const faceUrl = name => (name ? `/api/face/${encodeURIComponent(name)}` : "");

// ---------- header, vitals, automation ----------
function bar(label, value, max, cls) {
  const pct = Math.max(0, Math.min(100, ((+value || 0) / Math.max(1, +max || 1)) * 100));
  const low = cls === "hp" && pct < 30 ? " low" : "";
  return `<div class="vital"><b>${label}</b><div class="bar ${cls}${low}"><i style="width:${pct}%"></i><span>${value ?? "—"} / ${max ?? "—"}</span></div></div>`;
}
function renderHeader() {
  const s = live.stats || {}, lp = live.level_progress || {}, m = live.map || {};
  const on = live.phase === "playing";
  $("#connection-dot").classList.toggle("on", on);
  $("#connection").textContent = on ? "Playing" : live.phase || "offline";
  $("#character").textContent = live.character || "No character";
  $("#level-chip").textContent = `LV ${s.level ?? "—"}`;
  $("#xp-bar").style.width = `${lp.progress_percent ?? 0}%`;
  $("#xp-text").textContent = lp.next_experience == null ? `${(+lp.experience || 0).toLocaleString()} XP` : `${lp.progress_percent ?? 0}% · ${(+lp.remaining_experience || 0).toLocaleString()} to go`;
  $("#map-name").textContent = clean(m.name) || "No map";
  $("#map-region").textContent = m.region || "";
  $("#coords").textContent = m.path ? `${m.path}  (${m.x}, ${m.y})` : "—";
}
function renderVitals() {
  const s = live.stats || {};
  $("#vitals").innerHTML = bar("HP", s.hp, s.maxhp, "hp") + bar("SP", s.sp, s.maxsp, "sp") + bar("Food", s.food, 999, "food");
  const t = live.target;
  $("#target").innerHTML = t && t.id ? `Target: <b>${esc(t.name)}</b> · ${t.hp ?? "?"}% HP${t.combat ? " · fighting" : ""}` : "No target";
}
function renderTask() {
  const t = live.task, last = live.last_task;
  if (t) {
    const steps = t.trace || [];
    const leaf = steps[steps.length - 1];
    const circuit = steps.map(x => x.detail).filter(Boolean)[0];
    $("#task").innerHTML = `${esc(t.name)}<span class="state ${esc(t.status)}">${esc(t.status)}</span>${leaf && leaf !== steps[0] ? `<small>${esc(leaf.name)}</small>` : ""}${circuit ? `<small>${esc(circuit)}</small>` : ""}${t.error ? `<small class="flag bad">${esc(t.error)}</small>` : ""}`;
    $("#trace").innerHTML = steps.map(x => `<div style="padding-left:${x.depth * 10}px">${esc(x.name)} · ${esc(x.status)}${x.error ? ` · ${esc(x.error)}` : ""}${x.detail ? `<br>${esc(x.detail)}` : ""}</div>`).join("");
  } else {
    $("#task").innerHTML = `Idle<span class="state idle">no task</span>${last ? `<small>Last: ${esc(last.name)} · ${esc(last.status)}${last.error ? ` · ${esc(last.error)}` : ""}</small>` : ""}`;
    $("#trace").innerHTML = "";
  }
  const a = live.current_action;
  $("#action-now").textContent = a ? `Now: ${a.action} ${a.detail || ""}` : "";
  $("#resume").textContent = t && t.name.startsWith("autoplay") ? "Restart autoplay" : "Start autoplay";
}
function renderCharacter() {
  const s = live.stats || {};
  const stat = (k, label) => `<div class="stat"><b>${s[k] ?? "—"}</b><span>${label}</span></div>`;
  $("#stats").innerHTML = [["str", "STR"], ["dex", "DEX"], ["con", "CON"], ["int", "INT"], ["pow", "POW"], ["wc", "WC"], ["ac", "AC"], ["dam", "DAM"]].map(([k, l]) => stat(k, l)).join("");
  $("#skills").innerHTML = (live.skills || []).filter(k => k.level > 0).sort((a, b) => b.level - a.level).map(k =>
    `<div class="skill"><span>${esc(k.name)}</span><b>${k.level}</b><div class="bar"><i style="width:${k.progress_percent || 0}%"></i></div></div>`).join("") || '<span class="muted">No skills yet</span>';
  $("#protections").innerHTML = Object.entries(live.protections || {}).filter(([, v]) => v).map(([k, v]) => `<span>${esc(k)} ${v}%</span>`).join("") || '<span class="muted">None</span>';
  const e = live.economy || {};
  const money = v => { v = +v || 0; return `${Math.floor(v / 10000)}g ${Math.floor((v % 10000) / 100)}s ${v % 100}c`; };
  $("#wealth").textContent = `Carried ${money(e.carried)} · Bank ${e.bank_known ? money(e.banked) : "unknown"}`;
  const cond = live.conditions || {};
  $("#depletion").textContent = cond.depletion_points_known
    ? `Depletion ${cond.depletion_points} / ${cond.depletion_threshold} before a priest visit`
    : "Depletion unknown";
  $("#depletion").classList.toggle("warn-text", !!cond.depletion_points_known && cond.depletion_points > 0);
}

// ---------- map: static isometric ground level ----------
const TW = 48, TH = 24, HALF_W = TW / 2, HALF_H = TH / 2;
let zoom = 0; // 0 = fit
const canvas = $("#map"), ctx = canvas.getContext("2d");
let mapLayout = null;
function queueRedraw() { if (!redrawQueued) { redrawQueued = true; requestAnimationFrame(() => { redrawQueued = false; drawMap(); }); } }
function drawMap() {
  const m = live.map;
  if (!m || !m.tiles) return;
  const w = m.width, h = m.height, top = 72; // headroom for tall sprites
  const width = (w + h) * HALF_W, height = (w + h) * HALF_H + top + 8;
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ox = h * HALF_W, oy = top;
  mapLayout = { ox, oy, w, h };
  ctx.fillStyle = "#070909";
  ctx.fillRect(0, 0, width, height);
  ctx.imageSmoothingEnabled = false;
  const tiles = [...m.tiles].sort((a, b) => a.x + a.y - (b.x + b.y) || a.x - b.x);
  const targetId = live.target && live.target.id;
  const labels = [];
  for (const t of tiles) {
    const sx = ox + (t.x - t.y) * HALF_W - HALF_W, sy = oy + (t.x + t.y) * HALF_H;
    if (!t.objects.length) continue;
    for (const o of [...t.objects].sort((a, b) => a.layer - b.layer)) {
      const img = face(o.face);
      if (!img) continue;
      // Bottom-align to the tile, centred horizontally, like the client.
      ctx.drawImage(img, sx + (TW - img.width) / 2, sy + TH - img.height);
    }
    if (t.fogged) fog(sx, sy);
    const creature = [...t.objects].reverse().find(o => o.name && o.layer % 7 === 5);
    const isTarget = targetId && t.objects.some(o => o.target_id === targetId);
    if (t.player) outline(sx, sy, "#d9ad5b", 2);
    else if (isTarget) outline(sx, sy, "#ff4b3e", 2);
    if (creature && !t.player) {
      const hostile = t.objects.some(o => o.target && !o.friendly);
      const friendly = t.objects.some(o => o.friendly);
      dot(sx, sy, hostile ? "#e0675b" : friendly ? "#63c08a" : "#9aa4a1");
      labels.push([creature.name, sx + HALF_W, sy - 30, hostile]);
    }
  }
  if ($("#show-names").checked) {
    ctx.font = "11px Inter, sans-serif";
    ctx.textAlign = "center";
    for (const [name, x, y, hostile] of labels) {
      ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.85)"; ctx.strokeText(name, x, y);
      ctx.fillStyle = hostile ? "#ff9b90" : "#e8e4d6"; ctx.fillText(name, x, y);
    }
  }
  applyZoom();
}
function outline(sx, sy, color, width) {
  ctx.beginPath();
  ctx.moveTo(sx + HALF_W, sy); ctx.lineTo(sx + TW, sy + HALF_H); ctx.lineTo(sx + HALF_W, sy + TH); ctx.lineTo(sx, sy + HALF_H); ctx.closePath();
  ctx.lineWidth = width; ctx.strokeStyle = color; ctx.stroke();
}
// Remembered but not currently visible ground, dimmed like the client.
function fog(sx, sy) {
  ctx.beginPath();
  ctx.moveTo(sx + HALF_W, sy); ctx.lineTo(sx + TW, sy + HALF_H); ctx.lineTo(sx + HALF_W, sy + TH); ctx.lineTo(sx, sy + HALF_H); ctx.closePath();
  ctx.fillStyle = "rgba(4, 6, 8, 0.55)"; ctx.fill();
}
function dot(sx, sy, color) { ctx.beginPath(); ctx.arc(sx + HALF_W, sy + HALF_H, 3, 0, 7); ctx.fillStyle = color; ctx.fill(); }
function applyZoom() {
  const wrap = $("#map-wrap");
  const fit = Math.max(0.5, Math.min((wrap.clientWidth - 8) / canvas.width, (wrap.clientHeight - 8) / canvas.height));
  const scale = zoom === 0 ? fit : Math.max(0.5, fit * Math.pow(1.35, zoom));
  canvas.style.width = `${canvas.width * scale}px`;
  canvas.style.height = `${canvas.height * scale}px`;
}
function tileAt(event) {
  if (!mapLayout) return null;
  const r = canvas.getBoundingClientRect();
  const px = ((event.clientX - r.left) * canvas.width) / r.width - mapLayout.ox;
  const py = ((event.clientY - r.top) * canvas.height) / r.height - mapLayout.oy;
  const x = Math.floor((px / HALF_W + py / HALF_H) / 2), y = Math.floor((py / HALF_H - px / HALF_W) / 2);
  if (x < 0 || y < 0 || x >= mapLayout.w || y >= mapLayout.h) return null;
  return (live.map.tiles || []).find(t => t.x === x && t.y === y) || null;
}
canvas.addEventListener("mousemove", e => {
  const t = tileAt(e), tip = $("#map-tip");
  if (!t) { tip.style.display = "none"; return; }
  const names = t.objects.filter(o => o.name).map(o => `${esc(o.name)}${o.hp ? ` (${o.hp}%)` : ""}`);
  tip.innerHTML = `<b>${t.world_x}, ${t.world_y}</b>${t.player ? " · Sera" : ""}${names.length ? `<br>${names.join("<br>")}` : ""}<br><span class="muted">Click to walk here</span>`;
  const wr = $("#map-wrap").getBoundingClientRect();
  tip.style.left = `${e.clientX - wr.left + 14}px`;
  tip.style.top = `${e.clientY - wr.top + 14}px`;
  tip.style.display = "block";
});
canvas.addEventListener("mouseleave", () => ($("#map-tip").style.display = "none"));
canvas.addEventListener("click", e => {
  const t = tileAt(e);
  if (t) action(null, `Walking to ${t.world_x}, ${t.world_y}${live.task ? " (autoplay may take over again)" : ""}`, { type: "move", x: t.x, y: t.y });
});
$$("[data-zoom]").forEach(b => (b.onclick = () => { const d = +b.dataset.zoom; zoom = d === 0 ? 0 : zoom + d; applyZoom(); }));
$("#show-names").onchange = queueRedraw;
window.addEventListener("resize", applyZoom);

// ---------- underfoot ----------
function renderGround() {
  const items = live.ground || [];
  $("#ground").innerHTML = items.length ? items.map(i => `<button class="slot" data-ground="${i.tag}" title="${esc(i.name)} — click to pick up">${i.face ? `<img src="${faceUrl(i.face)}" alt="">` : `<span class="fallback">${esc(i.name)}</span>`}${i.quantity > 1 ? `<span class="qty">${i.quantity}</span>` : ""}</button>`).join("") : '<span class="muted">Nothing</span>';
  $$("[data-ground]").forEach(b => (b.onclick = () => action(b, `Picked up ${b.title.split(" — ")[0]}`, { type: "pickup", tag: +b.dataset.ground })));
}

// ---------- inventory ----------
CATEGORIES.forEach(([id, label]) => $("#inv-category").insertAdjacentHTML("beforeend", `<option value="${id}">${label}</option>`));
let lastInvSig = "";
function visibleItems() {
  const query = $("#inv-search").value.trim().toLowerCase();
  const [, , test] = CATEGORIES.find(c => c[0] === $("#inv-category").value) || CATEGORIES[0];
  return (live.inventory || []).filter(i => !HIDDEN_TYPES.has(i.type) && test(i) && (!query || i.name.toLowerCase().includes(query)));
}
function slotClass(i) {
  return ["slot", i.equipped && "equipped", !i.equipped && i.applied && "applied", i.magical && "magical", i.cursed && "cursed", i.unpaid && "unpaid", i.tag === selectedTag && "selected"].filter(Boolean).join(" ");
}
// Corner markers like the graphical client: lock top-left, magic/curse
// top-right, shop/trap/identification bottom-left.
function markers(i) {
  const tl = i.locked ? '<span class="mk tl" title="Locked">🔒</span>' : "";
  const tr = [i.magical && '<b class="mk-magic" title="Magical">✦</b>', i.cursed && `<b class="mk-curse" title="${i.damned ? "Damned" : "Cursed"}">${i.damned ? "☠☠" : "☠"}</b>`].filter(Boolean).join("");
  const bl = [i.unpaid && '<b class="mk-unpaid" title="Unpaid">$</b>', i.trapped && '<b class="mk-trap" title="Trapped">⚠</b>', i.identified === false && '<b class="mk-unid" title="Unidentified">?</b>'].filter(Boolean).join("");
  return tl + (tr ? `<span class="mk tr">${tr}</span>` : "") + (bl ? `<span class="mk bl">${bl}</span>` : "");
}
function statusText(i) {
  const s = [i.equipped && "equipped", !i.equipped && i.applied && "applied", i.magical && "magical", i.damned ? "damned" : i.cursed && "cursed", i.unpaid && "unpaid", i.trapped && "trapped", i.locked && "locked", i.identified === false && "unidentified"].filter(Boolean);
  return s.length ? ` (${s.join(", ")})` : "";
}
function renderInventory() {
  const items = visibleItems();
  const sig = JSON.stringify([items.map(i => [i.tag, i.quantity, i.equipped, i.applied, i.locked, i.face, i.magical, i.cursed, i.damned, i.unpaid, i.trapped, i.identified]), selectedTag, $("#inv-category").value]);
  if (sig !== lastInvSig) {
    lastInvSig = sig;
    const grid = $("#inventory");
    const equipped = items.filter(i => i.equipped || i.applied), rest = items.filter(i => !(i.equipped || i.applied));
    const slot = i => `<button class="${slotClass(i)}" data-tag="${i.tag}" title="${esc(i.name)}${statusText(i)}">${i.face ? `<img src="${faceUrl(i.face)}" alt="" loading="lazy">` : `<span class="fallback">${esc(i.name)}</span>`}${i.quantity > 1 ? `<span class="qty">${i.quantity}</span>` : ""}${markers(i)}</button>`;
    grid.innerHTML = (equipped.length && rest.length ? `<div class="inv-group">Worn & wielded</div>${equipped.map(slot).join("")}<div class="inv-group">Carried</div>` : equipped.map(slot).join("")) + rest.map(slot).join("") || '<span class="muted">No items</span>';
    $$("[data-tag]").forEach(b => (b.onclick = () => { selectedTag = +b.dataset.tag; renderInventory(); }));
  }
  renderItemDetail();
}
function renderItemDetail() {
  const i = (live.inventory || []).find(x => x.tag === selectedTag);
  const box = $("#item-detail");
  if (!i) { box.className = "item-detail muted"; box.textContent = "Select an item to see details and actions."; return; }
  box.className = "item-detail";
  const flags = [i.equipped && '<span class="flag good">equipped</span>', !i.equipped && i.applied && '<span class="flag good">applied</span>', i.locked && '<span class="flag warn">locked</span>', i.magical && '<span class="flag magic">magical</span>', i.cursed && `<span class="flag bad">${i.damned ? "damned" : "cursed"}</span>`, i.trapped && '<span class="flag bad">trapped</span>', i.unpaid && '<span class="flag warn">unpaid</span>', !i.identified && '<span class="flag">unidentified</span>'].filter(Boolean).join("");
  const meta = [i.quantity > 1 && `×${i.quantity}`, i.weight != null && `${(+i.weight * Math.max(1, i.quantity)).toFixed(1)} kg`, i.condition != null && i.condition !== 255 && `condition ${i.condition}%`, i.required_level && `needs level ${i.required_level}`].filter(Boolean).join(" · ");
  box.innerHTML = `<div class="item-head">${i.face ? `<img src="${faceUrl(i.face)}" alt="">` : "<span></span>"}<div><strong>${esc(i.name)}</strong><div class="item-meta">${meta}</div><div>${flags}</div></div></div>
    <div class="item-actions">
      <button data-do="apply">${i.equipped || i.applied ? "Unequip / unapply" : "Use / equip"}</button>
      <button data-do="examine">Examine</button>
      <button data-do="lock">${i.locked ? "Unlock" : "Lock"}</button>
      <button data-do="drop" class="danger" ${i.locked ? "disabled title='Unlock first'" : ""}>Drop</button>
    </div><p class="muted small">Examine results appear in the Chat tab.</p>`;
  $$("[data-do]").forEach(b => (b.onclick = () => {
    const kind = b.dataset.do;
    if (kind === "drop" && !confirm(`Drop ${i.name}?`)) return;
    const verbs = { apply: i.equipped || i.applied ? "Unequipped" : "Used", examine: "Examined", lock: i.locked ? "Unlocked" : "Locked", drop: "Dropped" };
    action(b, `${verbs[kind]} ${i.name}`, { type: kind, tag: i.tag });
  }));
}
$("#inv-search").oninput = () => { lastInvSig = ""; renderInventory(); };
$("#inv-category").onchange = () => { lastInvSig = ""; renderInventory(); };

// ---------- activity ----------
function renderActivity() {
  const list = [...((($("#activity-mode").value === "actions" ? slow.actions : slow.decisions) || []))].reverse();
  const q = $("#activity-search").value.trim().toLowerCase();
  $("#activity").innerHTML = list.filter(a => !q || `${a.action} ${a.detail} ${a.map}`.toLowerCase().includes(q)).slice(0, 250).map(a =>
    `<div class="act"><time>${new Date(a.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time><b title="${esc(a.task)}">${esc(a.action)}${a.count > 1 ? ` ×${a.count}` : ""}</b><span>${esc(a.detail)}${a.map ? ` <span class="muted">· ${esc(a.map.split("/").pop())} ${a.x},${a.y}</span>` : ""}</span></div>`).join("") || '<span class="muted">No activity yet</span>';
}
$("#activity-mode").onchange = renderActivity;
$("#activity-search").oninput = renderActivity;

// ---------- chat and dialogue ----------
function renderChat() {
  const msgs = live.messages || [], box = $("#messages");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  // The server keeps only the newest messages, so compare identities, not counts.
  const newest = msgs.length ? msgs[msgs.length - 1] : null;
  const sig = newest ? `${newest.time}|${newest.text}|${msgs.length}` : "";
  if (sig !== lastMessageSig || !box.childElementCount) {
    box.innerHTML = msgs.map(m => `<div class="msg"><time>${new Date(m.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time><span style="color:#${/^[0-9a-f]{6}$/i.test(m.color || "") ? m.color : "e8e4d6"}">${esc(clean(m.text))}</span></div>`).join("");
    // The first load is history, not unread chat.
    if (lastMessageTime !== null && !$("#tab-chat").classList.contains("active"))
      unreadChat += msgs.filter(m => m.time > lastMessageTime).length;
    lastMessageTime = newest ? newest.time : lastMessageTime ?? 0;
    lastMessageSig = sig;
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  $("#chat-badge").textContent = unreadChat ? Math.min(unreadChat, 99) : "";
  const d = live.interface;
  $("#dialogue").innerHTML = d ? `<b>${esc(d.title)}</b><p>${esc(d.text)}</p>${d.links.map((l, i) => `<button data-link="${i}">${esc(l)}</button>`).join("")}` : "";
  $$("[data-link]").forEach(b => (b.onclick = () => action(b, `Chose “${b.textContent}”`, { type: "dialog", index: +b.dataset.link })));
}
$("#chat-form").onsubmit = e => { e.preventDefault(); const v = $("#chat-input").value.trim(); if (v) action(e.submitter, `Said: ${v}`, { type: "chat", text: v }).then(ok => { if (ok) $("#chat-input").value = ""; }); };
$("#command-form").onsubmit = e => { e.preventDefault(); const v = $("#command-input").value.trim(); if (v) action(e.submitter, `Ran ${v}`, { type: "command", command: v }).then(ok => { if (ok) $("#command-input").value = ""; }); };

// ---------- quests, places, tasks ----------
let lastSlowSig = "";
function renderSlowLists() {
  const sig = JSON.stringify([slow.quests, slow.quest_journal, (slow.farm_spots || []).length, (slow.navigation_spots || []).length, (live.stats || {}).level]);
  if (sig === lastSlowSig) return;
  lastSlowSig = sig;
  $("#quest-journal").innerHTML = (slow.quest_journal || []).map(q => `<div class="quest ${esc(q.status)}"><strong>${esc(q.name)}</strong><span class="flag">${esc(q.status)}</span></div>`).join("") || '<span class="muted">Journal not loaded — press Refresh.</span>';
  $("#quests").innerHTML = (slow.quests || []).map(q => `<div class="quest"><strong>${esc(q.name)}</strong><button class="small" data-quest="${esc(q.name)}">Run</button><small>Level ${esc(q.suggested_level)} · ${esc(q.availability)}</small></div>`).join("");
  $$("[data-quest]").forEach(b => (b.onclick = () => task(b, `Started quest ${b.dataset.quest}`, { type: "quest", name: b.dataset.quest })));
  const level = (live.stats || {}).level || 1;
  $("#farm-spot").innerHTML = (slow.farm_spots || []).map(s => `<option value="${esc(s.id)}" ${s.min_level <= level && level <= s.max_level ? "" : 'class="muted"'}>${esc(s.name)} · L${s.min_level}–${s.max_level}${s.min_level <= level && level <= s.max_level ? " ✓" : ""}</option>`).join("");
  const chosen = $("#farm-spot").dataset.chosen;
  const fit = (slow.farm_spots || []).find(s => s.min_level <= level && level <= s.max_level);
  if (chosen && (slow.farm_spots || []).some(s => s.id === chosen)) $("#farm-spot").value = chosen;
  else if (fit) $("#farm-spot").value = fit.id;
  $("#nav-spot").innerHTML = '<option value="">Known destinations…</option>' + (slow.navigation_spots || []).map(s => `<option value="${esc(s.id)}">${esc(s.name)} · ${esc(s.category)}</option>`).join("");
}
$("#farm-spot").onchange = () => ($("#farm-spot").dataset.chosen = $("#farm-spot").value);
$("#nav-spot").onchange = () => { const s = (slow.navigation_spots || []).find(x => x.id === $("#nav-spot").value); if (s) { $("#nav-map").value = s.destination; $("#nav-x").value = s.x ?? ""; $("#nav-y").value = s.y ?? ""; } };
$("#resume").onclick = e => task(e.target, "Autoplay started", { type: "autoplay", until_level: 115 });
$("#stop").onclick = e => task(e.target, "Stopped — Sera is idle", { type: "stop" });
$("#run-auto").onclick = e => task(e.target, `Autoplay started (until level ${$("#auto-level").value})`, { type: "autoplay", until_level: +$("#auto-level").value });
$("#run-all").onclick = e => task(e.target, "Started all formal quests", { type: "quest", name: "all" });
$("#refresh-quests").onclick = e => action(e.target, "Requested quest journal", { type: "refresh_quests" });
$("#run-farm").onclick = e => {
  const s = (slow.farm_spots || []).find(x => x.id === $("#farm-spot").value);
  if (!s) return toast("Choose a farm spot first", true);
  const common = { until_level: +$("#farm-level").value };
  task(e.target, `Farming ${s.name}`, s.circuit && s.circuit.length ? { type: "farm_circuit", legs: s.circuit, ...common } : { type: "farm", zone: s.zone, target: s.target, item: "", quantity: 1, ...common });
};
$("#run-nav").onclick = e => { const dest = $("#nav-map").value.trim(); if (!dest) return toast("Enter a map path or choose a destination", true); task(e.target, `Travelling to ${dest}`, { type: "navigate", destination: dest, x: $("#nav-x").value, y: $("#nav-y").value }); };
$("#bank").onclick = e => task(e.target, "Depositing money", { type: "bank", banker: $("#banker").value, amount: $("#bank-amount").value });
$("#sell-junk").onclick = e => task(e.target, "Selling junk", { type: "sell_junk", merchant: "shop-floor", patterns: $("#junk-patterns").value.split("\n").map(x => x.trim()).filter(Boolean) });

// ---------- tabs ----------
$$(".tabs button").forEach(b => (b.onclick = () => {
  $$(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  $$(".tab").forEach(x => x.classList.toggle("active", x.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === "chat") { unreadChat = 0; renderChat(); const m = $("#messages"); m.scrollTop = m.scrollHeight; }
  if (b.dataset.tab === "activity") renderActivity();
}));

// ---------- polling ----------
let liveBusy = false, slowBusy = false;
async function pollLive() {
  if (liveBusy) return;
  liveBusy = true;
  try {
    const r = await fetch(`/api/state?include=${LIVE_KEYS}`, { cache: "no-store" });
    if (!r.ok) throw Error(`${r.status} ${r.statusText}`);
    live = await r.json();
    renderHeader(); renderVitals(); renderTask(); renderCharacter(); drawMap(); renderGround(); renderInventory(); renderChat(); renderSlowLists();
  } catch (e) {
    $("#connection").textContent = `Dashboard offline (${e.message})`;
    $("#connection-dot").classList.remove("on");
  } finally { liveBusy = false; }
}
async function pollSlow() {
  if (slowBusy) return;
  slowBusy = true;
  try {
    const r = await fetch(`/api/state?include=${SLOW_KEYS}`, { cache: "no-store" });
    if (r.ok) { slow = await r.json(); renderSlowLists(); if ($("#tab-activity").classList.contains("active")) renderActivity(); }
  } finally { slowBusy = false; }
}
pollLive(); pollSlow();
setInterval(pollLive, 1000);
setInterval(pollSlow, 5000);
