const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let state = null;
let clips = [];
let filter = localStorage.getItem("filter") || "all";
let lastJobsSig = "";
let lastClipsSig = "";
const cardEls = new Map();

const SETTINGS_FIELDS = [
  ["clips_per_video", "Clipuri / video normal", 1],
  ["clips_per_stream", "Clipuri / stream lung (VOD)", 1],
  ["max_clip_seconds", "Durată maximă clip (s, max 40)", 1],
  ["min_clip_seconds", "Durată minimă clip (s)", 1],
  ["live_sensitivity", "Sensibilitate live (0.3 – 5)", 0.1],
  ["live_cooldown_seconds", "Pauză între clipuri live (s)", 5],
  ["live_max_clips_per_hour", "Max clipuri live / oră", 1],
  ["live_check_interval_seconds", "Verificare live la fiecare (s)", 10],
  ["scan_interval_minutes", "Auto video noi la fiecare (min)", 5],
  ["videos_per_scan", "Video-uri noi / platformă / scanare", 1],
  ["max_video_age_days", "Ignoră video-uri mai vechi de (zile)", 1],
  ["skip_videos_shorter_than", "Ignoră video-uri sub (s)", 10],
  ["youtube_chat_replay_max_hours", "Chat replay YouTube pt. VOD-uri până la (ore)", 1],
  ["output_fps", "FPS clip final", 1],
];

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}
function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "acum";
  if (d < 3600) return `acum ${Math.floor(d / 60)} min`;
  if (d < 86400) return `acum ${Math.floor(d / 3600)} h`;
  return new Date(ts * 1000).toLocaleDateString("ro-RO");
}
function initials(name) {
  const words = name.trim().split(/\s+/);
  if (words.length > 1) return words.map((w) => w[0]).join("").slice(0, 2).toUpperCase();
  const caps = name.replace(/[^A-Z]/g, "");
  return (caps.length >= 2 ? caps : name).slice(0, 2).toUpperCase();
}
function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}
function openModal(id) { $("#" + id).classList.add("open"); }
function closeModal(id) { $("#" + id).classList.remove("open"); }
function ytLink(y) {
  if (!y) return null;
  if (y.startsWith("http")) return y;
  if (y.startsWith("UC") && y.length === 24) return `https://www.youtube.com/channel/${y}`;
  return `https://www.youtube.com/${y.startsWith("@") ? y : "@" + y}`;
}

// ------------------------------------------------------------------ sidebar & sections

let lastNavSig = "";
function renderNav() {
  const items = [{ id: "all", name: "Toți streamerii", color: "#e9ecf2", clips: clips.length }].concat(state.streamers);
  const w = state.whisper || {};
  const sig = JSON.stringify([filter, w.device, items.map((s) => [s.id, s.name, s.color, s.clips,
    !!(s.live && (s.live.live.kick || s.live.live.youtube))])]);
  if (sig === lastNavSig) return;
  lastNavSig = sig;
  $("#nav").innerHTML = items.map((s) => {
    const live = s.live && (s.live.live.kick || s.live.live.youtube);
    return `<div class="nav-item ${filter === s.id ? "active" : ""}" data-filter="${esc(s.id)}" style="--c:${esc(s.color)}">
      <span class="dot"></span><span>${esc(s.name)}</span>${live ? '<span class="live-dot" title="LIVE acum"></span>' : ""}
      <span class="count">${s.clips ?? 0}</span></div>`;
  }).join("");
  $("#sysInfo").innerHTML = w.device
    ? `Transcriere: <b>${esc(w.model)}</b> pe <b>${w.device === "cuda" ? "GPU" : "CPU"}</b>`
    : "Se încarcă modelul de transcriere…";
}

