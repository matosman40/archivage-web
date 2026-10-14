"use strict";
// identify.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------
// Identifier un élément d'un disque (films et séries) : recherche TMDB puis renommage
// ---------------------------------------------------------------

function canIdentify(r) {
  return r.connected && (state.disk.kind === "films" || state.disk.kind === "series");
}

function findDiskItem(path) {
  for (const disk of state.disk.disks) {
    for (const list of Object.values(disk.items || {})) {
      const hit = list.find((it) => it.path === path);
      if (hit) return hit;
    }
  }
  return null;
}

// Caractères refusés sur exFAT/NTFS : la liste vient du serveur (GET /api/config, version 45), où elle est
// définie une seule fois. La valeur par défaut ne sert qu'avant la réponse du serveur.
let forbiddenNameRe = /[<>"/\\|?*\x00-\x1f]/g;

function setForbiddenNameChars(chars) {
  if (!chars) return;
  const escaped = String(chars).replace(/[\\\]\[^-]/g, "\\$&");
  try {
    forbiddenNameRe = new RegExp(`[${escaped}\\x00-\\x1f]`, "g");
  } catch (_) { /* liste invalide : on garde la valeur par défaut */ }
}

// Un titre TMDB peut contenir des caractères interdits sur exFAT/NTFS : « Mission: Impossible » → « Mission - Impossible ».
function safeDiskName(text) {
  return String(text || "")
    .replace(/:\s*/g, " - ")
    .replace(forbiddenNameRe, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function fileExtOf(name) {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i) : "";
}

function identifyError(message) {
  const box = $("identify-error");
  box.textContent = message || "";
  box.classList.toggle("hidden", !message);
}

function identifyUpdateOk() {
  const id = state.identify;
  const typed = $("identify-name").value.trim();
  $("identify-ok").disabled = !id || !typed || id.busy;
}

function identifyNewName() {
  const id = state.identify;
  return safeDiskName($("identify-name").value) + (id && !id.is_dir ? id.ext : "");
}

function renderIdentifyResults() {
  const id = state.identify;
  const list = $("identify-results");
  if (!id) return;
  list.innerHTML = (id.results || []).map((r, i) => {
    const picked = id.picked === i;
    const year = r.year ? ` <span class="muted">(${esc(r.year)})</span>` : "";
    const overview = r.overview ? `<span class="muted small identify-overview">${esc(r.overview.slice(0, 180))}${r.overview.length > 180 ? "…" : ""}</span>` : "";
    const thumb = r.poster_url
      ? `<img src="${esc(r.poster_url)}" alt="" loading="lazy" decoding="async">`
      : `<span class="of-initial" aria-hidden="true">${esc((r.title || "?").charAt(0).toUpperCase())}</span>`;
    return `<li><button type="button" class="identify-pick${picked ? " is-picked" : ""}" data-identify-pick="${i}" aria-pressed="${picked}">
      <span class="identify-thumb">${thumb}</span>
      <span class="identify-text"><strong>${esc(r.title)}</strong>${year}${overview}</span>
    </button></li>`;
  }).join("");
}

const IDENTIFY_DEBOUNCE_MS = 300;
const IDENTIFY_CACHE_MAX = 60;
const identifyCache = new Map();  // requête -> résultats : retaper ou revenir sur un titre ne refait pas l'appel

function identifyKey(kind, q, year, adult) {
  return [kind, adult ? 1 : 0, q.toLowerCase(), year].join("|");
}

function identifyCurrentKey() {
  const id = state.identify;
  return identifyKey(id.kind, $("identify-query").value.trim(), $("identify-year").value.trim(),
    id.kind === "films" && $("identify-adult").checked);
}

// Lance la recherche pendant la saisie (avec un court délai), ou tout de suite si `immediate`.
function scheduleIdentifySearch(immediate = false) {
  const id = state.identify;
  if (!id) return;
  clearTimeout(id.timer);
  if (!$("identify-query").value.trim()) {
    id.seq = (id.seq || 0) + 1;  // annule une recherche encore en vol
    id.results = [];
    id.picked = null;
    $("identify-results").innerHTML = "";
    $("identify-status").textContent = "Tape un titre : les résultats s'affichent pendant la saisie.";
    identifyError("");
    return;
  }
  if (immediate) { searchIdentify(); return; }
  id.timer = setTimeout(searchIdentify, IDENTIFY_DEBOUNCE_MS);
}

function rememberIdentify(key, results) {
  if (identifyCache.size >= IDENTIFY_CACHE_MAX) identifyCache.delete(identifyCache.keys().next().value);
  identifyCache.set(key, results);
}

function applyIdentifyResults(id, results) {
  id.results = results;
  id.picked = null;
  $("identify-status").textContent = results.length
    ? `${NF0.format(results.length)} résultat(s). Entrée prend le premier, ↓ parcourt la liste.`
    : "Aucun résultat. Essaie sans l'année, ou un autre titre.";
  renderIdentifyResults();
  // Entrée pressée pendant la recherche : prend le premier dès que les résultats de CETTE saisie sont là.
  if (id.pickKey && id.pickKey === id.lastKey) {
    id.pickKey = null;
    if (results.length) { pickIdentify(0); $("identify-name").focus(); }
  }
}

async function searchIdentify() {
  const id = state.identify;
  if (!id) return;
  clearTimeout(id.timer);
  const q = $("identify-query").value.trim();
  identifyError("");
  if (!q) { scheduleIdentifySearch(); return; }
  const key = identifyCurrentKey();
  const seq = (id.seq || 0) + 1;
  id.seq = seq;
  id.lastKey = key;
  if (identifyCache.has(key)) {
    applyIdentifyResults(id, identifyCache.get(key));
    return;
  }
  $("identify-status").textContent = "Recherche sur TMDB…";
  try {
    const year = $("identify-year").value.trim();
    const params = new URLSearchParams({ kind: id.kind, q });
    if (year) params.set("year", year);
    if (id.kind === "films" && $("identify-adult").checked) params.set("adult", "true");
    const data = await api(`/api/tmdb/search?${params.toString()}`);
    if (state.identify !== id || id.seq !== seq) return;  // fenêtre fermée, ou saisie plus récente
    rememberIdentify(key, data.results || []);
    applyIdentifyResults(id, data.results || []);
  } catch (err) {
    if (state.identify !== id || id.seq !== seq) return;
    $("identify-status").textContent = "";
    identifyError(err.message);
  }
}

// Entrée dans le champ titre : si le résultat de cette requête est là, prend le premier ; sinon cherche.
function identifySubmit() {
  const id = state.identify;
  if (!id) return;
  if ($("identify-query").value.trim() && id.lastKey === identifyCurrentKey() && (id.results || []).length) {
    pickIdentify(0);
    $("identify-name").focus();
    return;
  }
  if ($("identify-query").value.trim()) id.pickKey = identifyCurrentKey();  // prend le premier à l'arrivée
  scheduleIdentifySearch(true);
}

function focusIdentifyPick(index) {
  const picks = document.querySelectorAll("#identify-results .identify-pick");
  if (picks[index]) picks[index].focus();
}

function pickIdentify(index) {
  const id = state.identify;
  const r = id && (id.results || [])[index];
  if (!r) return;
  id.picked = index;
  const base = safeDiskName(`${r.title}${r.year ? ` (${r.year})` : ""}`);
  $("identify-name").value = base;
  id.pickedName = base;
  identifyError("");
  renderIdentifyResults();
  identifyUpdateOk();
}

// Fenêtre « Identifier » générique : `target` décrit l'élément à renommer et l'adresse à appeler.
async function openIdentifyFor(target) {
  const kind = target.kind;
  const ext = target.isDir ? "" : fileExtOf(target.name);
  state.identify = { kind, name: target.name, is_dir: !!target.isDir, ext,
                     endpoint: target.endpoint, body: target.body, onDone: target.onDone,
                     results: [], picked: null, busy: false };
  const id = state.identify;
  $("identify-title").textContent = kind === "series" ? "Identifier la série" : "Identifier le film";
  // Case « Contenu adulte » : pré-cochée pour un film adulte, visible seulement depuis la bibliothèque serveur.
  $("identify-adult").checked = kind === "films" && !!target.adult;
  $("identify-adult-row").classList.toggle("hidden", !(kind === "films" && target.showAdult));
  $("identify-rename-row").classList.toggle("hidden", !target.serverRename);
  $("identify-rename-file").checked = false;
  $("identify-current").textContent = `Nom actuel : ${target.name}` + (target.count > 1 ? ` (${target.count} dossiers)` : "");
  $("identify-hint").textContent = target.note || "Format conseillé : Titre (année). Exemple : Idiocracy (2006).";
  $("identify-query").value = "";
  $("identify-year").value = "";
  $("identify-name").value = "";
  $("identify-ext").textContent = ext;
  $("identify-status").textContent = "";
  $("identify-results").innerHTML = "";
  identifyError("");
  identifyUpdateOk();
  $("identify-modal").classList.remove("hidden");
  setTimeout(() => $("identify-query").focus(), 0);

  // Titre et année déduits du nom actuel pour préremplir la recherche.
  // Seulement si l'utilisateur n'a encore rien saisi pendant la réponse : sa frappe n'est jamais écrasée.
  let prefilled = false;
  try {
    const g = await api(`/api/tmdb/guess?name=${encodeURIComponent(target.name)}`);
    if (state.identify !== id) return;
    if (!$("identify-query").value.trim()) {
      $("identify-query").value = g.title || "";
      if (!$("identify-year").value.trim()) $("identify-year").value = g.year || "";
      prefilled = true;
    }
  } catch (_) { /* pas grave : saisie manuelle */ }
  if (prefilled && $("identify-query").value) searchIdentify();
}


// Un élément d'un disque connecté (carte de l'onglet Disques).
function openIdentify(path) {
  const item = findDiskItem(path);
  if (!item) {
    toast("Élément introuvable : rafraîchis la liste.", "error");
    return;
  }
  openIdentifyFor({
    kind: state.disk.kind, name: item.name, isDir: !!item.is_dir,
    endpoint: "/api/disk-library/rename", body: { path }, adult: !!item.adult,
    onDone: () => loadDiskLibrary({ force: true }),
  });
}

// Un film ou une série de la bibliothèque serveur (depuis sa fiche).
function openServerIdentify() {
  const it = state.visible.find((x) => itemKey(x) === state.detailKey);
  if (!it) {
    toast("Élément introuvable : relance la recherche.", "error");
    return;
  }
  const done = () => {
    closeDrawer("detail-drawer");
    runSearch($("search-term").value.trim());
  };
  // Le fichier du serveur ne change pas : seul l'affichage, la recherche TMDB et le nom des copies suivent.
  const note = "Par défaut, le fichier du serveur garde son nom : l'identification sert à l'affichage, à la recherche TMDB "
    + "et au nom des copies. Coche « Renommer aussi le fichier » pour le renommer réellement (Plex devra être actualisé).";
  if (state.mediaType === "series") {
    openIdentifyFor({
      kind: "series", name: it.name, isDir: true, count: (it.paths || []).length, note,
      endpoint: "/api/library/identify", body: { kind: "series", paths: it.paths || [] }, onDone: done,
    });
  } else if (state.mediaType === "films") {
    openIdentifyFor({
      kind: "films", name: it.name, isDir: false, note, adult: !!it.adult, showAdult: true, serverRename: true,
      endpoint: "/api/library/identify", body: { kind: "films", paths: [it.path] }, onDone: done,
    });
  }
}

function closeIdentify() {
  if (state.identify) clearTimeout(state.identify.timer);
  state.identify = null;
  $("identify-modal").classList.add("hidden");
  identifyError("");
}

async function submitIdentify() {
  const id = state.identify;
  if (!id || id.busy) return;
  const name = identifyNewName();
  if (!safeDiskName($("identify-name").value)) return;
  id.busy = true;
  identifyUpdateOk();
  try {
    const picked = id.picked !== null ? (id.results || [])[id.picked] : null;
    const tmdbId = picked && $("identify-name").value.trim() === id.pickedName ? picked.id : null;
    const data = await api(id.endpoint, {
      method: "POST",
      body: JSON.stringify({ ...id.body, new_name: name, tmdb_id: tmdbId,
        ...(id.endpoint === "/api/library/identify" && id.kind === "films" ? { adult: $("identify-adult").checked, rename_file: $("identify-rename-file").checked } : {}) }),
    });
    const isIdentify = id.endpoint.includes("identify");
    if (data.unchanged) {
      toast("Le nom est déjà celui-ci.", "info");
    } else {
      toast(isIdentify ? `Identifié : ${data.title || data.name}` : `Renommé : ${data.name}`, "success");
      if (data.renamed_path) toast("Fichier renommé sur le serveur : actualise Plex (Réglages › Plex).", "info");
    }
    closeIdentify();
    if (id.onDone) id.onDone(data);
  } catch (err) {
    if (state.identify === id) {
      identifyError(err.message);
      id.busy = false;
      identifyUpdateOk();
    }
  }
}

function initIdentify() {
  $("identify-search").addEventListener("submit", (e) => { e.preventDefault(); identifySubmit(); });
  // Toute modification de la saisie annule une demande « Entrée pour prendre le premier » en attente.
  const typed = () => { if (state.identify) state.identify.pickKey = null; scheduleIdentifySearch(); };
  $("identify-query").addEventListener("input", typed);
  $("identify-year").addEventListener("input", typed);
  $("identify-adult").addEventListener("change", () => scheduleIdentifySearch(true));
  $("identify-query").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); focusIdentifyPick(0); }
  });
  $("identify-results").addEventListener("keydown", (e) => {
    const picks = [...$("identify-results").querySelectorAll(".identify-pick")];
    const at = picks.indexOf(document.activeElement);
    if (e.key === "ArrowDown" && at < picks.length - 1) { e.preventDefault(); picks[at + 1].focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); if (at <= 0) $("identify-query").focus(); else picks[at - 1].focus(); }
  });
  $("identify-results").addEventListener("click", (e) => {
    const pick = e.target.closest("[data-identify-pick]");
    if (pick) pickIdentify(Number(pick.dataset.identifyPick));
  });
  $("identify-name").addEventListener("input", () => {
    identifyError("");
    identifyUpdateOk();
  });
  $("identify-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); submitIdentify(); }
  });
  $("identify-ok").addEventListener("click", submitIdentify);
  $("identify-cancel").addEventListener("click", closeIdentify);
  $("identify-modal").addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeIdentify();
  });
}

