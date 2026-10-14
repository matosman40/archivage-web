"use strict";
// onefichier.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Onglet 1fichier : bibliothèque des films sauvegardés
// ---------------------------------------------------------------------

const OF_SORTS = {
  title: { dir: "asc", value: (e) => (ofTitle(e) || "").toLowerCase() },
  year: { dir: "desc", value: (e) => ofYear(e) || 0 },
  rating: { dir: "desc", value: (e) => ofMeta(e).rating || 0 },
  runtime: { dir: "desc", value: (e) => ofMeta(e).runtime || 0 },
  size: { dir: "desc", value: (e) => e.remote_size || e.size || 0 },
  sent: { dir: "desc", value: (e) => e.archived_at || e.created_at || 0 },
};

const OF_KIND = { film: "Film", serie: "Série", ebook: "Ebook" };
const OF_RESTORE_LABEL = { film: "ce film", serie: "cette série", ebook: "ce dossier d'ebooks" };

function ofKind(e) {
  return e.kind || "film";
}

function ofMeta(e) {
  return e.meta && !e.meta.unavailable ? e.meta : {};
}

function ofTitle(e) {
  const base = ofMeta(e).title || e.title || e.key;
  return e.season !== null && e.season !== undefined ? `${base} · Saison ${e.season}` : base;
}

function ofYear(e) {
  return e.year || ofMeta(e).year || "";
}

function ofStatus(e) {
  if (e.active) return { label: "Envoi en cours", cls: "info" };
  if (e.status !== "done") return { label: "Envoi incomplet", cls: "warn" };
  if (e.restored_at) return { label: "Restauré", cls: "info" };
  if (e.link_ok === false) return { label: "Lien introuvable", cls: "warn" };
  return { label: "Sur 1fichier", cls: "ok" };
}

function ofDoneEntries() {
  return state.of.entries.filter((e) => e.status === "done");
}

function ofPendingEntries() {
  return state.of.entries.filter((e) => e.status !== "done" || e.active);
}