function ensureSection(s) {
  let sec = $(`#sec-${CSS.escape(s.id)}`);
  if (sec) return sec;
  sec = document.createElement("section");
  sec.className = "streamer";
  sec.id = `sec-${s.id}`;
  sec.dataset.id = s.id;
  sec.innerHTML = `
    <div class="s-head">
      <div class="avatar"></div>
      <div class="s-title">
        <h2><span data-name></span> <button class="btn ghost sm" data-act="edit" title="Editează">✎</button></h2>
        <div class="links" data-links></div>
        <div class="badges" data-badges></div>
      </div>
      <div class="s-actions">
        <label class="switch" title="Când streamerul intră live, botul înregistrează și face clipuri automat">
          <input type="checkbox" data-watch><span class="slider"></span> Clipping live
        </label>
        <button class="btn ghost" data-act="videos">🔎 Video recente</button>
        <button class="btn" data-act="scan">⚡ Procesează cele mai noi</button>
      </div>
    </div>
    <div class="monitor" data-monitor></div>
    <form class="url-form" data-url>
      <input placeholder="Lipește un link YouTube sau Kick VOD (kick.com/canal/videos/...)" required>
      <button class="btn" type="submit">✂️ Fă clipuri</button>
    </form>
    <div class="grid" data-grid></div>
    <div class="empty" data-empty>Încă nu există clipuri. Apasă „Procesează cele mai noi” sau pornește „Clipping live”.</div>`;
  $("#main").appendChild(sec);
  return sec;
}

function renderSections() {
  const ids = new Set(state.streamers.map((s) => s.id));
  $$("#main > section.streamer").forEach((sec) => { if (!ids.has(sec.dataset.id)) sec.remove(); });
  for (const s of state.streamers) {
    const sec = ensureSection(s);
    sec.style.setProperty("--c", s.color || "#53fc18");
    sec.hidden = filter !== "all" && filter !== s.id;
    $(".avatar", sec).textContent = initials(s.name);
    $("[data-name]", sec).textContent = s.name;
    const yt = ytLink(s.youtube);
    $("[data-links]", sec).innerHTML = [
      yt ? `<a href="${esc(yt)}" target="_blank">▶ YouTube</a>` : "",
      s.kick ? `<a href="https://kick.com/${esc(s.kick)}" target="_blank">🟩 kick.com/${esc(s.kick)}</a>` : "",
      `<span>${s.clips || 0} clipuri</span>`,
    ].join("");
    const lv = (s.live && s.live.live) || {};
    const badges = [];
    for (const p of ["kick", "youtube"]) {
      if (lv[p]) {
        const v = lv[p].viewers ? ` · ${Number(lv[p].viewers).toLocaleString("ro-RO")} viewers` : "";
        badges.push(`<a class="badge live" href="${esc(lv[p].url)}" target="_blank" title="${esc(lv[p].title)}">🔴 LIVE pe ${p === "kick" ? "Kick" : "YouTube"}${v}</a>`);
      }
    }
    if (!badges.length) badges.push(`<span class="badge">offline${s.live && s.live.checked ? " · verificat " + ago(s.live.checked) : ""}</span>`);
    $("[data-badges]", sec).innerHTML = badges.join("");
    const watch = $("[data-watch]", sec);
    if (document.activeElement !== watch) watch.checked = !!(s.live && s.live.watching);
    watch.disabled = !!state.settings.auto_live;
    const mons = (s.live && s.live.monitors) || [];
    $("[data-monitor]", sec).innerHTML = mons.map((m) => `
      <div class="m">🎥 <b>${m.platform === "kick" ? "Kick" : "YouTube"}</b> · ${esc(m.status)} · înregistrat ${fmtTime(m.recorded)}
        · chat ${m.chat_per_min}/min · hype ${m.score} / prag ${m.threshold ?? "–"} · <b>${m.clips}</b> clipuri</div>`).join("")
      || (s.live && s.live.watching ? `<div class="m">👀 Aștept să intre live…</div>` : "");
  }
  // keep section order identical to config (only touch the DOM if it differs, moving would pause videos)
  const current = $$("#main > section.streamer").map((x) => x.dataset.id).join(",");
  if (current !== state.streamers.map((s) => s.id).join(",")) {
    state.streamers.forEach((s) => $("#main").appendChild($(`#sec-${CSS.escape(s.id)}`)));
  }
}

// ------------------------------------------------------------------ clips