// Import d'une jaquette (image choisie sur l'ordinateur) pour une collection d'ebooks.
function chooseEbookCover(path, onDone) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/jpeg,image/png,image/webp,image/gif";
  input.addEventListener("change", async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) { toast("Image trop volumineuse (5 Mo au maximum).", "error"); return; }
    try {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsDataURL(file);
      });
      const res = await api("/api/ebooks/cover", { method: "POST", body: JSON.stringify({ path, data }) });
      toast("Jaquette importée.", "success");
      onDone(res.poster_url);
    } catch (err) {
      toast(err.message, "error");
    }
  });
  input.click();
}

// Met la nouvelle jaquette sous les yeux : grille de la bibliothèque et fiche ouverte.
function afterEbookCover(path, posterUrl) {
  for (const list of [state.results, state.visible]) {
    for (const it of list || []) if (it.path === path) it.poster_url = posterUrl;
  }
  render();
  const idx = state.visible.findIndex((it) => it.path === path);
  if (state.detailKey === path && idx >= 0) renderDetail(state.visible[idx]);
}

function onDiskClick(e) {
  const coverBtn = e.target.closest("[data-disk-cover]");
  if (coverBtn) {
    e.preventDefault();
    chooseEbookCover(coverBtn.dataset.diskCover, () => loadDiskLibrary({ silent: true }));
    return;
  }
  const identifyBtn = e.target.closest("[data-disk-identify]");
  if (identifyBtn) { e.preventDefault(); openIdentify(identifyBtn.dataset.diskIdentify); return; }
  const kindBtn = e.target.closest("[data-disk-kind]");
  if (kindBtn) { setDiskKind(kindBtn.dataset.diskKind); return; }
  const filterBtn = e.target.closest("[data-disk-filter]");
  if (filterBtn) {
    const path = filterBtn.dataset.diskFilter;
    state.disk.diskFilter = state.disk.diskFilter === path ? "all" : path;
    state.disk.selected.clear();
    renderDiskView();
    return;
  }
  const folders = e.target.closest("[data-disk-folders]");
  if (folders) { openDiskFolders(folders.dataset.diskFolders); return; }
  const forget = e.target.closest("[data-disk-forget]");
  if (forget) { forgetDisk(forget.dataset.diskForget); }
  const integrity = e.target.closest("[data-disk-integrity]");
  if (integrity) { startDiskIntegrity(integrity.dataset.diskPath, integrity.dataset.diskIntegrity); }
}