function ofFiltered() {
  const q = $("of-search").value.trim().toLowerCase();
  const status = $("of-status").value;
  const genre = $("of-genre").value;
  const kind = $("of-kind").value;
  const tmdb = $("of-tmdb").value;
  let list = ofDoneEntries().filter((e) => {
    const m = ofMeta(e);
    if (kind !== "all" && ofKind(e) !== kind) return false;
    if (status === "restored" && !e.restored_at) return false;
    if (genre && !(m.genres || []).includes(genre)) return false;
    if (!tmdbPass(tmdb, hasTmdb(ofKind(e), e.tmdb_id))) return false;
    if (q) {
      const hay = [ofTitle(e), m.original_title, m.author, String(ofYear(e))].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  if (status === "pending") return [];
  const sort = OF_SORTS[$("of-sort").value] || OF_SORTS.title;
  const dir = state.of.dir;
  list = list.sort((a, b) => {
    const va = sort.value(a), vb = sort.value(b);
    const cmp = va < vb ? -1 : va > vb ? 1 : ofTitle(a).localeCompare(ofTitle(b), "fr");
    return dir === "asc" ? cmp : -cmp;
  });
  return list;
}

function ofSubline(e) {
  const m = ofMeta(e);
  if (ofKind(e) === "ebook") {
    return [m.author, m.books ? `${m.books} livre${m.books > 1 ? "s" : ""}` : null, (m.formats || []).join(" / ") || null];
  }
  if (ofKind(e) === "serie" && e.season !== null && e.season !== undefined) {
    return [`Saison ${e.season}`, e.episodes ? `${e.episodes} épisode${e.episodes > 1 ? "s" : ""}` : null,
            formatSize(e.remote_size || e.size)];
  }
  if (ofKind(e) === "serie") {
    return [ofYear(e), m.seasons ? `${m.seasons} saison${m.seasons > 1 ? "s" : ""}` : null,
            m.episodes ? `${m.episodes} épisodes` : null];
  }
  return [ofYear(e), m.runtime ? fmtDuration(m.runtime) : null];
}

function ofCardHtml(e) {
  const m = ofMeta(e);
  const kind = ofKind(e);
  const title = ofTitle(e);
  const year = ofYear(e);
  const st = ofStatus(e);
  const hue = hueOf(title);
  const img = m.poster_url ? `<img class="of-img" src="${esc(m.poster_url)}" alt="" loading="lazy" decoding="async">` : "";
  const meta = ofSubline(e).filter(Boolean).map(esc).join(" · ");
  const genres = (m.genres || []).slice(0, 2).map((g) => `<span class="chip">${esc(g)}</span>`).join("");
  return `
    <button type="button" class="of-card" data-of-open="${esc(e.key)}" aria-label="${esc(title)}, ${esc(OF_KIND[kind])}${year ? `, ${esc(year)}` : ""}">
      <span class="of-poster" style="background: linear-gradient(150deg, hsl(${hue} 40% 30%), hsl(${(hue + 50) % 360} 40% 10%))">
        ${img}
        <span class="of-initial" aria-hidden="true">${esc(title.charAt(0).toUpperCase())}</span>
        <span class="of-kind of-kind-${kind}">${esc(OF_KIND[kind])}</span>
        ${m.rating ? `<span class="of-rating">${esc(fmtRating(m.rating))}</span>` : ""}
        <span class="of-badge ${st.cls}">${esc(st.label)}</span>
      </span>
      <span class="of-meta">
        <strong class="of-title">${esc(title)}</strong>
        <span class="muted small">${meta || "&nbsp;"}</span>
        <span class="of-genres">${genres}</span>
      </span>
    </button>`;
}

function fmtDateTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function fmtSeconds(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min ${String(s).padStart(2, "0")} s`;
  return `${s} s`;
}

// Envoi en cours : étape, barre, octets, vitesse, temps restant, source et destination.
function ofActiveRowHtml(e) {
  const p = e.progress || {};
  const title = ofTitle(e);
  const pct = typeof p.percent === "number" ? p.percent : null;
  const now = Date.now() / 1000;
  const elapsed = p.started_at ? now - p.started_at : null;
  const hasBytes = typeof p.bytes_done === "number" && p.bytes_total > 0;
  const remaining = hasBytes && p.speed > 0 ? (p.bytes_total - p.bytes_done) / p.speed : null;
  const parts = [];
  if (hasBytes) parts.push(`${formatSize(p.bytes_done)} / ${formatSize(p.bytes_total)}`);
  if (p.speed > 0) parts.push(`${formatSize(p.speed)}/s`);
  if (remaining !== null) parts.push(`reste environ ${fmtSeconds(remaining)}`);
  if (elapsed !== null) parts.push(`écoulé ${fmtSeconds(elapsed)}`);
  const m = ofMeta(e);
  let contents;
  if (e.season !== null && e.season !== undefined) {
    contents = `Saison ${esc(e.season)} · ${esc(e.episodes || "?")} épisode${e.episodes > 1 ? "s" : ""} · ${formatSize(e.source_size)}`;
  } else if (ofKind(e) === "serie") {
    contents = `Série entière${m.seasons ? ` · ${m.seasons} saison${m.seasons > 1 ? "s" : ""}` : ""} · ${formatSize(e.source_size)}`;
  } else {
    contents = `Taille : ${formatSize(e.source_size)}`;
  }
  return `
    <li class="of-row of-active">
      <div class="of-main">
        <strong>${esc(title)}${ofYear(e) ? ` (${esc(ofYear(e))})` : ""} <span class="muted small">· ${esc(OF_KIND[ofKind(e)])}</span></strong>
        <div class="muted small">${esc(contents)}</div>
        <div class="of-stage"><span class="chip info">${esc(p.stage || "En cours")}</span>${pct !== null ? `<span class="of-pct">${pct} %</span>` : ""}</div>
        <div class="job-bar of-active-bar${pct === null ? " is-indeterminate" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct ?? 0}">
          <span class="job-bar-fill" style="width:${pct === null ? 100 : pct}%"></span>
        </div>
        <div class="muted small">${esc(parts.join(" · ") || "Préparation de l'envoi…")}</div>
        <div class="muted small">Source : <code>${esc(p.source || e.original_path || "—")}</code></div>
        <div class="muted small">Destination : dossier 1fichier <code>${esc(e.folder)}</code>, archive <code>${esc(e.archive_name || "")}</code> · lancé ${esc(fmtDateTime(p.started_at || e.created_at))}</div>
      </div>
    </li>`;
}

function ofQueueRowHtml(q) {
  return `
    <li class="of-row of-queued">
      <div class="of-main">
        <strong>${esc(q.label)} <span class="muted small">· en attente</span></strong>
        <div class="muted small">Envoi ${esc(q.position)} sur ${esc(q.total)} de sa tâche : commence quand les précédents sont finis</div>
        <div class="muted small">Source : <code>${esc(q.source)}</code></div>
        <label class="job-prio muted small">Priorité
          <select data-job-priority="${esc(q.job_id)}" data-previous="${esc(q.priority || "normal")}">${priorityOptions(q.priority)}</select>
        </label>
      </div>
    </li>`;
}

function ofPendingRowHtml(e) {
  if (e.active) return ofActiveRowHtml(e);
  const title = ofTitle(e);
  const st = ofStatus(e);
  const actions = `<button class="btn btn-ghost small" type="button" data-of-recover="${esc(e.key)}">Vérifier sur 1fichier</button>
       <button class="btn btn-ghost small" type="button" data-of-forget="${esc(e.key)}">Retirer de la liste</button>`;
  return `
    <li class="of-row">
      <div class="of-main">
        <strong>${esc(title)}${ofYear(e) ? ` (${esc(ofYear(e))})` : ""} <span class="muted small">· ${esc(OF_KIND[ofKind(e)])}</span></strong>
        <div class="muted small">dossier ${esc(e.folder)} · envoi commencé le ${esc(fmtDateTime(e.created_at))}</div>
        ${e.original_path ? `<div class="muted small">Source : <code>${esc(e.original_path)}</code></div>` : ""}
        <div class="of-chips"><span class="chip ${st.cls}">${esc(st.label)}</span></div>
        ${e.error ? `<p class="of-error small">${esc(e.error)}</p>` : ""}
      </div>
      <div class="of-actions">${actions}</div>
    </li>`;
}

function renderOfStats() {
  const done = ofDoneEntries();
  const count = (k) => done.filter((e) => ofKind(e) === k).length;
  const total = done.reduce((sum, e) => sum + (e.remote_size || e.size || 0), 0);
  const rated = done.map((e) => ofMeta(e).rating).filter((r) => r);
  const avg = rated.length ? rated.reduce((a, b) => a + b, 0) / rated.length : null;
  const restored = done.filter((e) => e.restored_at).length;
  const pending = ofPendingEntries().filter((e) => !e.active).length;
  $("of-stats").innerHTML = [
    stat("Éléments", NF0.format(done.length), `${NF0.format(restored)} restauré(s)`, null),
    stat("Répartition", `${NF0.format(count("film"))} · ${NF0.format(count("serie"))} · ${NF0.format(count("ebook"))}`,
         "films · séries · ebooks", null),
    stat("Volume", formatSize(total), "sur 1fichier", null),
    stat("Note moyenne", avg === null ? "—" : NF1.format(avg), rated.length ? `${rated.length} noté(s) (TMDB)` : "fiches TMDB absentes", null),
    stat("Envois incomplets", NF0.format(pending), pending ? "à vérifier" : "aucun", null),
  ].join("");
}

function renderOfGenres() {
  const select = $("of-genre");
  const current = select.value;
  const genres = [...new Set(ofDoneEntries().flatMap((e) => ofMeta(e).genres || []))].sort((a, b) => a.localeCompare(b, "fr"));
  paintBox(select, `<option value="">Tous les genres</option>` + genres.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join(""));
  select.value = genres.includes(current) ? current : "";
}

// Cache de travail : archives temporaires des envois et restaurations (dossier de Réglages › 1fichier).
function renderOfCache() {
  const box = $("of-cache");
  const st = state.of.staging;
  box.classList.toggle("hidden", !state.of.configured || !st);
  if (!st) return;
  const text = $("of-cache-text");
  const btn = $("of-cache-clear");
  if (!st.exists) {
    text.textContent = `Cache de travail introuvable : ${st.path}. Vérifie le montage et le dossier dans Réglages › 1fichier.`;
    btn.disabled = true;
    btn.title = "";
    return;
  }
  const free = st.free_bytes !== null ? ` · ${formatSize(st.free_bytes)} libres` : "";
  const other = st.other_count ? ` · ${NF0.format(st.other_count)} élément(s) non reconnu(s), conservé(s)` : "";
  const busy = st.busy ? " · un transfert est en cours" : "";
  text.textContent = `Cache de travail : ${NF0.format(st.work_dirs)} dossier(s) temporaire(s) · ${formatSize(st.work_bytes)} utilisés${free}${other}${busy}`;
  let reason = "";
  if (st.busy) reason = `Vidage impossible : ${st.busy}.`;
  else if (!st.work_dirs) reason = "Rien à vider.";
  btn.disabled = !!reason;
  btn.title = reason;
}

async function clearOfCache() {
  const st = state.of.staging;
  if (!st || st.busy || !st.work_dirs) return;
  const ok = window.confirm(
    `Supprimer ${st.work_dirs} dossier(s) temporaire(s) (${formatSize(st.work_bytes)}) ?\n` +
    "Ils sont recréés au prochain envoi. Les autres fichiers du dossier ne sont pas touchés.");
  if (!ok) return;
  try {
    const r = await api("/api/1fichier/staging/clear", { method: "POST" });
    toast(`Cache vidé : ${NF0.format(r.removed)} dossier(s), ${formatSize(r.freed_bytes)} libérés.`, "success");
  } catch (err) {
    toast(err.message || "Vidage impossible.", "error");
  }
  await loadOneFichierView({ silent: true });
}

function renderOfView() {
  const done = ofDoneEntries();
  const pending = ofPendingEntries();
  const total = done.reduce((sum, e) => sum + (e.remote_size || e.size || 0), 0);
  const exportWarn = done.length && state.of.indexCopies && !state.of.indexCopies.last_export_at
    ? " · ⚠ index jamais exporté hors du serveur (Réglages › 1fichier)" : "";
  const lastRun = state.of.checks && state.of.checks.last_run;
  const lostWarn = lastRun && lastRun.links_lost && lastRun.links_lost.length
    ? ` · ⚠ ${lastRun.links_lost.length} lien(s) introuvable(s) au dernier contrôle` : "";
  $("of-sub").textContent = state.of.configured
    ? `${NF0.format(done.length)} élément(s) · ${formatSize(total)} · index : ${state.of.indexFile || "—"}${state.of.metaRunning ? " · récupération des fiches TMDB en cours…" : ""}${exportWarn}${lostWarn}`
    : "1fichier n'est pas configuré.";

  renderOfStats();
  renderOfGenres();
  renderOfCache();

  const pendingBox = $("of-pending");
  const queue = state.of.queue || [];
  const showPending = (pending.length > 0 || queue.length > 0) && ["all", "pending"].includes($("of-status").value);
  const busy = pending.some((e) => e.active) || queue.length > 0;
  pendingBox.classList.toggle("hidden", !showPending);
  paintBox(pendingBox, showPending
    ? `<h3 class="of-section-title">${busy ? "Envois en cours et incomplets" : "Envois incomplets"}${queue.length ? ` <span class="muted small">· ${queue.length} en attente</span>` : ""}</h3>` +
      `<ul class="of-list">${pending.map(ofPendingRowHtml).join("")}${queue.map(ofQueueRowHtml).join("")}</ul>`
    : "");

  const grid = $("of-grid");
  const empty = $("of-empty");
  if (!state.of.configured) {
    clearBox(grid);
    empty.classList.remove("hidden");
    empty.innerHTML = `<div class="glyph" aria-hidden="true">☁</div><h3>1fichier non configuré</h3>
      <p>Ajoute ta clé API dans Réglages › 1fichier.</p>
      <button class="btn btn-primary" type="button" data-of-settings="1">Ouvrir les réglages</button>`;
    return;
  }
  const items = ofFiltered();
  paintBox(grid, items.map(ofCardHtml).join(""));
  grid.classList.toggle("is-large", items.length > LARGE_GRID);
  if (items.length === 0) {
    empty.classList.remove("hidden");
    empty.innerHTML = done.length === 0
      ? `<div class="glyph" aria-hidden="true">☁</div><h3>Rien sur 1fichier pour l'instant</h3>
         <p>Sélectionne des films, des séries ou des dossiers d'ebooks dans leur onglet, puis « Envoyer sur 1fichier » dans le panneau d'actions.</p>`
      : `<div class="glyph" aria-hidden="true">⌕</div><h3>Aucun résultat</h3><p>Modifie la recherche ou les filtres.</p>`;
  } else {
    empty.classList.add("hidden");
  }
}

const OF_TIMEOUT_MS = 15000;
let ofSeq = 0;  // numéro de la dernière lecture : une réponse plus ancienne est ignorée

// Heure courte d'un instant (en ms) : « 14:32 ».
function fmtClock(ms) {
  return new Date(ms).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

async function loadOneFichierView({ silent = false } = {}) {
  if (state.surface !== "of") return;
  clearTimeout(state.of.pollTimer);
  const seq = ++ofSeq;
  const gen = adultGen;
  if (!silent) $("of-grid").innerHTML = `<p class="muted">Chargement de la bibliothèque…</p>`;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), OF_TIMEOUT_MS);
  let failed = false;
  try {
    const data = await api("/api/1fichier", { signal: ctrl.signal });
    // Réponse remplacée par une lecture plus récente, ou d'avant un changement d'accès adulte : ignorée.
    if (seq !== ofSeq || gen !== adultGen) return;
    state.of.entries = data.entries || [];
    state.of.configured = !!data.configured;
    state.of.indexFile = data.index_file;
    state.of.indexCopies = data.index_copies || null;
    state.of.checks = data.checks || null;
    state.of.restoreDirs = data.restore_dirs || {};
    state.of.metaRunning = !!data.meta_running;
    state.of.queue = data.queue || [];
    state.of.staging = data.staging || null;
    state.of.lastOk = Date.now();
    renderOfView();
    const e = state.of.detailId ? state.of.entries.find((x) => x.key === state.of.detailId) : null;
    if (e && !$("of-drawer").classList.contains("hidden")) {
      // Fiche ouverte : redessinée seulement si ses données ont changé, et jamais pendant une saisie dedans.
      const sig = JSON.stringify({ ...e, progress: undefined });
      if (!silent || (sig !== state.of.detailSig && !$("of-drawer-body").contains(document.activeElement))) {
        renderOfDetail(e, { keepScroll: silent });
      }
    }
  } catch (err) {
    if (seq !== ofSeq || gen !== adultGen) return;
    failed = true;
    if (silent) {
      // Coupure pendant un suivi : on le dit, avec l'heure de la dernière mise à jour réussie.
      const since = state.of.lastOk ? ` · dernière mise à jour à ${fmtClock(state.of.lastOk)}` : "";
      $("of-sub").textContent = `Actualisation interrompue${since}`;
    } else {
      const message = err.name === "AbortError"
        ? "La bibliothèque ne répond pas (délai dépassé). Vérifie que le serveur tourne, puis réessaie."
        : err.message;
      $("of-grid").innerHTML = `<div class="empty"><div class="glyph" aria-hidden="true">!</div><h3>Bibliothèque indisponible</h3>
        <p>${esc(message)}</p><button class="btn btn-primary" type="button" data-of-retry="1">Réessayer</button></div>`;
    }
  } finally {
    clearTimeout(timer);
    // La lecture suivante se programme même après une coupure : un envoi en cours garde son suivi.
    if (seq === ofSeq && state.surface === "of") {
      const busy = state.of.entries.some((e) => e.active) || state.of.queue.length > 0;
      if (busy || state.of.metaRunning || (failed && silent)) {
        const delay = failed ? 5000 : busy ? 2000 : 3000;
        state.of.pollTimer = setTimeout(() => loadOneFichierView({ silent: true }), delay);
      }
    }
  }
}

function openOneFichierView() {
  state.surface = "of";
  document.querySelectorAll(".media-tab").forEach((b) => {
    const on = b.dataset.media === "1fichier";
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelector(".app-main").classList.add("of-mode");
  $("of-view").classList.remove("hidden");
  state.selected.clear();
  refreshSelectionDom();
  updateSelectionUi();
  scheduleComputePlan();
  loadOneFichierView();
}

function closeOneFichierView() {
  if (state.surface !== "of") return;
  state.surface = "library";
  state.of.detailId = null;
  clearTimeout(state.of.pollTimer);
  document.querySelector(".app-main").classList.remove("of-mode");
  $("of-view").classList.add("hidden");
  setSagasSurface("of", false);
  closeDrawer("of-drawer");
}

async function openOfDetail(key) {
  const e = state.of.entries.find((x) => x.key === String(key));
  if (!e) return;
  state.of.detailId = e.key;
  try {
    state.of.disks = (await api("/api/disks")).disks || [];
  } catch (_) {
    state.of.disks = [];
  }
  renderOfDetail(e);
  openDrawer("of-drawer");
}

function renderOfDetail(e, { keepScroll = false } = {}) {
  const kind = ofKind(e);
  const m = ofMeta(e);
  const title = ofTitle(e);
  const year = ofYear(e);
  const st = ofStatus(e);
  const hue = hueOf(title);
  const runtimeChip = kind === "ebook"
    ? (m.books ? `${m.books} livre${m.books > 1 ? "s" : ""}` : "")
    : kind === "serie"
      ? (m.runtime ? `${m.runtime} min / épisode` : "")
      : (m.runtime ? fmtDuration(m.runtime) : "");
  const chips = [
    `<span class="chip ${kind === "film" ? "" : "accent"}">${esc(OF_KIND[kind])}</span>`,
    year ? `<span class="chip">${esc(year)}</span>` : "",
    runtimeChip ? `<span class="chip">${esc(runtimeChip)}</span>` : "",
    m.rating ? `<span class="chip accent">${esc(fmtRating(m.rating))}${m.votes ? ` · ${esc(NF0.format(m.votes))} votes` : ""}</span>` : "",
    ...(m.genres || []).map((g) => `<span class="chip">${esc(g)}</span>`),
    `<span class="chip ${st.cls}">${esc(st.label)}</span>`,
  ].join("");

  let actions = "";
  if (e.active) {
    actions = `<p class="muted small">Un envoi est en cours pour cet élément.</p>`;
  } else if (e.status === "done") {
    const defaultDir = (state.of.restoreDirs || {})[kind] || "";
    const diskOptions = state.of.disks.map((d) => {
      const name = d.path.split("/").filter(Boolean).pop() || d.path;
      return `<option value="disk:${esc(d.path)}">Disque externe : ${esc(name)} (${esc(d.path)})</option>`;
    }).join("");
    actions = `
      <button class="btn btn-ghost" type="button" data-of-check="${esc(e.key)}">Vérifier le lien sur 1fichier</button>
      <label class="field of-dest-field"><span>Destination de la restauration</span>
        <select id="of-target-detail" aria-label="Destination de la restauration">
          <option value="default" ${defaultDir ? "" : "disabled"}>${defaultDir ? `Dossier de restauration : ${esc(defaultDir)}` : "Dossier de restauration non défini (Réglages)"}</option>
          <option value="origin">Emplacement d'origine : ${esc(e.original_path || "")}</option>
          ${diskOptions}
        </select>
        <span class="muted small">Un disque externe reçoit l'élément dans son dossier MEDIA/… (réglages « Sous-dossier films / séries / ebooks »).</span>
      </label>
      <button class="btn btn-primary" type="button" data-of-restore="${esc(e.key)}" data-of-title="${esc(title)}">Restaurer ${esc(e.season !== null && e.season !== undefined ? "cette saison" : OF_RESTORE_LABEL[kind])}</button>`;
  } else {
    actions = `
      <button class="btn btn-primary" type="button" data-of-recover="${esc(e.key)}">Vérifier sur 1fichier</button>
      <button class="btn btn-ghost" type="button" data-of-forget="${esc(e.key)}">Retirer de la liste</button>`;
  }

  const typeFacts = kind === "serie" && e.season !== null && e.season !== undefined
    ? [factRow("Série", esc(ofMeta(e).title || e.title || "—")), factRow("Saison", esc(e.season)),
       factRow("Épisodes", esc(e.episodes || "—")), factRow("Dossier de la série", esc(e.show_name || "—"))]
    : kind === "serie"
    ? [factRow("Saisons", esc(m.seasons || "—")), factRow("Épisodes", esc(m.episodes || "—")),
       factRow("Statut", esc(m.status || "—"))]
    : kind === "ebook"
      ? [factRow("Auteur", esc(m.author || "—")), factRow("Livres", esc(m.books || "—")),
         factRow("Formats", esc((m.formats || []).join(", ") || "—")),
         factRow("Premier livre", esc(m.first_book || "—")),
         factRow("Jaquette tirée de", esc(m.cover_source || "aucune (format sans jaquette)"), true)]
      : [];
  const facts = [
    factRow("Dossier 1fichier", `<code>${esc(e.folder)}</code>`),
    e.status === "done" ? factRow("Copie sur disque", e.local_copy ? `<code>${esc(e.local_copy)}</code>` : "aucune copie sur disque", true) : "",
    factRow("Taille", esc(formatSize(e.remote_size || e.size))),
    factRow("Envoyé le", esc(fmtDate(e.archived_at || e.created_at))),
    e.status === "done" ? factRow("Date d'envoi sur 1fichier", esc(remoteDateLabel(e))) : "",
    e.status === "done" ? factRow("Lien sur 1fichier", linkStatusHtml(e), true) : "",
    factRow("Restauré le", e.restored_at ? esc(fmtDate(e.restored_at)) : "—"),
    factRow("Titre d'origine", esc(m.original_title || "—")),
    factRow("Fiche TMDB", m.tmdb_url ? `<a href="${esc(m.tmdb_url)}" target="_blank" rel="noopener">themoviedb.org</a>` : "—"),
    ...typeFacts,
    factRow("Emplacement d'origine", `<code>${esc(e.original_path || "—")}</code>`, true),
  ].join("");

  const summary = m.summary
    ? `<p class="summary">${esc(m.summary)}</p>`
    : kind === "ebook"
      ? `<p class="summary none">Pas de fiche TMDB pour un ebook : les informations viennent du nom du dossier et de la jaquette.</p>`
      : `<p class="summary none">Aucun résumé disponible${e.meta && e.meta.unavailable ? " : fiche TMDB introuvable" : ""}.</p>`;
  const tagline = m.tagline ? `<p class="of-tagline">« ${esc(m.tagline)} »</p>` : "";
  const eyebrow = `${OF_KIND[kind]} · 1fichier`;
  const subline = ofSubline(e).filter(Boolean).map(esc).join(" · ");

  $("of-drawer-body").innerHTML = `
    <div class="detail-hero">
      <div class="detail-bg" aria-hidden="true"></div>
      <div class="detail-hero-inner">
        <div class="detail-poster">
          ${m.poster_url ? `<img src="${esc(m.poster_url)}" alt="" decoding="async">` : `<div class="of-poster-fallback" style="background: linear-gradient(150deg, hsl(${hue} 40% 30%), hsl(${(hue + 50) % 360} 40% 10%))">${esc(title.charAt(0).toUpperCase())}</div>`}
        </div>
        <div class="detail-head">
          <span class="eyebrow">${esc(eyebrow)}</span>
          <h2 id="of-drawer-title">${esc(title)}</h2>
          <span class="sub">${subline}</span>
          <div class="chips">${chips}</div>
        </div>
      </div>
    </div>
    <div class="detail-actions of-detail-actions">${actions}</div>
    ${e.status === "done" && !e.active ? `
    <section class="detail-section of-access" id="of-access">
      <h3>Récupérer hors de l'application</h3>
      <p class="muted small">Lien 1fichier et mot de passe de l'archive 7z : suffisent pour la télécharger et l'ouvrir ailleurs.</p>
      <div class="of-access-body">
        <button class="btn btn-ghost" type="button" data-of-reveal="${esc(e.key)}">Afficher le lien et le mot de passe</button>
      </div>
    </section>` : ""}
    ${castSectionHtml(kind === "film" ? e.tmdb_id : null)}
    <section class="detail-section"><h3>Synopsis</h3>${tagline}${summary}</section>
    <section class="detail-section"><h3>Informations</h3><dl class="facts">${facts}</dl></section>
  `;

  const bg = $("of-drawer-body").querySelector(".detail-bg");
  const backdrop = m.backdrop_url || m.poster_url;
  bg.style.backgroundImage = backdrop
    ? cssUrl(backdrop)
    : `linear-gradient(150deg, hsl(${hue} 40% 30%), hsl(${(hue + 50) % 360} 40% 10%))`;
  if (!keepScroll) $("of-drawer-body").scrollTop = 0;
  state.of.detailSig = JSON.stringify({ ...e, progress: undefined });
  hydrateCast($("of-drawer-body"));
}

let restoreBusy = false;  // une restauration à la fois : un double clic ne demande pas deux fois

async function restoreEntry(key, title, choice) {
  if (restoreBusy) return;
  restoreBusy = true;
  try {
    const value = choice || "default";
    const target = value.startsWith("disk:") ? "disk" : value;
    const disk = target === "disk" ? value.slice(5) : null;
    const where = target === "origin"
      ? "à son emplacement d'origine"
      : target === "disk"
        ? `sur le disque ${disk}`
        : "dans le dossier de restauration des réglages";
    if (!confirm(`Restaurer « ${title} » ${where} ? La copie sur 1fichier est conservée.`)) return;
    const data = await api("/api/1fichier/restore", {
      method: "POST",
      body: JSON.stringify({ keys: [String(key)], target, disk }),
    });
    trackJob(data.job_id, "restore_1fichier", { live: true });
    toast("Restauration démarrée.", "success");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    restoreBusy = false;
  }
}

// Date telle que renvoyée par 1fichier (texte) : affichée en français si elle est lisible.
function fmtRemoteDate(raw) {
  if (!raw) return null;
  const d = new Date(String(raw).replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return String(raw);
  return d.toLocaleString("fr-FR", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function remoteDateLabel(e) {
  if (e.uploaded_at) return fmtRemoteDate(e.uploaded_at);
  if (e.link_checked_at) return "non fournie par 1fichier";
  return "non vérifiée (bouton « Vérifier le lien »)";
}

function linkStatusHtml(e) {
  if (e.link_ok === true) {
    return `<span class="chip ok">Actif</span> <span class="muted small">vérifié le ${esc(fmtDateTime(e.link_checked_at))}</span>`;
  }
  if (e.link_ok === false) {
    return `<span class="chip warn">Introuvable sur 1fichier</span> <span class="muted small">vérifié le ${esc(fmtDateTime(e.link_checked_at))}</span>`;
  }
  return `<span class="chip">Non vérifié</span>`;
}

async function revealEntry(key) {
  const box = document.querySelector("#of-access .of-access-body");
  if (box) box.innerHTML = `<p class="muted small">Chargement…</p>`;
  try {
    const d = await api(`/api/1fichier/entries/${encodeURIComponent(key)}/access`);
    if (!box || !box.isConnected) return;
    box.innerHTML = `
      <div class="of-copy-row">
        <span class="of-copy-label">Lien 1fichier</span>
        <code class="of-copy-value">${esc(d.url)}</code>
        <button class="btn btn-primary" type="button" data-copy="${esc(d.url)}">Copier le lien</button>
      </div>
      <div class="of-copy-row">
        <span class="of-copy-label">Mot de passe 7z</span>
        <code class="of-copy-value">${esc(d.password)}</code>
        <button class="btn btn-primary" type="button" data-copy="${esc(d.password)}">Copier le mot de passe</button>
      </div>
      <p class="muted small">Archive : <code>${esc(d.archive_name || "")}</code> (7z, AES-256). Sans le mot de passe, son contenu n'est pas lisible.</p>`;
  } catch (err) {
    if (box) box.innerHTML = `<button class="btn btn-ghost" type="button" data-of-reveal="${esc(key)}">Réessayer</button>`;
    toast(err.message, "error");
  }
}

async function checkEntry(key) {
  try {
    const data = await api(`/api/1fichier/entries/${encodeURIComponent(key)}/check`, { method: "POST" });
    toast(data.link_ok ? "Lien actif sur 1fichier." : "Archive introuvable sur 1fichier : le lien ne fonctionne plus.",
      data.link_ok ? "success" : "error");
    await loadOneFichierView({ silent: true });
    const e = state.of.entries.find((x) => x.key === String(key));
    if (e && state.of.detailId === String(key)) renderOfDetail(e);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function recoverEntry(key) {
  try {
    const data = await api(`/api/1fichier/entries/${encodeURIComponent(key)}/recover`, { method: "POST" });
    toast(`Archive retrouvée sur 1fichier (${formatSize(data.size)}).`, "success");
    await loadOneFichierView();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function forgetEntry(key) {
  if (!confirm("Retirer cet envoi incomplet de la liste ? Un fichier partiellement envoyé reste à supprimer à la main sur 1fichier.")) return;
  try {
    await api(`/api/1fichier/entries/${encodeURIComponent(key)}`, { method: "DELETE" });
    toast("Entrée retirée.", "success");
    closeDrawer("of-drawer");
    await loadOneFichierView();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function refreshOfMeta() {
  const btn = $("of-refresh-meta");
  btn.disabled = true;
  try {
    const data = await api("/api/1fichier/refresh-meta", { method: "POST" });
    toast(data.started ? "Actualisation des fiches lancée en arrière-plan." : "Une actualisation est déjà en cours.", "success");
    await loadOneFichierView({ silent: true });
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
  }
}

function onOfClick(e) {
  const open = e.target.closest("[data-of-open]");
  if (open) { openOfDetail(open.dataset.ofOpen); return; }
  const restore = e.target.closest("[data-of-restore]");
  if (restore) {
    const choice = $("of-target-detail") ? $("of-target-detail").value : "default";
    restoreEntry(restore.dataset.ofRestore, restore.dataset.ofTitle, choice);
    return;
  }
  if (e.target.closest("[data-of-retry]")) { loadOneFichierView(); return; }
  const reveal = e.target.closest("[data-of-reveal]");
  if (reveal) { revealEntry(reveal.dataset.ofReveal); return; }
  const check = e.target.closest("[data-of-check]");
  if (check) { checkEntry(check.dataset.ofCheck); return; }
  const recover = e.target.closest("[data-of-recover]");
  if (recover) { recoverEntry(recover.dataset.ofRecover); return; }
  const forget = e.target.closest("[data-of-forget]");
  if (forget) { forgetEntry(forget.dataset.ofForget); return; }
  if (e.target.closest("[data-of-settings]")) { openSettingsDrawer(); }
}

// Index des mots de passe : état des copies et export chiffré téléchargé par le navigateur.
async function refreshChecksStatus() {
  const out = $("set-1f-checks-status");
  if (!out) return;
  try {
    const c = await api("/api/1fichier/checks/status");
    const r = c.last_run;
    const next = c.next_run_at ? `prochain contrôle le ${fmtDateTime(c.next_run_at)}` : "contrôles inactifs (clé API absente)";
    const last = r
      ? `dernier contrôle du ${fmtDateTime(r.at)} : ${r.links_ok} lien(s) OK, ${r.links_lost.length} introuvable(s), ${r.errors.length} erreur(s)`
        + (r.tests.length
          ? `, test de restauration ${r.tests[0].ok === true ? "réussi" : r.tests[0].ok === false ? "ÉCHOUÉ" : "non effectué"} (${r.tests[0].folder})`
          : ", aucun test de restauration éligible")
      : "aucun contrôle encore effectué";
    const bad = r && (r.links_lost.length || r.errors.length || r.tests.some((t) => !t.ok));
    out.className = `path-check-result small ${bad ? "error" : "muted"}`;
    out.textContent = c.running ? "Contrôle en cours…" : `${last} · ${next}.`;
  } catch (err) {
    out.className = "path-check-result muted small";
    out.textContent = "État des contrôles indisponible.";
  }
}

async function runOneFichierChecks() {
  const out = $("set-1f-checks-result");
  out.className = "path-check-result muted small";
  out.textContent = "Lancé en arrière-plan…";
  try {
    const d = await api("/api/1fichier/checks/run", { method: "POST" });
    out.textContent = d.started ? "Contrôle lancé : le résultat s'affiche ci-dessus." : "Un contrôle est déjà en cours.";
    setTimeout(refreshChecksStatus, 4000);
  } catch (err) {
    out.className = "path-check-result error small";
    out.textContent = err.message;
  }
}

async function refreshIndexStatus() {
  refreshChecksStatus();
  const out = $("set-1f-index-status");
  if (!out) return;
  try {
    const s = await api("/api/1fichier/index/status");
    const copies = s.backups
      ? `${s.backups} copie(s) locale(s), la plus récente du ${fmtDateTime(s.last_backup_at)}`
      : "aucune copie locale pour l'instant";
    const exported = s.last_export_at
      ? `dernier export chiffré du ${fmtDateTime(s.last_export_at)}`
      : "jamais exporté : aucune copie hors du serveur";
    out.className = `path-check-result small ${s.last_export_at ? "muted" : "error"}`;
    out.textContent = `${copies} · ${exported}.`;
  } catch (err) {
    out.className = "path-check-result muted small";
    out.textContent = "État de l'index indisponible.";
  }
}

async function exportOneFichierIndex() {
  const pass = $("set-1f-export-pass").value;
  const out = $("set-1f-export-result");
  if (pass.length < 12) {
    out.className = "path-check-result error small";
    out.textContent = "Phrase de passe : 12 caractères minimum.";
    return;
  }
  out.className = "path-check-result muted small";
  out.textContent = "Export en cours…";
  try {
    const resp = await fetch("/api/1fichier/index/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ passphrase: pass }),
    });
    if (!resp.ok) {
      let message = `Erreur HTTP ${resp.status}`;
      try { const d = await resp.json(); if (d.detail) message = d.detail; } catch (_) { /* pas de JSON */ }
      throw new Error(message);
    }
    const blob = await resp.blob();
    const disposition = resp.headers.get("content-disposition") || "";
    const match = disposition.match(/filename="?([^";]+)"?/);
    const fileName = match ? match[1] : "1fichier-index.7z";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = fileName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    $("set-1f-export-pass").value = "";
    out.className = "path-check-result ok small";
    out.textContent = "Export téléchargé. Garde-le hors du serveur, avec sa phrase de passe.";
    await refreshIndexStatus();
  } catch (err) {
    out.className = "path-check-result error small";
    out.textContent = err.message;
  }
}

async function test1fichier() {
  const out = $("set-1f-test-result");
  if ($("set-1f-key").value.trim() !== $("set-1f-key").dataset.saved) {
    out.className = "path-check-result error small";
    out.textContent = "Enregistre les réglages avant de tester la nouvelle clé.";
    return;
  }
  out.className = "path-check-result muted small";
  out.textContent = "Test en cours…";
  try {
    await api("/api/1fichier/test", { method: "POST" });
    out.className = "path-check-result ok small";
    out.textContent = "Connexion OK.";
  } catch (err) {
    out.className = "path-check-result error small";
    out.textContent = err.message;
  }
}