function fileName(c, ext) {
  const base = (c.title || c.id).replace(/[“”"]/g, "").replace(/[^\p{L}\p{N} _-]+/gu, "").trim().slice(0, 60) || c.id;
  return `${base}.${ext}`;
}

function clipCard(c) {
  const el = document.createElement("article");
  el.className = "card";
  el.dataset.id = c.id;
  el.dataset.v = c.v || 0;
  const live = c.source_kind === "live";
  const platform = c.platform === "kick" ? "Kick" : "YouTube";
  const reasons = (c.reasons || []).map((r) => `<span class="chip">${esc(r)}</span>`).join("");
  const kw = ((c.effects && c.effects.keywords) || []).map((k) => k.replace(/[.,!?]+$/, ""));
  const keywords = kw.length ? `<div class="meta">🎯 Accent pe: <b>${esc([...new Set(kw)].slice(0, 8).join(", "))}</b></div>` : "";
  const v = c.v ? `?v=${c.v}` : "";
  el.innerHTML = `
    <h3 class="ctitle">${esc(c.title)}</h3>
    <div class="vid">
      <video src="/media/${esc(c.file)}${v}" poster="/media/${esc(c.thumb)}${v}" preload="none" controls playsinline></video>
      <span class="tag ${live ? "live" : ""}">${live ? "🔴 LIVE" : "VOD"} · ${platform}</span>
      <span class="dur">${fmtTime(c.duration)}</span>
    </div>
    <div class="body">
      <div class="meta" title="${esc(c.source_title)}">${esc(String(c.source_title || "").replace(/\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/, ""))}</div>
      <div class="meta"><a href="${esc(c.source_url)}" target="_blank">↗ Sursa${live ? "" : " @ " + fmtTime(c.start)}</a> · ${ago(c.created_at)}</div>
      <div class="chips">${reasons}</div>
      ${keywords}
      <div class="score">Scor interes: <b>${Number(c.score).toFixed(1)}</b></div>
      <details><summary>📝 Transcript</summary><p>${esc(c.transcript)}</p></details>
      <div class="cactions">
        <a class="btn sm" href="/media/${esc(c.file)}${v}" download="${esc(fileName(c, "mp4"))}">⬇ MP4</a>
        <a class="btn sm ghost" href="/media/${esc(c.srt)}${v}" download="${esc(fileName(c, "srt"))}">⬇ SRT</a>
        <button class="btn sm ghost" data-copy>📋 Copiază text</button>
        <button class="btn sm danger ghost" data-del title="Șterge clipul">🗑</button>
      </div>
    </div>`;
  $("[data-copy]", el).onclick = async () => {
    await navigator.clipboard.writeText(`${c.title}\n\n${c.transcript}`);
    toast("Transcript copiat");
  };
  $("[data-del]", el).onclick = async () => {
    if (!confirm("Ștergi clipul definitiv?")) return;
    await api(`/api/clips/${encodeURIComponent(c.id)}`, { method: "DELETE" });
    clips = clips.filter((x) => x.id !== c.id);
    renderClips();
    refreshState();
  };
  return el;
}

function renderClips() {
  const byStreamer = {};
  for (const c of clips) (byStreamer[c.streamer] ||= []).push(c);
  const alive = new Set(clips.map((c) => c.id));
  for (const [id, el] of cardEls) if (!alive.has(id)) { el.remove(); cardEls.delete(id); }
  for (const sec of $$("#main > section.streamer")) {
    const list = (byStreamer[sec.dataset.id] || []).sort((a, b) => b.created_at - a.created_at);
    const grid = $("[data-grid]", sec);
    // Insert only new cards so videos that are playing are not interrupted.
    for (let i = list.length - 1; i >= 0; i--) {
      const c = list[i];
      const old = cardEls.get(c.id);
      if (old) {
        const playing = old.querySelector("video") && !old.querySelector("video").paused;
        if (String(c.v || 0) === old.dataset.v || playing) continue;
        const fresh = clipCard(c);
        old.replaceWith(fresh);
        cardEls.set(c.id, fresh);
        continue;
      }
      const el = clipCard(c);
      cardEls.set(c.id, el);
      const after = list.slice(i + 1).map((x) => cardEls.get(x.id)).find((e) => e && e.parentNode === grid);
      if (after) grid.insertBefore(el, after);
      else grid.appendChild(el);
    }
    $("[data-empty]", sec).hidden = list.length > 0;
  }
}

// ------------------------------------------------------------------ jobs

function renderJobs() {
  const jobs = state.jobs || [];
  const active = jobs.filter((j) => j.status === "queued" || j.status === "running");
  $("#jobsCount").textContent = active.length;
  const sig = JSON.stringify(jobs.map((j) => [j.id, j.status, j.progress, j.message]));
  if (sig === lastJobsSig) return;
  const finishedNow = lastJobsSig && jobs.some((j) => j.status === "done" && !lastJobsSig.includes(`"${j.id}","done"`));
  lastJobsSig = sig;
  const open = new Set($$("#jobsList details[open]").map((d) => d.dataset.id));
  $("#jobsList").innerHTML = jobs.length ? jobs.map((j) => `
    <div class="job">
      <div class="row"><span class="label" title="${esc(j.label)}">${esc(j.label)}</span><span class="st ${j.status}">${statusRo(j.status)}</span>
        ${j.status === "queued" || j.status === "running" ? `<button class="btn ghost sm" data-cancel="${j.id}">Anulează</button>` : ""}</div>
      <div class="bar"><i style="width:${Math.round(j.progress * 100)}%"></i></div>
      <div class="msg">${esc(j.message)}</div>
      <details data-id="${j.id}" ${open.has(j.id) ? "open" : ""}><summary>Log</summary><pre>${esc((j.logs || []).join("\n"))}</pre></details>
    </div>`).join("") : `<div class="empty">Nicio activitate încă.</div>`;
  $$("[data-cancel]").forEach((b) => (b.onclick = async () => { await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }); refreshState(); }));
  if (finishedNow) loadClips();
}
function statusRo(s) {
  return {
    queued: "în coadă", running: "rulează", done: "gata", error: "eroare", cancelled: "anulat",
    processing: "se procesează", skipped: "sărit (prea vechi)",
  }[s] || s;
}

