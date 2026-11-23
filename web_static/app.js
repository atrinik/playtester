"use strict";
// Atrinik observer dashboard. Polls a small live slice of /api/state every
// second and the heavy lists less often; renders the ground-level map from the
// runtime face images, statically (no animation), like the graphical client.

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clean = v => String(v ?? "").replace(/\[\/?[a-z]+(=[^\]]*)?\]/gi, "");
// The Content-Security-Policy refuses inline style attributes, so rendered
// HTML carries sizes and colours as data attributes and this sets them
// through CSSOM: data-width (percent), data-indent (pixels), data-color (hex).
function applyStyles(root) {
  root.querySelectorAll("[data-width]").forEach(el => { el.style.width = `${el.dataset.width}%`; });
  root.querySelectorAll("[data-indent]").forEach(el => { el.style.paddingLeft = `${el.dataset.indent}px`; });
  root.querySelectorAll("[data-color]").forEach(el => { el.style.color = `#${el.dataset.color}`; });
}

const LIVE_KEYS = "phase,connected,character,stats,level_progress,target,current_action,task,last_task,map,inventory,inventory_ready,ground,messages,interface,economy,conditions,protections,skills,party,alerts,engine_error";
const SLOW_KEYS = "build,farming_analysis,post_mortems,trends,decisions,actions,quests,quest_journal,farm_spots,navigation_spots,unlocks,equipment,route_blocks,route_learned,dialogue_edges,route_shadow";
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
  return `<div class="vital"><b>${label}</b><div class="bar ${cls}${low}"><i data-width="${pct}"></i><span>${value ?? "—"} / ${max ?? "—"}</span></div></div>`;
}
let lastAlertsSig = "";
function renderAlerts() {
  // The region is aria-live: rewrite it only when the alerts change.
  const html = (live.alerts || []).map(a =>
    `<span class="alert ${esc(a.level)}" title="${esc(a.id)}">${esc(a.text)}</span>`).join("");
  if (html === lastAlertsSig) return;
  lastAlertsSig = html;
  $("#alerts").innerHTML = html;
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
// The build never changes while the process runs; it arrives with the slow poll.
function renderBuild() {
  const b = slow.build;
  if (!b) return;
  const link = (text, href, title) => href
    ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(title)}">${esc(text)}</a>`
    : `<span title="${esc(title)}">${esc(text)}</span>`;
  const parts = [];
  if (b.commit) {
    parts.push(link(b.version || "unreleased", b.release_url,
      b.distance ? `${b.distance} commit(s) after ${b.tag}` : (b.tag ? `Release ${b.tag}` : "No release tag")));
    parts.push(link(b.short_commit, b.commit_url, `Commit ${b.commit}`));
  } else {
    parts.push(link("unknown build", "", "Built without Git metadata"));
  }
  if (b.dirty) parts.push('<span class="warn-text" title="Built from a checkout with local changes">modified</span>');
  $("#build").innerHTML = parts.join(" · ");
}
// Farming analyzer: status, the objective toggle and that objective's ranking.
const rate = v => { v = +v || 0; return v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}k` : `${Math.round(v)}`; };
// Money is held in copper and shown in the game's coins, largest first:
// coins(768192) is "76g 81s 92c"; coins(v, 2) keeps the two largest coins.
const COIN_LABELS = [[1e8, " amber"], [1e7, " mithril"], [1e6, " jade"], [1e4, "g"], [100, "s"], [1, "c"]];
const coins = (v, limit = COIN_LABELS.length) => {
  v = Math.round(+v || 0);
  const sign = v < 0 ? "-" : "";
  v = Math.abs(v);
  const parts = [];
  for (const [worth, label] of COIN_LABELS) {
    const count = Math.floor(v / worth);
    v -= count * worth;
    if (count && parts.length < limit) parts.push(`${count}${label}`);
  }
  return sign + (parts.join(" ") || "0c");
};
const since = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? `${Math.round(s)}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`; };
let lastFarmingSig = "";
// The way to a spot from her savebed (analyzer approach danger).
const pct = v => `${Math.round((+v || 0) * 100)}%`;
function approachLine(r, fromBed) {
  if (!r.approach_status || r.approach_status === "disabled") return "";
  const worst = String(r.approach_worst_map || "").replace(/^.*\//, "");
  const text = r.approach_status === "unreachable"
    ? `Approach unknown: no modelled path from ${fromBed ? "her savebed" : "any savebed or town"}`
    : r.approach_status === "origin" ? "Approach: starts at an origin"
    : `Approach ${pct(r.approach_safety_score)} safe${worst ? ` · worst ${esc(worst)} (danger ${Math.round(+r.approach_danger || 0)})` : ""} · ${(r.approach_maps || []).length} maps`;
  return `<small class="${r.approach_risky ? "warn-text" : "muted"}" title="Spot safety ${pct(r.spot_safety_score)} · overall ${pct(r.safety_score)}${r.approach_risky ? " · farmed only when no spot with a safe approach is usable" : ""}">${text}</small>`;
}
function renderFarming() {
  const fa = slow.farming_analysis;
  if (!fa) return;
  const sig = JSON.stringify([fa.status, fa.objective, fa.selected_objective, fa.objective_override, fa.finished_at, fa.error, fa.running, fa.self_improvement, fa.savebed, fa.farmed_spot && fa.farmed_spot.path]);
  const status = $("#fa-status");
  status.classList.toggle("warn-text", fa.status === "failed" || fa.status === "unsupported");
  status.textContent = fa.running ? "Analyzing…"
    : fa.status === "ready" ? `Updated ${since(fa.finished_at)} ago · ${fa.duration}s run`
    : fa.status === "failed" ? `Last run failed: ${fa.error}`
    : fa.status === "unsupported" ? `Not analyzed: ${fa.error}`
    : fa.status === "unavailable" ? "Analyzer unavailable" : "Waiting for the first run";
  if (sig === lastFarmingSig) return;
  lastFarmingSig = sig;
  const chosen = fa.selected_objective || fa.objective;
  $("#fa-objectives").innerHTML = Object.entries(fa.objectives || {}).map(([key, label]) =>
    `<button role="radio" aria-checked="${key === chosen}" class="${key === chosen ? "active" : ""}" data-objective="${esc(key)}">${esc(label)}</button>`).join("");
  const p = fa.profile;
  const override = fa.objective_override
    ? `Farming for ${(fa.objectives || {})[fa.objective] || fa.objective} while depleted (${fa.objective_override.points} points); back to ${(fa.objectives || {})[chosen] || chosen} once cured. `
    : "";
  $("#fa-profile").textContent = override + (p
    ? `Simulating ${p.skill} ${p.skill_level}${p.max_aggro_pack ? ` (≤${p.max_aggro_pack} aggro)` : ''} · ${p.spell_damage > 0 ? `spells only: ${p.spell_damage} ${p.spell_attack}/cast · ${p.cast_delay}s/cast · ${p.mana_cost} of ${p.max_mana} SP · ${p.mana_regen} SP/s` : `DAM ${p.damage} · WC ${p.weapon_class} · ${p.weapon_speed}s/swing`} · HP ${p.max_health} · AC ${p.armour_class}`
    : "") + (fa.approach_origin ? ` · approach from ${String(fa.approach_origin).replace(/^.*\//, "")}` : "");
  // Mark the spot autoplay farms; list it first when it ranks below the top five.
  const farmed = fa.farmed_spot;
  const shown = (fa.ranking || []).map((r, i) => [r, i + 1]);
  if (farmed && !shown.some(([r]) => r.path === farmed.path)) shown.unshift([farmed, farmed.rank]);
  $("#fa-ranking").innerHTML = shown.map(([r, rank]) => `
    <div class="spot${(farmed ? r.path === farmed.path : rank === 1) ? " top" : ""}">
      <div class="spot-head"><b>#${rank ?? "?"} ${esc(r.name)}</b>${farmed && r.path === farmed.path ? '<span class="flag">farming</span>' : ""}<span class="flag">${esc(String(r.region || "").replace(/_/g, " "))}</span></div>
      <small>${esc(r.location || (r.map_paths || []).join(", "))}</small>
      <div class="spot-metrics"><span title="Estimated XP per hour">XP ${rate(r.xp_hour)}/h</span><span title="Estimated money per hour">${coins(r.money_hour, 2)}/h</span><span title="Estimated loot drops per hour">Loot ${rate(r.loot_hour)}/h</span><span title="Estimated incoming damage per clear">Danger ${Math.round(+r.danger || 0)}</span></div>
      ${approachLine(r, !!fa.approach_origin)}
      <small class="muted">${esc((r.monsters || []).slice(0, 5).join(", "))}${(r.monsters || []).length > 5 ? " …" : ""}</small>
    </div>`).join("") || '<span class="muted">No ranking yet.</span>';
  const learning = fa.self_improvement || {};
  const goals = learning.goals || {};
  const goalText = [goals.area_spells ? "area spells for packs" : "",
    goals.offence ? "better weapon and armour" : "", goals.healing ? "more healing" : "",
    ...Object.keys(goals.protection || {}).map(k => `${k} protection`)].filter(Boolean).join(", ");
  const bed = fa.savebed;
  const bedText = bed ? `<small class="muted">Respawns at the savebed on ${esc(bed.map.replace(/^.*\//, ""))} (${bed.x}, ${bed.y}).</small>` : "";
  $("#fa-learning").innerHTML = bedText + ((learning.spots || []).map(s => `
    <div class="spot">
      <div class="spot-head"><b>${esc(s.spot.split("|")[0].replace(/^.*\//, ""))}</b><span class="flag">adaptation ${s.level}/${s.max_level}</span></div>
      ${(s.reviews || []).slice().reverse().map(r => `<small>${new Date(r.time * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} · ${esc(r.kind)} · <b>${esc(r.cause)}</b>: ${esc(r.detail)}${(r.remedies || []).length ? ` → ${esc(r.remedies.join(", "))}` : ""}</small>`).join("")}
    </div>`).join("") + (goalText ? `<small class="muted">Improvement goals: ${esc(goalText)}</small>` : ""))
    || '<span class="muted">No deaths or emergency recalls to learn from yet.</span>';
}
// Death post-mortems: the minute before each recent death.
let lastDeathsSig = "";
function vitalsChart(pm) {
  const v = pm.vitals || [];
  if (!v.length) return "";
  const t0 = pm.time - 60, w = 240, h = 48;
  // The window is the 60 seconds ending at the death.
  const x = t => Math.max(0, Math.min(w, ((t - t0) / 60) * w)).toFixed(1);
  // Samples are taken only when a vital changes: it holds its value until
  // the next one, and the last until the death, so each trace is drawn as
  // steps (not interpolated) through the death time; one sample is flat.
  const y = (pick, s) => (h - Math.max(0, Math.min(1, pick(s))) * h).toFixed(1);
  const line = (pick, cls) => `<polyline class="${cls}" points="${v.map((s, i) =>
    (i ? `${x(s[0])},${y(pick, v[i - 1])} ` : "") + `${x(s[0])},${y(pick, s)}`).join(" ")} ${x(pm.time)},${y(pick, v[v.length - 1])}"/>`;
  return `<svg class="vitals-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Health, spell points and food over the last minute">`
    + line(s => s[1] / Math.max(1, s[2]), "hp") + line(s => s[3] / Math.max(1, s[4]), "sp")
    + line(s => s[5] / 1000, "food") + "</svg>";
}
function renderDeaths() {
  const deaths = slow.post_mortems || [];
  // The whole reports: a death's record is rebuilt in place after its
  // post-death window, under the same time.
  const sig = JSON.stringify(deaths);
  if (sig === lastDeathsSig) return;
  lastDeathsSig = sig;
  $("#fa-deaths").innerHTML = deaths.map(d => {
    const when = new Date(d.time * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const who = Object.entries(d.attackers || {}).map(([k, n]) => `${esc(k)} ×${n}`).join(", ");
    const types = Object.entries(d.damage_by_type || {}).map(([k, n]) => `${esc(k)} ${n}`).join(", ");
    const last = (d.messages || []).slice(-6).map(m => `<small class="muted">${esc(clean(m[1]))}</small>`).join("");
    // What the bot was doing: its last decisions before the death.
    // The death's own bookkeeping (the death, its depletion points) is not
    // a decision that explains it.
    const did = (d.decisions || []).filter(r => r.action !== "death" && r.action !== "depletion").slice(-4).map(r =>
      `<small>${esc(r.action)}${r.detail ? `: ${esc(r.detail)}` : ""}</small>`).join("");
    return `<div class="spot">
      <div class="spot-head"><b>${when} · ${esc(String(d.map || "").replace(/^.*\//, ""))} (${d.x}, ${d.y})</b><span class="flag">${esc(d.cause)}</span></div>
      <small>${esc(d.detail)}${d.damage_taken ? ` · ${d.damage_taken} damage${types ? ` (${types})` : ""}` : ""}${who ? ` · ${who}` : ""}</small>
      ${vitalsChart(d)}${did}${last}
    </div>`;
  }).join("") || '<span class="muted">No deaths recorded yet.</span>';
}
// Day-long trends: hourly rates and levels from per-minute samples.
const TREND_SERIES = [
  ["xp_per_hour", "XP per hour", v => rate(v)],
  ["deaths_per_hour", "Deaths per hour", v => `${v}`],
  ["heals_per_hour", "Heals per hour", v => `${v}`],
  ["depletion", "Depletion points", v => `${v}`],
  ["food", "Food", v => `${v}`],
];
function trendLine(points, key) {
  const vals = points.map(p => p[key]).filter(v => v !== null && v !== undefined);
  if (vals.length < 2) return "";
  const t0 = points[0].t, t1 = points[points.length - 1].t || t0 + 1;
  const max = Math.max(1, ...vals), w = 240, h = 32;
  const pts = points.filter(p => p[key] !== null && p[key] !== undefined)
    .map(p => `${(((p.t - t0) / Math.max(1, t1 - t0)) * w).toFixed(1)},${(h - (p[key] / max) * h).toFixed(1)}`).join(" ");
  return `<svg class="trend-chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${esc(key)} over the last day"><polyline points="${pts}"/></svg>`;
}
let lastTrendsSig = "";
function renderTrends() {
  const points = (slow.trends || {}).points || [];
  // The current minute's values change between buckets.
  const sig = points.length ? `${points.length}:${JSON.stringify(points[points.length - 1])}` : "";
  if (sig === lastTrendsSig) return;
  lastTrendsSig = sig;
  if (points.length < 2) { $("#fa-trends").innerHTML = '<span class="muted">Collecting samples…</span>'; return; }
  const last = points[points.length - 1];
  $("#fa-trends").innerHTML = TREND_SERIES.map(([key, label, fmt]) => {
    const value = last[key];
    return `<div class="trend"><div class="spot-head"><small>${label}</small><b>${value === null || value === undefined ? "–" : esc(fmt(value))}</b></div>${trendLine(points, key)}</div>`;
  }).join("");
}
$("#fa-objectives").addEventListener("click", e => {
  const b = e.target.closest("button[data-objective]");
  if (!b || b.classList.contains("active")) return;
  action(b, `Farming objective: ${b.textContent}`, { type: "farm_objective", objective: b.dataset.objective })
    .then(ok => { if (ok) { lastFarmingSig = ""; pollSlow(); } });
});
function renderVitals() {
  const s = live.stats || {};
  $("#vitals").innerHTML = bar("HP", s.hp, s.maxhp, "hp") + bar("SP", s.sp, s.maxsp, "sp") + bar("Food", s.food, 999, "food");
  applyStyles($("#vitals"));
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
    $("#trace").innerHTML = steps.map(x => `<div data-indent="${(+x.depth || 0) * 10}">${esc(x.name)} · ${esc(x.status)}${x.error ? ` · ${esc(x.error)}` : ""}${x.detail ? `<br>${esc(x.detail)}` : ""}</div>`).join("");
    applyStyles($("#trace"));
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
    `<div class="skill"><span>${esc(k.name)}</span><b>${k.level}</b><div class="bar"><i data-width="${+k.progress_percent || 0}"></i></div></div>`).join("") || '<span class="muted">No skills yet</span>';
  applyStyles($("#skills"));
  $("#protections").innerHTML = Object.entries(live.protections || {}).filter(([, v]) => v).map(([k, v]) => `<span>${esc(k)} ${v}%</span>`).join("") || '<span class="muted">None</span>';
  const e = live.economy || {};
  $("#wealth").textContent = `Carried ${coins(e.carried)} · Bank ${e.bank_known ? coins(e.banked) : "unknown"}` +
    (e.operator_assisted_copper ? ` · ${coins(e.operator_assisted_copper)} given by an operator, not earned` : "");
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
    box.innerHTML = msgs.map(m => `<div class="msg"><time>${new Date(m.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time><span data-color="${/^[0-9a-f]{6}$/i.test(m.color || "") ? m.color : "e8e4d6"}">${esc(clean(m.text))}</span></div>`).join("");
    applyStyles(box);
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
  const sig = JSON.stringify([slow.quests, slow.quest_journal, (slow.farm_spots || []).map(s => s.id), (slow.navigation_spots || []).length]);
  if (sig === lastSlowSig) return;
  lastSlowSig = sig;
  $("#quest-journal").innerHTML = (slow.quest_journal || []).map(q => `<div class="quest ${esc(q.status)}"><strong>${esc(q.name)}</strong><span class="flag">${esc(q.status)}</span></div>`).join("") || '<span class="muted">Journal not loaded — press Refresh.</span>';
  $("#quests").innerHTML = (slow.quests || []).map(q => `<div class="quest"><strong>${esc(q.name)}</strong><button class="small" data-quest="${esc(q.name)}">Run</button><small>Level ${esc(q.suggested_level)} · ${esc(q.availability)}</small></div>`).join("");
  $$("[data-quest]").forEach(b => (b.onclick = () => task(b, `Started quest ${b.dataset.quest}`, { type: "quest", name: b.dataset.quest })));
  // Spots come from the farming analyzer's ranking for the preferred objective.
  $("#farm-spot").innerHTML = (slow.farm_spots || []).map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("") || '<option value="">Waiting for the farming analyzer…</option>';
  const chosen = $("#farm-spot").dataset.chosen;
  if (chosen && (slow.farm_spots || []).some(s => s.id === chosen)) $("#farm-spot").value = chosen;
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
  task(e.target, `Farming ${s.name}`, { type: "farm_circuit", analyzer_spot: s.id, legs: s.circuit, ...common });
};
$("#run-nav").onclick = e => { const dest = $("#nav-map").value.trim(); if (!dest) return toast("Enter a map path or choose a destination", true); task(e.target, `Travelling to ${dest}`, { type: "navigate", destination: dest, x: $("#nav-x").value, y: $("#nav-y").value }); };
$("#bank").onclick = e => task(e.target, "Depositing money", { type: "bank", banker: $("#banker").value, amount: $("#bank-amount").value });
$("#sell-junk").onclick = e => task(e.target, "Selling junk", { type: "sell_junk", merchant: "shop-floor", patterns: $("#junk-patterns").value.split("\n").map(x => x.trim()).filter(Boolean) });

// ---------- unlocks (read-only; docs/ROUTING.md 4.3) ----------
// Built with DOM nodes and classes only: no markup strings, no inline styles.
const UNLOCK_WHERE = { inventory: "carried", container: "in an open container" };
function unlockRow(title, flag, note) {
  const row = document.createElement("div");
  row.className = "quest";
  const name = document.createElement("strong");
  name.textContent = title;
  const tag = document.createElement("span");
  tag.className = "flag";
  tag.textContent = flag;
  row.append(name, tag);
  if (note) {
    const small = document.createElement("small");
    small.textContent = note;
    row.append(small);
  }
  return row;
}
let lastUnlockSig = "";
function renderUnlocks() {
  const u = slow.unlocks;
  const sig = JSON.stringify(u ?? null);
  if (sig === lastUnlockSig) return;
  lastUnlockSig = sig;
  const rows = [];
  if (!u) {
    const none = document.createElement("span");
    none.className = "muted";
    none.textContent = "Not available.";
    rows.push(none);
  } else {
    for (const key of u.keys || []) {
      const items = (key.items || []).map(i => `${i.name} (${UNLOCK_WHERE[i.where] || i.where}${i.container ? `: ${i.container}` : ""})`).join(", ");
      rows.push(unlockRow(key.lock, `${key.tiles} gated tile${key.tiles === 1 ? "" : "s"}`, items));
    }
    if (!(u.keys || []).length) rows.push(unlockRow("No keys", u.inventory_known ? "none carried" : "inventory loading", ""));
    for (const a of u.ambiguous || []) rows.push(unlockRow(a.name, "ambiguous: opens nothing", `could be any of ${a.locks.join(", ")}`));
    const notes = [`${(u.keys || []).length} of ${u.locks_known} lock ids open`, u.quests_loaded ? `${u.quest_parts_done} journal parts done` : "journal not loaded"];
    if (u.override) notes.push("override: every gate open");
    const summary = document.createElement("small");
    summary.className = "muted";
    summary.textContent = `${notes.join(" · ")} · state ${u.fingerprint}`;
    rows.push(summary);
  }
  $("#unlocks").replaceChildren(...rows);
}

// ---------- owner blocks (docs/ROUTING.md 4.7) ----------
// Built with DOM nodes and classes only, like the unlocks: no markup strings.
function blockText(tag, text, cls) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  el.textContent = text;
  return el;
}
function blockSpan(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s >= 86400) return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
  if (s >= 3600) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 60)} min`;
}
const blockWhen = t => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
async function blockPost(button, label, data) {
  const original = button.textContent;
  button.disabled = true; button.textContent = "…";
  try {
    await post("/api/route-blocks", data);
    toast(label);
    lastBlockSig = "";
    setTimeout(pollSlow, 250);
    return true;
  } catch (e) {
    toast(`${label} failed: ${e.message}`, true);
    return false;
  } finally { button.disabled = false; button.textContent = original; }
}
let lastBlockSig = "";
function renderBlocks() {
  const b = slow.route_blocks;
  const sig = JSON.stringify(b ?? null);
  if (sig === lastBlockSig) return;
  lastBlockSig = sig;
  const rows = [];
  if (!b) {
    rows.push(blockText("span", "Not available.", "muted"));
  } else {
    if (!b.character_loaded) rows.push(blockText("span", "Blocks are kept per character: waiting for her to log in.", "muted"));
    for (const block of b.blocks || []) {
      const row = document.createElement("div");
      row.className = "quest";
      row.append(blockText("strong", block.subject));
      const remove = blockText("button", "Remove", "small danger");
      remove.onclick = () => blockPost(remove, `Removed the block on ${block.subject}`, { action: "remove", subject: block.subject });
      row.append(remove);
      row.append(blockText("small", `until ${blockWhen(block.until)} (${blockSpan(block.remaining_seconds)} left)${block.note ? ` · ${block.note}` : ""}`));
      rows.push(row);
    }
    if (b.character_loaded && !(b.blocks || []).length) rows.push(blockText("span", "No blocks: every route is open to her.", "muted"));
  }
  $("#route-blocks").replaceChildren(...rows);
  const care = [];
  for (const c of (b && b.care) || []) {
    const row = document.createElement("div");
    row.className = "quest";
    row.append(blockText("strong", `${c.trip || "trip"} to ${c.destination}`));
    const state = c.cleared ? (c.starving ? "crossing starving" : c.food ? "crossing hungry" : "crossing") : c.staging ? "walking to her savebed" : "waiting";
    row.append(blockText("span", state, c.cleared ? (c.food ? "flag bad" : "flag warn") : c.alerted ? "flag bad" : "flag"));
    row.append(blockText("small", `${c.subjects.join(", ")} · ${c.mode} · waited ${blockSpan(c.waiting_seconds)}${c.missing.length && !c.cleared ? ` · needs ${c.missing.join(", ")}` : ""}`));
    care.push(row);
  }
  for (const d of (b && b.decisions) || []) {
    const row = document.createElement("div");
    row.className = "quest";
    row.append(blockText("strong", d.action));
    row.append(blockText("span", blockWhen(d.time), "flag"));
    row.append(blockText("small", `${d.detail || ""}${d.map ? ` · ${d.map}` : ""}`));
    care.push(row);
  }
  if (!care.length) care.push(blockText("span", "No care crossings recorded.", "muted"));
  $("#route-care").replaceChildren(...care);
}
$("#block-here").onclick = () => { const path = (live.map || {}).path; if (!path) return toast("Her map is not known yet", true); $("#block-subject").value = path; };
$("#block-add").onclick = e => {
  const subject = $("#block-subject").value.trim();
  if (!subject) return toast("Enter a map path, an exit or a seam", true);
  const seconds = Number($("#block-duration").value);
  blockPost(e.target, `Blocked ${subject} for ${blockSpan(seconds)}`, { action: "add", subject, seconds, note: $("#block-note").value.trim() })
    .then(ok => { if (ok) { $("#block-subject").value = ""; $("#block-note").value = ""; } });
};

// ---------- tabs ----------
$$(".tabs button").forEach(b => (b.onclick = () => {
  $$(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  $$(".tab").forEach(x => x.classList.toggle("active", x.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === "chat") { unreadChat = 0; renderChat(); const m = $("#messages"); m.scrollTop = m.scrollHeight; }
  if (b.dataset.tab === "activity") renderActivity();
  if (b.dataset.tab === "knowledge") pollKnowledge();
  if (b.dataset.tab === "equipment") renderEquipment();
  if (b.dataset.tab === "farming") { pollTerrain(); pollOwnerWatch(); }
  if (b.dataset.tab === "feedback") pollFeedback();
}));

// ---------- knowledge (read-only) ----------
// Her observation ledger: what she read or was told and where it came from.
const KIND_LABELS = { door: "Door", gate: "Gate", lever: "Lever", sign: "Sign", magic_mouth: "Magic mouth", npc: "Dialogue", book: "Book", scroll: "Scroll", examine: "Examined", journal: "Journal", system: "Server" };
let knowledgeBusy = false;
async function pollKnowledge() {
  if (knowledgeBusy) return;
  knowledgeBusy = true;
  try {
    const kind = $("#knowledge-kind").value, factKind = $("#fact-kind").value;
    const r = await fetch(`/api/knowledge?limit=200&fact_limit=200${kind ? `&kind=${encodeURIComponent(kind)}` : ""}${factKind ? `&fact_kind=${encodeURIComponent(factKind)}` : ""}`, { cache: "no-store" });
    if (r.ok) { const k = await r.json(); renderFacts(k); renderKnowledge(k); }
  } finally { knowledgeBusy = false; }
}
function renderKnowledge(k) {
  const counts = k.counts || {}, select = $("#knowledge-kind"), chosen = select.value;
  const kinds = Object.keys(counts).sort();
  const options = '<option value="">All sources</option>' + kinds.map(x => `<option value="${esc(x)}">${esc(KIND_LABELS[x] || x)} (${counts[x]})</option>`).join("");
  if (select.dataset.sig !== options) { select.innerHTML = options; select.value = chosen; select.dataset.sig = options; }
  const total = kinds.reduce((sum, x) => sum + counts[x], 0);
  $("#knowledge-summary").textContent = `${total.toLocaleString()} reading${total === 1 ? "" : "s"} kept` +
    (k.authored_index ? "" : " · authored-text index unavailable: only fact lines are kept") +
    (k.busy ? " · the database is busy; try again shortly" : "");
  const when = t => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  $("#knowledge").innerHTML = (k.observations || []).map(o => `<div class="reading">
    <div class="reading-head"><b>${esc(KIND_LABELS[o.source_kind] || o.source_kind)}</b><span>${esc(o.source_name || o.source_key)}</span><span class="flag">${esc(o.attribution)}</span>${o.seen > 1 ? `<span class="flag">×${esc(o.seen)}</span>` : ""}<time>${esc(when(o.last_at))}</time></div>
    <p>${esc(clean(o.text))}</p>
    <small>${esc(o.map)}${o.x == null ? "" : ` (${esc(o.x)}, ${esc(o.y)})`} · ${esc(o.source_key)}</small>
  </div>`).join("") || '<span class="muted">Nothing read yet.</span>';
}
$("#knowledge-kind").onchange = pollKnowledge;

// Facts her parsers read from those readings (facts.py), built as DOM nodes
// with textContent only.
const FACT_LABELS = { "tile-property": "Tile property", "key-name": "Key name", "key-holder": "Key holder", "lock-colour": "Lock colour", "npc-key": "NPC mentions a key", password: "Password", direction: "Direction", "trap-warning": "Trap warning", weakness: "Weakness", immunity: "Immunity", "quest-gate": "Quest gate", "quest-part": "Quest part", unopenable: "Unopenable", "generic-lock": "Generic lock text" };
const node = (tag, className, text) => { const e = document.createElement(tag); if (className) e.className = className; if (text != null) e.textContent = String(text); return e; };
function renderFacts(k) {
  const counts = k.fact_counts || {}, select = $("#fact-kind"), chosen = select.value;
  const kinds = Object.keys(counts).sort();
  const sig = kinds.map(x => `${x}:${counts[x]}`).join(",");
  if (select.dataset.sig !== sig) {
    select.replaceChildren(node("option", "", "All kinds"), ...kinds.map(x => { const o = node("option", "", `${FACT_LABELS[x] || x} (${counts[x]})`); o.value = x; return o; }));
    select.firstChild.value = "";
    select.value = kinds.includes(chosen) ? chosen : "";
    select.dataset.sig = sig;
  }
  const total = kinds.reduce((sum, x) => sum + counts[x], 0);
  $("#facts-summary").textContent = `${total.toLocaleString()} fact${total === 1 ? "" : "s"} kept · no-magic tile facts ` +
    (k.persist_tile_facts ? "put a potion before a spell there (persist_tile_facts on)" : "are recorded only (persist_tile_facts off)");
  const when = t => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const rows = (k.facts || []).map(f => {
    const row = node("div", "reading fact");
    const head = node("div", "reading-head");
    head.append(node("b", "", FACT_LABELS[f.kind] || f.kind), node("span", "fact-value", f.value === "" ? "(not given)" : f.value),
      node("span", `flag ${f.status === "confirmed" ? "good" : ""}`.trim(), f.status), node("span", "flag", `${Math.round((f.confidence || 0) * 100)}%`));
    const time = node("time", "", when(f.updated_at)); head.append(time);
    row.append(head);
    if (f.detail) row.append(node("p", "", clean(f.detail)));
    row.append(node("small", "", [f.subject, f.source_name, f.parser].filter(Boolean).join(" · ")));
    return row;
  });
  $("#facts").replaceChildren(...(rows.length ? rows : [node("span", "muted", "No facts yet.")]));
}
$("#fact-kind").onchange = pollKnowledge;
// ---------- learned route subjects (read-only; docs/ROUTING.md 4.4, 4.8) ----------
// Built with node() and textContent only. route_costs.py reads the same rows.
const SIGNAL_LABELS = { death: ["death", "deaths"], recall: ["recall", "recalls"], damage: ["damage", "damage"], blocked: ["held exit", "held exits"], crossing_failed: ["failed crossing", "failed crossings"] };
const signalCount = (k, n) => `${n} ${(SIGNAL_LABELS[k] || [k, k])[n === 1 ? 0 : 1]}`;
const learnedCost = s => (s < 60 ? `${Math.round(s)} s` : blockSpan(s));
let lastLearnedSig = "";
function renderLearned() {
  const l = slow.route_learned;
  const sig = JSON.stringify(l ?? null);
  if (sig === lastLearnedSig) return;
  lastLearnedSig = sig;
  const rows = [];
  if (!l) rows.push(node("span", "muted", "Not available."));
  else if (!l.character_loaded) rows.push(node("span", "muted", "Route events are kept per character: waiting for her to log in."));
  else {
    if (l.error) rows.push(node("span", "muted", l.error));
    for (const s of l.subjects || []) {
      const row = node("div", "quest");
      row.append(node("strong", "", s.subject), node("span", s.signals.death ? "flag bad" : "flag warn", `${learnedCost(s.cost_seconds)} provisional`));
      const signals = Object.entries(s.signals).map(([k, n]) => signalCount(k, n)).join(", ");
      row.append(node("small", "", `${signals} · last: ${(SIGNAL_LABELS[s.last_signal] || [s.last_signal])[0]} ${blockWhen(s.last_at)} · half by ${blockWhen(s.half_at)}${s.traversals ? ` · crossed ${s.traversals}×` : ""}`));
      rows.push(row);
    }
    if (!(l.subjects || []).length && !l.error) rows.push(node("span", "muted", "Nothing learned: no route has cost her anything yet."));
    const totals = Object.entries(l.events || {}).filter(([, n]) => n).map(([k, n]) => signalCount(k, n)).join(", ");
    rows.push(node("small", "muted", `${l.learned || 0} subjects with a cost · ${l.traversed || 0} crossed · events (7 d): ${totals || "none"}${l.pending ? ` · ${l.pending} not written yet` : ""}${l.migrated ? "" : " · history not migrated yet"}`));
  }
  $("#route-learned").replaceChildren(...rows);
}
// ---------- shadow routes (read-only; docs/ROUTING.md 4.8, P6) ----------
// Built with node() and textContent only.
let lastShadowSig = "";
const shadowCost = c => (c && c.total != null ? `${learnedCost(c.time)} travel, ${learnedCost(c.prior)} prior, ${learnedCost(c.learned)} learned` : "no route");
function renderShadow() {
  const d = slow.route_shadow, sig = JSON.stringify(d ?? null);
  if (sig === lastShadowSig) return;
  lastShadowSig = sig;
  if (!d) return $("#route-shadow").replaceChildren(node("span", "muted", "Not available."));
  const rows = [node("small", "muted", d.enabled ? "learned_route_costs on: her live plans use learned costs; the counts below are live plans." : "learned_route_costs off: her plans are as before; the learned ones only run in the shadow.")];
  for (const [kind, trips] of Object.entries(d.counts || {})) {
    for (const [trip, results] of Object.entries(trips)) {
      const row = node("div", "quest");
      const total = Object.values(results).reduce((a, b) => a + b, 0);
      row.append(node("strong", "", `${kind} · ${trip}`), node("span", "flag", `${total}`), node("small", "", Object.entries(results).map(([r, n]) => `${r} ${n}`).join(", ")));
      rows.push(row);
    }
  }
  if (!Object.keys(d.counts || {}).length) rows.push(node("span", "muted", "No comparison yet."));
  const farm = d.farm;
  if (farm) {
    const row = node("div", "quest");
    const spot = s => (s && s.name ? `${s.name} (D ${s.danger ?? "?"} s${s.score != null ? `, score ${s.score}` : ""})` : "none");
    row.append(node("strong", "", "Farm score"), node("span", `flag ${farm.result === "agreed" ? "good" : "warn"}`, farm.result), node("small", "", `live ${spot(farm.live)} · shadow ${spot(farm.shadow)}${farm.care ? " · with farm-route care" : ""}${(farm.chokepoints || []).length ? ` · chokepoints: ${farm.chokepoints.join(", ")}` : ""}`));
    rows.push(row);
  }
  for (const e of d.recent || []) {
    const row = node("div", "quest");
    const title = e.kind === "farm" ? `farm: ${(e.live || {}).name || "none"} → ${(e.shadow || {}).name || "none"}` : `${e.trip}: ${e.source} → ${e.destination}`;
    row.append(node("strong", "", title), node("span", `flag ${e.result === "diverged" ? "warn" : "bad"}`, e.result));
    const notes = [blockWhen(e.at)];
    if (e.kind === "trip") {
      notes.push(`live: ${(e.live || {}).edges || 0} edges, ${(e.live || {}).error || shadowCost(e.live)}`);
      notes.push(`shadow: ${(e.shadow || {}).edges || 0} edges, ${shadowCost(e.shadow)}`);
      if (e.why) notes.push(e.why);
      if (e.reason) notes.push(e.reason);
      if (e.care) notes.push(`care: ${e.care}`);
      const worst = ((e.shadow || {}).worst || []).map(([s, v]) => `${s} ${learnedCost(v)}`).join(", ");
      if (worst) notes.push(`worst: ${worst}`);
    } else if (e.paused && Object.keys(e.paused).length) {
      notes.push(`paused: ${Object.entries(e.paused).slice(0, 3).map(([k, v]) => `${k} ${v}`).join("; ")}`);
    }
    row.append(node("small", "", notes.join(" · ")));
    rows.push(row);
  }
  rows.push(node("small", "muted", `${(d.in_flight || []).length} in flight · ${d.skipped || 0} skipped by the throttle · ${d.dropped || 0} dropped · model v${d.model_version || 0}`));
  $("#route-shadow").replaceChildren(...rows);
}
// ---------- voyages (read-only; docs/ROUTING.md P4) ----------
// NPC voyages and quest-gated exits, built with node() and text only.
let lastVoyageSig = "";
function renderVoyages() {
  const d = slow.dialogue_edges, sig = JSON.stringify(d ?? null);
  if (sig === lastVoyageSig) return;
  lastVoyageSig = sig;
  if (!d) return $("#voyages").replaceChildren(node("span", "muted", "Not available."));
  const rows = [node("small", "muted", d.enabled ? "dialogue_edges on: her routes may use the voyages offered below." : "dialogue_edges off: her routes use no voyage and every quest-gated exit stays as before.")];
  for (const v of d.voyages || []) {
    const row = node("div", "quest");
    row.append(node("strong", "", `${v.npc}: ${v.source} → ${v.destination}`), node("span", `flag ${v.status === "offered" ? "good" : ""}`.trim(), v.status));
    const notes = [...(v.requires || []), ...(v.side_effects || []).map(x => `side effect: ${x}`), v.interface];
    row.append(node("small", "", notes.join(" · ")));
    rows.push(row);
  }
  for (const g of d.gated_exits || []) {
    const row = node("div", "quest");
    row.append(node("strong", "", `exit ${g.exit}`), node("span", "flag", g.open == null ? "flag off" : g.open ? "open" : "closed"), node("small", "", (g.requires || []).join(" · ")));
    rows.push(row);
  }
  if (d.error) rows.push(node("small", "muted", d.error));
  $("#voyages").replaceChildren(...rows);
}
// ---------- equipment (read-only) ----------
// Effective item stats per gear family, built from DOM nodes and text only.
// node() is shared with the Knowledge tab above.
const signed = v => (v > 0 ? `+${v}` : `${v}`);
function gearPiece(title, r) {
  const piece = node("div", "gear-piece");
  const head = node("div", "gear-piece-head");
  head.append(node("span", "muted", title), node("strong", null, r.name));
  if (r.source === "archetype") head.append(node("span", "flag", "not examined"));
  else if (r.source === "unknown") head.append(node("span", "flag warn", "unknown"));
  head.append(node("span", "gear-scores", `current ${r.old_score} · effective ${r.new_score ?? "—"}`));
  piece.append(head);
  if ((r.stats || []).length) {
    const stats = node("ul", "gear-stats");
    for (const [label, value] of r.stats) stats.append(node("li", null, `${label} ${value}`));
    piece.append(stats);
  } else if (r.source !== "unknown") piece.append(node("span", "muted", "Adds no stats when worn."));
  if ((r.notes || []).length) {
    const notes = node("ul", "gear-notes");
    for (const note of r.notes) notes.append(node("li", null, note));
    piece.append(notes);
  }
  return piece;
}
let lastEquipSig = "";
function renderEquipment() {
  const e = slow.equipment, sig = JSON.stringify(e || null);
  if (sig === lastEquipSig) return;
  lastEquipSig = sig;
  const box = $("#equipment"), shadow = $("#equipment-shadow");
  const families = (e && e.families) || [];
  $("#equipment-summary").textContent = e ? `${e.examined} examined this session` : "";
  box.replaceChildren(...families.map(f => {
    const card = node("div", f.diverges ? "gear-family diverges" : "gear-family");
    const head = node("div", "gear-head");
    head.append(node("b", null, f.label));
    if (f.diverges) head.append(node("span", "flag warn", "scores disagree"));
    card.append(head);
    for (const w of f.worn) card.append(gearPiece("Worn", w));
    if (!f.worn.length) card.append(node("span", "muted", "Nothing worn."));
    if (f.candidate) {
      card.append(gearPiece("Best carried", f.candidate));
      if (f.deltas.length) {
        const deltas = node("ul", "gear-deltas");
        for (const [label, value] of f.deltas) deltas.append(node("li", value > 0 ? "delta up" : "delta down", `${label} ${signed(value)}`));
        card.append(deltas);
      }
    }
    if (f.diverges) {
      const choice = c => (c ? `put on ${c.name}` : "keep what she wears");
      card.append(node("div", "gear-shadow", `Current scoring would ${choice(f.old_choice)}; effective stats would ${choice(f.new_choice)}.`));
    }
    return card;
  }));
  if (e && e.error) box.append(node("span", "warn-text", e.error));
  else if (!families.length) box.append(node("span", "muted", "No gear carried."));
  const log = [...((e && e.shadow) || [])].reverse();
  shadow.replaceChildren(...log.map(x => {
    const row = node("div", "act");
    row.append(node("time", null, new Date(x.time * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })), node("b", null, "gear-shadow"), node("span", null, x.detail));
    return row;
  }));
  if (!log.length) shadow.append(node("span", "muted", "No comparison logged yet."));
}

// ---------- terrain (read-only; docs/LEARNING_AND_FEEDBACK.md 4.7) ----------
// Her fights by terrain, built from DOM nodes and text only with node().
const FEATURE_LABELS = { corridor: "Corridor", doorway: "Doorway", room: "Room", open: "Open ground", unknown: "Unknown" };
const OUTCOME_FLAGS = { won: "flag good", retreated: "flag warn", recalled: "flag warn", died: "flag bad" };
let terrainBusy = false, lastTerrainSig = "";
async function pollTerrain() {
  if (terrainBusy) return;
  terrainBusy = true;
  try {
    const r = await fetch("/api/terrain?limit=20", { cache: "no-store" });
    if (r.ok) renderTerrain(await r.json());
  } finally { terrainBusy = false; }
}
function renderTerrain(t) {
  const sig = JSON.stringify(t);
  if (sig === lastTerrainSig) return;
  lastTerrainSig = sig;
  $("#terrain-summary").textContent = `${(t.total || 0).toLocaleString()} fight${t.total === 1 ? "" : "s"} kept` +
    (t.terrain_known ? "" : " · world graph unavailable: features unknown") +
    (t.busy ? " · the database is busy; try again shortly" : "");
  const rates = (t.rates || []).map(r => {
    const row = node("div", "quest");
    row.append(node("strong", "", `${FEATURE_LABELS[r.feature] || r.feature} · ${r.attackers === 3 ? "3+" : r.attackers} at once${r.ranged ? " · ranged in view" : ""}`),
      node("span", r.death_rate > 0 ? "flag bad" : "flag", `${Math.round((r.death_rate || 0) * 100)}% deaths`),
      node("small", "", `${r.fights} fight${r.fights === 1 ? "" : "s"} · ${r.damage_per_second == null ? "no time" : `${r.damage_per_second} damage/s`}`));
    return row;
  });
  $("#terrain-rates").replaceChildren(...(rates.length ? rates : [node("span", "muted", "No fights recorded yet.")]));
  const when = s => new Date(s * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const fights = (t.fights || []).map(f => {
    const row = node("div", "reading");
    const head = node("div", "reading-head");
    head.append(node("b", "", FEATURE_LABELS[f.feature] || f.feature), node("span", "", `reach ${f.reach ?? "?"}`),
      node("span", OUTCOME_FLAGS[f.outcome] || "flag", f.outcome), node("time", "", when(f.ended)));
    row.append(head);
    const parts = [`${f.peak_attackers} at once`, `${f.damage_taken} damage in ${Math.round(f.seconds)} s`];
    if (f.ranged_attackers) parts.push(`${f.ranged_attackers} ranged in view, ${f.ranged_covered} covered`);
    if (f.kills) parts.push(`${f.kills} killed`);
    if (f.players_in_view) parts.push(`${f.players_in_view} player${f.players_in_view === 1 ? "" : "s"} in view`);
    row.append(node("p", "", parts.join(" · ")), node("small", "", `${f.map} (${f.x}, ${f.y})`));
    return row;
  });
  $("#terrain-fights").replaceChildren(...fights);
}

// ---------- watching the owner (read-only; docs/LEARNING_AND_FEEDBACK.md 4.14) ----------
// The operator character's fights she watched, built with node() only.
let ownerWatchBusy = false, lastOwnerWatchSig = "";
async function pollOwnerWatch() {
  if (ownerWatchBusy) return;
  ownerWatchBusy = true;
  try {
    const r = await fetch("/api/owner-watch?limit=20", { cache: "no-store" });
    if (r.ok) renderOwnerWatch(await r.json());
  } finally { ownerWatchBusy = false; }
}
function renderOwnerWatch(w) {
  const sig = JSON.stringify(w);
  if (sig === lastOwnerWatchSig) return;
  lastOwnerWatchSig = sig;
  const state = !w.operator_configured ? "no operator character configured" :
    !w.enabled ? "off (watch_owner flag)" : w.engaged ? "watching a fight" : w.in_view ? "in view" : "not in view";
  $("#owner-watch-summary").textContent = `${state} · ${(w.total || 0).toLocaleString()} kept` +
    (w.shared_dropped ? ` · ${w.shared_dropped} shared dropped` : "") +
    (w.busy ? " · the database is busy; try again shortly" : "");
  const when = s => new Date(s * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const rows = (w.observations || []).map(o => {
    const row = node("div", "reading");
    const head = node("div", "reading-head");
    head.append(node("b", "", FEATURE_LABELS[o.feature] || o.feature), node("span", "", `reach ${o.reach ?? "?"}`),
      node("span", o.kills ? "flag good" : "flag", `${o.kills} killed`), node("time", "", when(o.ended)));
    row.append(head);
    const kinds = (o.kinds || []).map(k => `${k[0]}${k[1] ? ` (${k[1]})` : ""}`).join(", ");
    const parts = [`${o.peak_adjacent} next to it at once`, `${o.damage_dealt} dealt in ${o.hits_dealt} hits`,
      `${o.damage_taken} taken in ${o.hits_taken}`, `${Math.round(o.seconds)} s`];
    if (o.ranged_hits) parts.push(`${o.ranged_hits} ranged hit${o.ranged_hits === 1 ? "" : "s"}, stepped after ${o.stepped_after_ranged}`);
    if (o.hp_before != null) parts.push(`HP ${o.hp_before}% before, ${o.hp_low}% lowest, ${o.hp_after}% after`);
    if (o.gap_seconds != null) parts.push(`${Math.round(o.gap_seconds)} s since the last fight`);
    row.append(node("p", "", parts.join(" · ")));
    if (kinds) row.append(node("p", "", kinds));
    row.append(node("small", "", `${o.map} (${o.x}, ${o.y})`));
    return row;
  });
  $("#owner-watch").replaceChildren(...(rows.length ? rows : [node("span", "muted", "No fights watched yet.")]));
}

// ---------- feedback drafts (read-only) ----------
// Her feedback queue (feedback.py), built from DOM nodes and text only: a
// body is shown as the plain Markdown text that would be filed, never
// rendered. Read-only by design: the owner approves, rejects and edits with
// `atrinik-playtester feedback` on CT130, and only the separate filer files.
const FEEDBACK_KIND_LABELS = { bug: "Bug", hint: "Hint", balance: "Balance", qol: "Quality of life", clarity: "Clarity" };
const feedbackOpen = new Set();
let feedbackBusy = false, lastFeedbackSig = "";
function fillSelect(select, first, values, label) {
  const chosen = select.value, sig = values.join(",");
  if (select.dataset.sig === sig) return;
  select.replaceChildren(node("option", "", first), ...values.map(x => { const o = node("option", "", label(x)); o.value = x; return o; }));
  select.firstChild.value = "";
  select.value = values.includes(chosen) ? chosen : "";
  select.dataset.sig = sig;
}
async function pollFeedback() {
  if (feedbackBusy) return;
  feedbackBusy = true;
  try {
    const kind = $("#feedback-kind").value, repo = $("#feedback-repo").value;
    const r = await fetch(`/api/feedback?limit=200${kind ? `&kind=${encodeURIComponent(kind)}` : ""}${repo ? `&repo=${encodeURIComponent(repo)}` : ""}`, { cache: "no-store" });
    if (r.ok) renderFeedback(await r.json());
  } finally { feedbackBusy = false; }
}
function renderFeedback(f) {
  const sig = JSON.stringify(f);
  if (sig === lastFeedbackSig) return;
  lastFeedbackSig = sig;
  fillSelect($("#feedback-kind"), "All kinds", f.kinds || [], x => FEEDBACK_KIND_LABELS[x] || x);
  fillSelect($("#feedback-repo"), "All repositories", f.repos || [], x => x);
  const counts = f.counts || {}, drafts = Object.values(counts.draft || {}).reduce((a, b) => a + b, 0);
  $("#feedback-summary").textContent = `${drafts} draft${drafts === 1 ? "" : "s"} · read-only: approve, reject and edit with "atrinik-playtester feedback" on CT130; the body below is the Markdown copy to file by hand` +
    (f.busy ? " · the database is busy; try again shortly" : "");
  const day = t => new Date(t * 1000).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" });
  const rows = (f.items || []).map(x => {
    const item = node("div", "feedback-item");
    const head = node("div", "reading-head");
    head.append(node("b", "", FEEDBACK_KIND_LABELS[x.kind] || x.kind), node("strong", "", x.title),
      node("span", x.status === "draft" && !x.withheld ? "flag" : "flag warn", x.withheld ? "withheld" : x.status), node("span", "flag", `×${x.occurrences}`));
    item.append(head);
    item.append(node("small", "", [x.repo, x.short_fingerprint, `${x.days} day${x.days === 1 ? "" : "s"}`, `first ${day(x.first_seen)}`, `last ${day(x.last_seen)}`, (x.labels || []).join(", ")].filter(Boolean).join(" · ")));
    if (x.issue_url) item.append(node("small", "", `Issue: ${x.issue_url}`));
    if ((x.remote_candidates || []).length) item.append(node("small", "", `Possible duplicates: ${x.remote_candidates.join(", ")}`));
    if (x.error && !x.withheld) item.append(node("small", "", `Filer: ${x.error}`));
    const details = node("details");
    details.open = feedbackOpen.has(x.id);
    details.addEventListener("toggle", () => { if (details.open) feedbackOpen.add(x.id); else feedbackOpen.delete(x.id); });
    const body = x.withheld ? "Withheld: its text names a private term (a player, her account or the server). It is shown again only once she renders it again without one." : x.body_md;
    details.append(node("summary", "", "Body as it would be filed"), node("pre", "feedback-body", body));
    item.append(details);
    return item;
  });
  $("#feedback").replaceChildren(...(rows.length ? rows : [node("span", "muted", "No drafts.")]));
}
$("#feedback-kind").onchange = pollFeedback;
$("#feedback-repo").onchange = pollFeedback;

// ---------- polling ----------
let liveBusy = false, slowBusy = false;
async function pollLive() {
  if (liveBusy) return;
  liveBusy = true;
  try {
    const r = await fetch(`/api/state?include=${LIVE_KEYS}`, { cache: "no-store" });
    if (!r.ok) throw Error(`${r.status} ${r.statusText}`);
    live = await r.json();
    renderHeader(); renderAlerts(); renderVitals(); renderTask(); renderCharacter(); drawMap(); renderGround(); renderInventory(); renderChat(); renderSlowLists();
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
    if (r.ok) { slow = await r.json(); renderBuild(); renderFarming(); renderDeaths(); renderTrends(); renderSlowLists(); renderEquipment(); renderUnlocks(); renderVoyages(); renderBlocks(); renderLearned(); renderShadow(); if ($("#tab-activity").classList.contains("active")) renderActivity(); }
    if ($("#tab-knowledge").classList.contains("active")) pollKnowledge();
    if ($("#tab-farming").classList.contains("active")) { pollTerrain(); pollOwnerWatch(); }
    if ($("#tab-feedback").classList.contains("active")) pollFeedback();
  } finally { slowBusy = false; }
}
pollLive(); pollSlow();
setInterval(pollLive, 1000);
setInterval(pollSlow, 5000);
