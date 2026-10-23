"use strict";
// Atrinik observer dashboard. Polls a small live slice of /api/state every
// second and the heavy lists less often; renders the ground-level map from the
// runtime face images, statically (no animation), like the graphical client.

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clean = v => String(v ?? "").replace(/\[\/?[a-z]+(=[^\]]*)?\]/gi, "");

const LIVE_KEYS = "phase,connected,character,stats,level_progress,target,current_action,task,last_task,map,inventory,inventory_ready,ground,messages,interface,economy,conditions,protections,skills,party,alerts";
const SLOW_KEYS = "build,farming_analysis,post_mortems,trends,decisions,actions,quests,quest_journal,farm_spots,navigation_spots";
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
function renderFarming() {
  const fa = slow.farming_analysis;
  if (!fa) return;
  const sig = JSON.stringify([fa.status, fa.objective, fa.selected_objective, fa.objective_override, fa.finished_at, fa.error, fa.running, fa.self_improvement, fa.savebed]);
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
    : "");
  $("#fa-ranking").innerHTML = (fa.ranking || []).map((r, i) => `
    <div class="spot${i === 0 ? " top" : ""}">
      <div class="spot-head"><b>#${i + 1} ${esc(r.name)}</b><span class="flag">${esc(String(r.region || "").replace(/_/g, " "))}</span></div>
      <small>${esc(r.location || (r.map_paths || []).join(", "))}</small>
      <div class="spot-metrics"><span title="Estimated XP per hour">XP ${rate(r.xp_hour)}/h</span><span title="Estimated money per hour">${coins(r.money_hour, 2)}/h</span><span title="Estimated loot value per hour">Loot ${rate(r.loot_hour)}/h</span><span title="Estimated incoming damage per clear">Danger ${Math.round(+r.danger || 0)}</span></div>
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
  $("#wealth").textContent = `Carried ${coins(e.carried)} · Bank ${e.bank_known ? coins(e.banked) : "unknown"}`;
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
    if (r.ok) { slow = await r.json(); renderBuild(); renderFarming(); renderDeaths(); renderTrends(); renderSlowLists(); if ($("#tab-activity").classList.contains("active")) renderActivity(); }
  } finally { slowBusy = false; }
}
pollLive(); pollSlow();
setInterval(pollLive, 1000);
setInterval(pollSlow, 5000);