// ------------------------------------------------------------------ videos modal

async function showVideos(sid) {
  const s = state.streamers.find((x) => x.id === sid);
  $("#videosTitle").textContent = `Videoclipuri recente – ${s.name}`;
  $("#videosBody").innerHTML = `<div class="spinner"></div><p class="muted" style="text-align:center">Citesc YouTube + Kick…</p>`;
  openModal("videosModal");
  try {
    const items = await api(`/api/streamers/${encodeURIComponent(sid)}/videos`);
    if (!items.length) { $("#videosBody").innerHTML = `<div class="empty">Nu am găsit videoclipuri.</div>`; return; }
    $("#videosBody").innerHTML = items.map((v, i) => {
      const kind = v.platform === "kick" ? "Kick VOD" : v.kind === "stream" ? "YouTube Live (reluare)" : "YouTube Video";
      const st = v.status === "done" ? `✔ procesat · ${v.clips} clipuri` : v.status ? statusRo(v.status) : "neprocesat";
      return `<div class="vrow">
        <img src="${esc(v.thumbnail || "")}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">
        <div class="info"><b title="${esc(v.title)}">${esc(v.title || "(fără titlu)")}</b>
          <small><span class="badge ${v.platform === "kick" ? "kick" : "yt"}">${kind}</span> · ${v.duration ? fmtTime(v.duration) : "?"} · ${esc(st)}</small></div>
        <a class="btn ghost sm" href="${esc(v.url)}" target="_blank">↗</a>
        <button class="btn sm" data-i="${i}">✂️ ${v.status === "done" ? "Refă" : "Fă clipuri"}</button>
      </div>`;
    }).join("");
    $$("#videosBody [data-i]").forEach((b) => (b.onclick = async () => {
      const v = items[+b.dataset.i];
      try {
        await api("/api/process", { method: "POST", body: { streamer: sid, url: v.url } });
        b.disabled = true; b.textContent = "În coadă ✓";
        toast("Am pus video-ul în coadă");
        refreshState();
      } catch (e) { toast(e.message, true); }
    }));
  } catch (e) {
    $("#videosBody").innerHTML = `<div class="empty">Eroare: ${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ settings & streamers

function showSettings() {
  const s = state.settings;
  $("#settingsForm").innerHTML = SETTINGS_FIELDS.map(([k, label, step]) =>
    `<label>${esc(label)}<input name="${k}" type="number" step="${step}" value="${esc(s[k])}"></label>`).join("")
    + `<p class="hint">Sensibilitate live mai mare = mai multe clipuri (prag mai mic). Clipurile au mereu maximum 40 de secunde.</p>`;
  openModal("settingsModal");
}

function showStreamer(sid) {
  const f = $("#streamerForm");
  const s = state.streamers.find((x) => x.id === sid) || { id: "", name: "", youtube: "", kick: "", language: "en", color: "#53fc18", crop_sides: 0, layout: "auto" };
  for (const k of ["id", "name", "youtube", "kick", "language", "color", "crop_sides", "layout"]) f.elements[k].value = s[k] ?? (k === "layout" ? "auto" : "");
  $("#streamerModalTitle").textContent = sid ? `Editează ${s.name}` : "Adaugă streamer";
  $("#deleteStreamer").hidden = !sid;
  openModal("streamerModal");
}

// ------------------------------------------------------------------ data loading

async function refreshState() {
  try {
    state = await api("/api/state");
    $("#autoScan").checked = !!state.settings.auto_scan;
    $("#autoLive").checked = !!state.settings.auto_live;
    renderNav();
    renderSections();
    renderJobs();
    if (!cardEls.size && clips.length) renderClips();
  } catch (e) {
    console.error(e);
  }
}
async function loadClips() {
  try {
    const data = await api("/api/clips?limit=600");
    const sig = data.map((c) => `${c.id}:${c.v || 0}`).join(",");
    clips = data;
    if (sig !== lastClipsSig) { lastClipsSig = sig; renderClips(); renderNav(); }
  } catch (e) { console.error(e); }
}

// ------------------------------------------------------------------ events

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-filter],[data-act],[data-close]");
  if (!t) return;
  if (t.dataset.close) { closeModal(t.dataset.close); return; }
  if (t.dataset.filter) {
    filter = t.dataset.filter;
    localStorage.setItem("filter", filter);
    renderNav(); renderSections();
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  const sid = t.closest("section.streamer")?.dataset.id;
  if (t.dataset.act === "videos") return showVideos(sid);
  if (t.dataset.act === "edit") return showStreamer(sid);
  if (t.dataset.act === "scan") {
    t.disabled = true;
    try {
      const r = await api(`/api/streamers/${encodeURIComponent(sid)}/scan`, { method: "POST" });
      toast(r.queued ? `Am pus ${r.queued} video-uri noi în coadă` : "Nu există video-uri noi neprocesate");
      refreshState();
    } catch (e) { toast(e.message, true); }
    t.disabled = false;
  }
});

document.addEventListener("change", async (ev) => {
  const t = ev.target;
  if (t.matches("[data-watch]")) {
    const sid = t.closest("section.streamer").dataset.id;
    await api(`/api/streamers/${encodeURIComponent(sid)}/watch`, { method: "POST", body: { on: t.checked } });
    toast(t.checked ? "Clipping live pornit – aștept/înregistrez live-ul" : "Clipping live oprit");
    refreshState();
  }
  if (t.id === "autoScan" || t.id === "autoLive") {
    await api("/api/settings", { method: "POST", body: { [t.id === "autoScan" ? "auto_scan" : "auto_live"]: t.checked } });
    refreshState();
  }
});

document.addEventListener("submit", async (ev) => {
  const form = ev.target.closest("form[data-url]");
  if (!form) return;
  ev.preventDefault();
  const sid = form.closest("section.streamer").dataset.id;
  const input = $("input", form);
  try {
    await api("/api/process", { method: "POST", body: { streamer: sid, url: input.value.trim() } });
    input.value = "";
    toast("Am pus video-ul în coadă – vezi „Activitate”");
    refreshState();
  } catch (e) { toast(e.message, true); }
});

$("#btnJobs").onclick = () => $("#jobsDrawer").classList.toggle("open");
$("#btnSettings").onclick = showSettings;
$("#btnShare").onclick = () => { openModal("shareModal"); refreshShare(); };

// ------------------------------------------------------------------ sharing

let shareTimer = null;
function renderShare(s) {
  $("#shareLan").checked = !!s.lan;
  $("#shareNet").checked = !!s.internet;
  const link = (u) => `<div class="share-url"><a href="${esc(u)}" target="_blank">${esc(u)}</a>
    <button class="btn sm ghost" data-copyurl="${esc(u)}">📋 Copiază</button></div>`;
  let lan = "";
  if (s.lan) {
    lan = s.lan_urls && s.lan_urls.length ? s.lan_urls.map(link).join("")
      : `<div class="muted">${esc(s.error || "Pornesc galeria…")}</div>`;
  }
  $("#shareLanUrls").innerHTML = lan;
  let net = "";
  if (s.internet) {
    net = s.public_url ? link(s.public_url) : `<div class="muted">⏳ ${esc(s.tunnel_status || "pornesc…")}</div>`;
  }
  $("#shareNetUrl").innerHTML = net;
  $("#shareLocal").innerHTML = s.local_url ? `Previzualizare pe PC-ul tău: ${link(s.local_url)}` : "";
  $$("[data-copyurl]").forEach((b) => (b.onclick = async () => { await navigator.clipboard.writeText(b.dataset.copyurl); toast("Link copiat"); }));
  $("#btnShare").classList.toggle("on", !!(s.lan || s.internet));
}
async function refreshShare() {
  try { renderShare(await api("/api/share")); } catch (e) { console.error(e); }
  clearTimeout(shareTimer);
  if ($("#shareModal").classList.contains("open")) shareTimer = setTimeout(refreshShare, 2500);
}
for (const id of ["shareLan", "shareNet"]) {
  $("#" + id).addEventListener("change", async () => {
    const body = id === "shareLan" ? { lan: $("#shareLan").checked } : { internet: $("#shareNet").checked };
    try { renderShare(await api("/api/share", { method: "POST", body })); } catch (e) { toast(e.message, true); }
    refreshShare();
  });
}
$("#btnAddStreamer").onclick = () => showStreamer("");
$("#btnCheckLive").onclick = async () => { await api("/api/live/check", { method: "POST" }); toast("Verific cine e live…"); setTimeout(refreshState, 6000); };
$("#saveSettings").onclick = async () => {
  const body = {};
  for (const el of $("#settingsForm").elements) if (el.name) body[el.name] = Number(el.value);
  try { await api("/api/settings", { method: "POST", body }); closeModal("settingsModal"); toast("Setări salvate"); refreshState(); }
  catch (e) { toast(e.message, true); }
};
$("#saveStreamer").onclick = async () => {
  const f = $("#streamerForm");
  if (!f.reportValidity()) return;
  const body = Object.fromEntries(new FormData(f));
  body.crop_sides = Number(body.crop_sides || 0);
  if (!body.id) delete body.id;
  try { await api("/api/streamers", { method: "POST", body }); closeModal("streamerModal"); toast("Streamer salvat"); refreshState(); }
  catch (e) { toast(e.message, true); }
};
$("#deleteStreamer").onclick = async () => {
  const id = $("#streamerForm").elements.id.value;
  if (!id || !confirm("Ștergi streamerul? (clipurile rămân pe disc)")) return;
  await api(`/api/streamers/${encodeURIComponent(id)}`, { method: "DELETE" });
  closeModal("streamerModal");
  refreshState();
};
$$(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) m.classList.remove("open"); }));

(async function init() {
  await refreshState();
  await loadClips();
  setInterval(refreshState, 3000);
  setInterval(loadClips, 10000);
})();