function onDiskChange(e) {
  const box = e.target.closest("[data-disk-select]");
  if (box) {
    const path = box.dataset.diskSelect;
    if (box.checked) state.disk.selected.add(path);
    else state.disk.selected.delete(path);
    box.closest(".disk-item").classList.toggle("is-selected", box.checked);
    renderDiskActions();
    return;
  }
  if (e.target.id === "disk-disk") {
    state.disk.diskFilter = e.target.value;
    state.disk.selected.clear();
    renderDiskView();
  }
  if (e.target.id === "disk-tmdb") {
    state.disk.tmdbFilter = e.target.value;
    state.disk.selected.clear();  // la sélection ne garde pas les éléments masqués par le filtre
    renderDiskView();
  }
}

function openDiskView() {
  state.surface = "disk";
  document.querySelectorAll(".media-tab").forEach((b) => {
    const on = b.dataset.media === "disques";
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelector(".app-main").classList.add("of-mode");
  $("disk-view").classList.remove("hidden");
  state.selected.clear();
  refreshSelectionDom();
  updateSelectionUi();
  $("disk-search").value = $("search-term").value;
  if (!state.disk.loaded) renderDiskView();
  loadDiskLibrary();
  clearInterval(state.disk.timer);
  state.disk.timer = setInterval(() => loadDiskLibrary({ silent: true }), DISK_POLL_MS);
}

function closeDiskView() {
  if (state.surface !== "disk") return;
  state.surface = "library";
  clearInterval(state.disk.timer);
  clearTimeout(state.disk.metaTimer);
  state.disk.timer = null;
  state.disk.selected.clear();
  document.querySelector(".app-main").classList.remove("of-mode");
  $("disk-view").classList.add("hidden");
  setSagasSurface("disk", false);
}
