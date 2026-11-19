const $ = (s, el = document) => el.querySelector(s);
const PAGE = 24;

let streamers = [];
let clips = [];
let filter = new URLSearchParams(location.search).get("s") || "all";
let shown = PAGE;
let lastSig = "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}
function ago(ts) {
  const d = Date.now() / 1000 - ts;
  if (d < 3600) return `acum ${Math.max(1, Math.floor(d / 60))} min`;
  if (d < 86400) return `acum ${Math.floor(d / 3600)} h`;
  return new Date(ts * 1000).toLocaleDateString("ro-RO");
}
function fileName(c, ext) {
  const base = (c.title || c.id).replace(/[“”"]/g, "").replace(/[^\p{L}\p{N} _-]+/gu, "").trim().slice(0, 60) || c.id;
  return `${base}.${ext}`;
}
const cleanSrc = (t) => String(t || "").replace(/\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/, "");
function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), 3000);
}
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
}

function renderTabs() {
  const total = streamers.reduce((a, s) => a + s.clips, 0);
  const items = [{ id: "all", name: "Toți", color: "#e9ecf2", clips: total }].concat(streamers.filter((s) => s.clips > 0));
  $("#tabs").innerHTML = items.map((s) => `
    <button class="tab ${filter === s.id ? "active" : ""}" data-s="${esc(s.id)}" style="--c:${esc(s.color)}">
      <span class="dot"></span>${esc(s.name)} <span class="n">${s.clips}</span></button>`).join("");
}

function card(c) {
  const s = streamers.find((x) => x.id === c.streamer) || { name: c.streamer, color: "#53fc18" };
  const live = c.source_kind === "live";
  const v = `?v=${c.v}`;
  const kw = [...new Set((c.keywords || []).map((k) => k.replace(/[.,!?]+$/, "")))].slice(0, 6);
  return `
  <article class="card" style="--c:${esc(s.color)}">
    <h3 class="ctitle">${esc(c.title)}</h3>
    <div class="vid">
      <video src="/media/${esc(c.file)}${v}" poster="/media/${esc(c.thumb)}${v}" preload="none" controls playsinline></video>
      <span class="tag ${live ? "live" : ""}">${live ? "🔴 LIVE" : "VOD"} · ${c.platform === "kick" ? "Kick" : "YouTube"}</span>
      <span class="dur">${fmtTime(c.duration)}</span>
    </div>
    <div class="body">
      <div class="who">● ${esc(s.name)}</div>
      <div class="meta">${c.source_url ? `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">↗ ${esc(cleanSrc(c.source_title) || "Sursa")}</a>` : esc(cleanSrc(c.source_title))}</div>
      <div class="meta">${ago(c.created_at)}${kw.length ? ` · 🎯 ${esc(kw.join(", "))}` : ""}</div>
      <details><summary>📝 Transcript</summary><p>${esc(c.transcript)}</p></details>
      <div class="cactions">
        <a class="btn sm" href="/media/${esc(c.file)}${v}" download="${esc(fileName(c, "mp4"))}">⬇ Descarcă MP4</a>
        <a class="btn sm ghost" href="/media/${esc(c.srt)}${v}" download="${esc(fileName(c, "srt"))}">⬇ SRT</a>
        <button class="btn sm ghost" data-copy="${esc(c.id)}">📋 Titlu + text</button>
      </div>
    </div>
  </article>`;
}

function visible() {
  const q = $("#q").value.trim().toLowerCase();
  return clips.filter((c) => (filter === "all" || c.streamer === filter)
    && (!q || `${c.title} ${c.transcript} ${c.source_title}`.toLowerCase().includes(q)));
}

function render() {
  const list = visible();
  const sig = JSON.stringify([filter, $("#q").value, shown, list.map((c) => `${c.id}:${c.v}`)]);
  if (sig === lastSig) return;
  // Don't rebuild the grid under someone who is watching a clip.
  if ([...document.querySelectorAll("video")].some((v) => !v.paused)) return;
  lastSig = sig;
  $("#grid").innerHTML = list.slice(0, shown).map(card).join("");
  $("#empty").hidden = list.length > 0;
  $("#more").hidden = list.length <= shown;
}

async function load() {
  try {
    const [s, c] = await Promise.all([fetch("/api/streamers").then((r) => r.json()), fetch("/api/clips").then((r) => r.json())]);
    streamers = s;
    clips = c;
    renderTabs();
    render();
  } catch (e) {
    console.error(e);
  }
}

document.addEventListener("click", async (ev) => {
  const tab = ev.target.closest("[data-s]");
  if (tab) {
    filter = tab.dataset.s;
    shown = PAGE;
    history.replaceState(null, "", filter === "all" ? location.pathname : `?s=${encodeURIComponent(filter)}`);
    renderTabs();
    lastSig = "";
    render();
    return;
  }
  const cp = ev.target.closest("[data-copy]");
  if (cp) {
    const c = clips.find((x) => x.id === cp.dataset.copy);
    if (c) {
      await copyText(`${c.title}\n\n${c.transcript}`);
      toast("Copiat");
    }
  }
});
$("#more").onclick = () => { shown += PAGE; render(); };
$("#q").addEventListener("input", () => { shown = PAGE; lastSig = ""; render(); });

load();
setInterval(load, 30000);
