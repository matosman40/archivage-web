"use strict";
// settings.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Journal
// ---------------------------------------------------------------------

async function openLogDrawer() {
  openDrawer("log-drawer");
  const content = $("log-content");
  content.innerHTML = `<p class="muted">Chargement…</p>`;
  try {
    const data = await api("/api/logs");
    if (data.entries.length === 0) {
      content.innerHTML = `<p class="muted">Aucune opération enregistrée pour le moment.</p>`;
      return;
    }
    content.innerHTML = data.entries.map((e) => `
      <div class="log-line">
        <span>${esc(e.timestamp)}</span>
        <span class="${esc(e.status.toLowerCase())}">${esc(e.status)}</span>
        <span>${esc(e.operation)}</span>
        <span>${esc(e.detail)}</span>
      </div>`).join("");
  } catch (err) {
    content.innerHTML = `<p class="muted">Erreur : ${esc(err.message)}</p>`;
  }
}

// ---------------------------------------------------------------------
// Destinations
// ---------------------------------------------------------------------

// Relit les disques. La liste n'est reconstruite que si les disques ont changé ; le choix de
// l'utilisateur (un disque, ou la saisie manuelle) est conservé.
let disksSeq = 0;
async function loadDisks() {
  const sel = $("destination-select");
  const seq = ++disksSeq;
  try {
    const data = await api("/api/disks");
    if (seq !== disksSeq) return; // une lecture plus récente est en cours : celle-ci est périmée
    const disks = data.disks || [];
    const previous = sel.value;
    const paths = disks.map((d) => d.path);
    const shown = [...sel.options].map((o) => o.value).filter((v) => v !== "__manual__");
    const sameList = shown.length === paths.length && paths.every((p, i) => p === shown[i]);
    if (!sameList) {
      sel.innerHTML = `<option value="__manual__">Autre chemin (saisie manuelle)…</option>`;
      for (const d of disks) {
        const o = document.createElement("option");
        o.value = d.path;
        sel.appendChild(o);
      }
    }
    for (const d of disks) {
      const o = [...sel.options].find((x) => x.value === d.path);
      if (o) o.textContent = `${d.path} — libre ${formatSize(d.free_bytes)}`;
    }
    if (previous === "__manual__" || paths.includes(previous)) {
      sel.value = previous;
    } else if (disks.length > 0) {
      sel.value = disks[0].path;
    } else {
      sel.value = "__manual__";
      $("destination-space").textContent = "Aucun disque détecté — saisis un chemin, ou vérifie que le disque est monté et accessible au conteneur.";
    }
    updateDestinationSpace();
  } catch (err) {
    if (seq === disksSeq) toast(err.message, "error");
  }
}

function updateDestinationSpace() {
  const sel = $("destination-select");
  const isManual = sel.value === "__manual__";
  $("destination-manual").classList.toggle("hidden", !isManual);
  if (isManual) {
    $("destination-space").textContent = "Chemin tel que visible depuis le conteneur (ex. /media/devmon/MonDisque).";
    return;
  }
  const opt = sel.options[sel.selectedIndex];
  $("destination-space").textContent = opt ? opt.textContent : "";
}

function isPlausibleDestination(path) {
  if (!path) return false;
  const norm = path.trim();
  if (norm === "/" || norm === ".") return false;
  return norm.split("/").filter(Boolean).length >= 2;
}

function currentDestination() {
  const sel = $("destination-select");
  if (sel.value === "__manual__") return $("destination-manual").value.trim();
  return sel.value;
}

// ---------------------------------------------------------------------
// Réglages
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// Priorités des tâches et plages de transfert
// ---------------------------------------------------------------------

const PRIORITY_CHOICES = [["urgent", "Urgent"], ["normal", "Normal"], ["bas", "Basse"]];
const PRIORITY_NAME = { urgent: "urgente", normal: "normale", bas: "basse" };
const RESOURCE_NAME = { onefichier: "1fichier", disk: "disque" };

function priorityOptions(selected = "normal") {
  return PRIORITY_CHOICES.map(([value, label]) =>
    `<option value="${value}"${value === selected ? " selected" : ""}>${label}</option>`).join("");
}

async function changeJobPriority(jobId, priority, select) {
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/priority`, { method: "POST", body: JSON.stringify({ priority }) });
  } catch (err) {
    toast(err.message, "error");
    if (select) select.value = select.dataset.previous || "normal";
    return;
  }
  if (select) select.dataset.previous = priority;
  toast(`Priorité : ${PRIORITY_NAME[priority]}. S'applique aux éléments qui restent à traiter.`, "success");
}

const TW_NEW_ROW = { name: "Plage", start: "09:00", end: "12:00", enabled: true, onefichier_mbps: 0, disk_mbps: 0 };
const TW_DEFAULTS = [
  { name: "Jour", start: "09:00", end: "22:00", enabled: true, onefichier_mbps: 2, disk_mbps: 30 },
  { name: "Nuit", start: "01:00", end: "07:00", enabled: true, onefichier_mbps: 0, disk_mbps: 0 },
];

function twRowHtml(w) {
  return `<div class="tw-row" data-tw-row>
    <label class="tw-field tw-name"><span>Nom</span><input type="text" data-tw="name" maxlength="40" value="${esc(w.name)}"></label>
    <label class="tw-field"><span>Début</span><input type="text" data-tw="start" inputmode="numeric" placeholder="09:00" value="${esc(w.start)}"></label>
    <label class="tw-field"><span>Fin (24:00 = minuit)</span><input type="text" data-tw="end" inputmode="numeric" placeholder="22:00" value="${esc(w.end)}"></label>
    <label class="tw-field"><span>1fichier (Mo/s)</span><input type="number" min="0" step="0.5" data-tw="onefichier_mbps" value="${esc(w.onefichier_mbps)}"></label>
    <label class="tw-field"><span>Disque (Mo/s)</span><input type="number" min="0" step="1" data-tw="disk_mbps" value="${esc(w.disk_mbps)}"></label>
    <label class="check-row tw-enabled"><input type="checkbox" data-tw="enabled"${w.enabled ? " checked" : ""}><span>Active</span></label>
    <button type="button" class="btn btn-ghost small" data-tw-remove>Supprimer</button>
  </div>`;
}

function renderTransferWindows(windows) {
  $("set-tw-list").innerHTML = (windows || []).map(twRowHtml).join("");
}

function readTransferWindows() {
  return [...document.querySelectorAll("#set-tw-list [data-tw-row]")].map((row) => {
    const field = (key) => row.querySelector(`[data-tw="${key}"]`);
    return {
      name: field("name").value.trim() || "Plage",
      start: field("start").value.trim(),
      end: field("end").value.trim(),
      enabled: field("enabled").checked,
      onefichier_mbps: Number(field("onefichier_mbps").value || 0),
      disk_mbps: Number(field("disk_mbps").value || 0),
    };
  });
}

function transferStatusText(st) {
  const speed = (mbps) => (mbps > 0 ? `${mbps} Mo/s` : "illimité");
  if (st.active) {
    return `Maintenant : plage « ${st.active.name} » (1fichier ${speed(st.active.onefichier_mbps)}, `
      + `disque ${speed(st.active.disk_mbps)}), jusqu'à ${st.active.end}.`;
  }
  const next = st.next ? ` Reprise à ${st.next.start} (« ${st.next.name} »).` : " Aucune plage activée.";
  return `Hors plage : transferts en pause.${next}`;
}

async function loadTransferStatus() {
  const el = $("set-tw-status");
  try {
    el.textContent = transferStatusText(await api("/api/transfers/status"));
  } catch (_) {
    el.textContent = "";
  }
}

function linesToList(text) {
  return text.split("\n").map((l) => l.trim()).filter(Boolean);
}

async function openSettingsDrawer() {
  openDrawer("settings-drawer");
  loadAdultState();
  document.querySelectorAll(".path-check-result").forEach((n) => { n.textContent = ""; n.className = "path-check-result muted small"; });
  try {
    const data = await api("/api/settings");
    const s = data.settings;
    $("set-source-films").value = s.source_dirs_films.join("\n");
    $("set-source-series").value = s.source_dirs_series.join("\n");
    $("set-source-ebooks").value = s.source_dirs_ebooks.join("\n");
    $("set-dest-subdir-films").value = s.dest_subdir_films;
    $("set-dest-subdir-series").value = s.dest_subdir_series;
    $("set-dest-subdir-ebooks").value = s.dest_subdir_ebooks;
    $("set-source-adultes").value = (s.source_dirs_adultes || []).join("\n");
    $("set-dest-subdir-adultes").value = s.dest_subdir_adultes || "";
    $("set-scan-roots").value = s.dest_scan_roots.join("\n");
    $("set-safety-margin").value = Math.round(s.safety_margin_bytes / (1024 * 1024));
    $("set-video-extensions").value = s.video_extensions.join(", ");
    $("set-ebook-extensions").value = s.ebook_extensions.join(", ");
    $("set-plex-url").value = s.plex_url;
    // Jetons masqués par le serveur : la valeur enregistrée sert à ne renvoyer que ce qui change.
    $("set-plex-token").value = s.plex_token || "";
    $("set-plex-token").dataset.saved = s.plex_token || "";
    $("set-radarr-url").value = s.radarr_url || "";
    $("set-radarr-key").value = s.radarr_api_key || "";
    $("set-radarr-key").dataset.saved = s.radarr_api_key || "";
    $("set-sonarr-url").value = s.sonarr_url || "";
    $("set-sonarr-key").value = s.sonarr_api_key || "";
    $("set-sonarr-key").dataset.saved = s.sonarr_api_key || "";
    $("set-tmdb-key").value = s.tmdb_api_key || "";
    $("set-tmdb-key").dataset.saved = s.tmdb_api_key || "";
    $("set-tmdb-lang").value = s.tmdb_language || "fr-FR";
    $("set-dry-run").checked = s.dry_run;
    $("set-delete-confirm").value = s.delete_confirm_text;
    $("set-1f-key").value = s.onefichier_api_key || "";
    $("set-1f-key").dataset.saved = s.onefichier_api_key || "";
    $("set-1f-staging").value = s.onefichier_staging_dir || "";
    $("set-1f-compression").value = s.onefichier_compression ?? 0;
    $("set-1f-archive-copy").value = s.archive_copy_dir || "";
    renderTransferWindows(s.transfer_windows);
    loadTransferStatus();
    $("set-1f-notify").value = s.notify_url || "";
    $("set-1f-notify").dataset.saved = s.notify_url || "";
    refreshIndexStatus();
    $("set-rd-films").value = s.restore_dir_films || "";
    $("set-rd-series").value = s.restore_dir_series || "";
    $("set-rd-ebooks").value = s.restore_dir_ebooks || "";
    $("set-1f-test-result").textContent = "";
    $("set-1f-test-result").className = "path-check-result muted small";
    for (const w of data.warnings || []) toast(w, "error");
  } catch (err) {
    toast(err.message, "error");
  }
}

let savingSettings = false;

// Un second clic pendant l'enregistrement n'envoie rien de plus.
async function saveSettings() {
  if (savingSettings) return;
  savingSettings = true;
  $("settings-save-btn").disabled = true;
  try {
    await saveSettingsOnce();
  } finally {
    savingSettings = false;
    $("settings-save-btn").disabled = false;
  }
}

async function saveSettingsOnce() {
  const patch = {
    source_dirs_films: linesToList($("set-source-films").value),
    source_dirs_series: linesToList($("set-source-series").value),
    source_dirs_ebooks: linesToList($("set-source-ebooks").value),
    dest_subdir_films: $("set-dest-subdir-films").value.trim().replace(/^\/+/, ""),
    dest_subdir_series: $("set-dest-subdir-series").value.trim().replace(/^\/+/, ""),
    dest_subdir_ebooks: $("set-dest-subdir-ebooks").value.trim().replace(/^\/+/, ""),
    source_dirs_adultes: linesToList($("set-source-adultes").value),
    dest_subdir_adultes: $("set-dest-subdir-adultes").value.trim().replace(/^\/+/, ""),
    dest_scan_roots: linesToList($("set-scan-roots").value),
    safety_margin_bytes: Math.max(0, Math.round(Number($("set-safety-margin").value || 0) * 1024 * 1024)),
    video_extensions: $("set-video-extensions").value.split(",").map((s) => s.trim()).filter(Boolean),
    ebook_extensions: $("set-ebook-extensions").value.split(",").map((s) => s.trim()).filter(Boolean),
    plex_url: $("set-plex-url").value.trim(),
    tmdb_language: $("set-tmdb-lang").value,
    dry_run: $("set-dry-run").checked,
    delete_confirm_text: $("set-delete-confirm").value.trim() || "SUPPRIMER",
  };
  // Clés masquées par le serveur : n'envoyer que ce qui a été retapé.
  const plexToken = $("set-plex-token").value;
  if (plexToken !== $("set-plex-token").dataset.saved) patch.plex_token = plexToken;
  patch.radarr_url = $("set-radarr-url").value.trim();
  patch.sonarr_url = $("set-sonarr-url").value.trim();
  for (const app of ["radarr", "sonarr"]) {
    const cle = $(`set-${app}-key`).value.trim();
    if (cle !== $(`set-${app}-key`).dataset.saved) patch[`${app}_api_key`] = cle;
  }
  const tmdbKey = $("set-tmdb-key").value.trim();
  if (tmdbKey !== $("set-tmdb-key").dataset.saved) patch.tmdb_api_key = tmdbKey;
  const key1f = $("set-1f-key").value.trim();
  if (key1f !== $("set-1f-key").dataset.saved) patch.onefichier_api_key = key1f;
  patch.onefichier_staging_dir = $("set-1f-staging").value.trim();
  patch.onefichier_compression = Math.min(9, Math.max(0, Number($("set-1f-compression").value || 0)));
  patch.archive_copy_dir = $("set-1f-archive-copy").value.trim();
  patch.transfer_windows = readTransferWindows();
  const notify = $("set-1f-notify").value.trim();
  if (notify !== $("set-1f-notify").dataset.saved && !notify.startsWith("•")) patch.notify_url = notify;
  patch.restore_dir_films = $("set-rd-films").value.trim();
  patch.restore_dir_series = $("set-rd-series").value.trim();
  patch.restore_dir_ebooks = $("set-rd-ebooks").value.trim();

  try {
    const data = await api("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
    toast("Réglages enregistrés.", "success");
    for (const w of data.warnings || []) toast(w, "error");
    closeDrawer("settings-drawer");
    await Promise.all([loadDisks(), loadPlexStatus(), refreshDryRunFlag()]);
    // Les résultats et la sélection ne correspondent plus aux réglages : on repart de zéro.
    state.results = [];
    state.searched = false;
    state.selected.clear();
    state.plan = { items: [], total_size: 0, warnings: [], merge: false, key: "" };
    planSeq++;
    rebuildControls();
    render();
    scheduleComputePlan();
    setEmpty(`<div class="glyph">✓</div><h3>Réglages mis à jour</h3><p>Relance une recherche pour voir les changements.</p>` +
      `<button class="btn btn-primary" data-action="search-all" type="button">Tout afficher</button>`);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function resetSettings() {
  if (!confirm("Réinitialiser tous les réglages aux valeurs du fichier .env ?")) return;
  try {
    await api("/api/settings/reset", { method: "POST" });
    toast("Réglages réinitialisés.", "success");
    await openSettingsDrawer();
    await Promise.all([loadDisks(), loadPlexStatus(), refreshDryRunFlag()]);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function checkPaths(textareaId, resultEl) {
  const paths = linesToList($(textareaId).value);
  if (paths.length === 0) {
    resultEl.textContent = "Aucun chemin à vérifier.";
    resultEl.className = "path-check-result muted small";
    return;
  }
  resultEl.textContent = "Vérification…";
  resultEl.className = "path-check-result muted small";
  const results = await Promise.all(paths.map(async (p) => {
    try {
      const r = await api(`/api/settings/check-path?path=${encodeURIComponent(p)}`);
      return { path: p, ok: r.readable };
    } catch (_) {
      return { path: p, ok: false };
    }
  }));
  const missing = results.filter((r) => !r.ok).map((r) => r.path);
  if (missing.length === 0) {
    resultEl.textContent = `Tous les chemins sont accessibles (${results.length}).`;
    resultEl.className = "path-check-result ok small";
  } else {
    resultEl.textContent = `Introuvable ou illisible dans le conteneur : ${missing.join(", ")}`;
    resultEl.className = "path-check-result error small";
  }
}

async function refreshDryRunFlag() {
  try {
    const cfg = await api("/api/config");
    $("dryrun-flag").classList.toggle("hidden", !cfg.dry_run);
    if (cfg.delete_confirm_text) state.deleteConfirmText = cfg.delete_confirm_text;
    state.tmdbEnabled = !!cfg.tmdb_enabled;
    if (cfg.forbidden_name_chars) setForbiddenNameChars(cfg.forbidden_name_chars);
    // ALLOW_DELETE=false (version 45) : sans suppression, les boutons qui retirent un fichier de sa source sont cachés.
    const sansSuppression = cfg.allow_delete === false;
    ["action-delete", "action-move", "disk-delete", "disk-move"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle("hidden", sansSuppression);
    });
    paintProtection(cfg);
  } catch (_) { /* ignore */ }
}

// Bloc « Protection » des Réglages (version 46) : lecture seule, pour savoir ce qui est réellement actif.
function paintProtection(cfg) {
  $("prot-delete").textContent = cfg.allow_delete === false
    ? "Suppression : désactivée (ALLOW_DELETE=false). Aucun fichier ne peut être retiré de sa source."
    : "Suppression : autorisée (ALLOW_DELETE=true).";
  $("prot-verify").textContent = cfg.verify_sha256 === false
    ? "Vérification des copies : tailles seulement (VERIFY_SHA256=false), plus rapide, moins sûre."
    : "Vérification des copies : empreintes SHA-256 (recommandé).";
  const protege = !!cfg.access_protected;
  const avecJeton = protege && !!jetonCourant();
  $("prot-access").textContent = protege
    ? (avecJeton
      ? "Modifications : protégées par un jeton (ACCESS_TOKEN). Ce navigateur a le jeton."
      : "Modifications : protégées par un jeton (ACCESS_TOKEN). Ce navigateur n'a pas encore le jeton : il sera demandé à la première modification.")
    : "Modifications : ouvertes à tout le réseau local (ACCESS_TOKEN vide). Définis ACCESS_TOKEN pour les protéger.";
  $("prot-token-btn").classList.toggle("hidden", !protege);
  $("prot-token-forget").classList.toggle("hidden", !avecJeton);
}

// ---------------------------------------------------------------------
// Accès aux médias adultes (version 36)
// ---------------------------------------------------------------------
// Le serveur vérifie le code et tient la session (cookie HttpOnly, hors de portée du JavaScript).
// Ici on n'affiche que l'état et on transmet les saisies. Cinq touches rapides sur le titre
// « Archivage » ouvrent la saisie du code ; rien ne l'indique à l'écran.

const ADULT_TAP_COUNT = 5;
const ADULT_TAP_WINDOW_MS = 2000;
const ADULT_CODE_FIELDS = ["adult-code-current", "adult-code-new", "adult-code-confirm"];
const adult = { codeSet: false, unlocked: false, tapTimes: [] };
// Génération des accès : incrémentée à chaque changement de session. Une réponse de liste lancée avant
// le changement porte l'ancienne valeur et n'est plus affichée (elle pourrait contenir des adultes).
let adultGen = 0;

function renderAdultState() {
  document.querySelectorAll('.media-tab-adult, [data-disk-kind="adultes"]').forEach((b) => b.classList.toggle("hidden", !adult.unlocked));
  $("adult-chip").classList.toggle("hidden", !adult.unlocked);
  $("adult-lock-btn").classList.toggle("hidden", !adult.unlocked);
  $("adult-code-btn").textContent = adult.codeSet ? "Changer le code" : "Créer le code";
  if (adult.unlocked) {
    $("adult-status").textContent = "Médias adultes : affichés dans ce navigateur, jusqu'à sa fermeture.";
  } else {
    $("adult-status").textContent = adult.codeSet
      ? "Médias adultes : masqués."
      : "Médias adultes : masqués. Aucun code n'est défini.";
  }
}

// Met à jour l'état ; si l'accès a changé, rafraîchit les vues déjà affichées.
function setAdultState(s) {
  const changed = adult.unlocked !== !!s.unlocked;
  adult.codeSet = !!s.code_set;
  adult.unlocked = !!s.unlocked;
  renderAdultState();
  if (changed) refreshAfterAdultChange();
}

async function loadAdultState() {
  try {
    setAdultState(await api("/api/adult/state"));
  } catch (_) { /* hors ligne : on garde l'état affiché */ }
}

function refreshAfterAdultChange() {
  adultGen++;                 // les réponses lancées avant ce changement ne seront plus affichées
  SAGAS.token++;              // et les sagas repartent de zéro
  SAGAS.loading = false;
  SAGAS.loaded = false;
  SAGAS.data = null;
  SAGAS.error = null;
  clearTimeout(SAGAS.timer);
  if (!adult.unlocked) purgeAdultData();
  if (!adult.unlocked && state.adultOnly) {
    setMediaType("films");  // relance la recherche : la catégorie Adultes n'est plus accessible
  } else if (state.searched) {
    runSearch($("search-term").value.trim(), { silent: true });
  }
  if (state.surface === "of") loadOneFichierView({ silent: true });
  if (state.surface === "disk") loadDiskLibrary({ force: true, silent: true });
  if (sagasVisible() && !SAGAS.loading) loadSagas();
  loadRecentJobs();
}

// Verrouillage : rien d'adulte ne reste à l'écran ni en mémoire. Les réponses encore en vol sont
// ignorées (adultGen, planSeq, SAGAS.token, personSeq) ; les vues ouvertes sont relues ensuite.
function purgeAdultData() {
  // Sélection : seuls les films adultes sortent (les autres restent cochés).
  const keep = state.results.filter((it) => !it.adult && state.selected.has(itemKey(it))).map(itemKey);
  state.selected.clear();
  keep.forEach((k) => state.selected.add(k));
  state.results = [];
  state.visible = [];
  state.error = null;
  state.loading = state.searched;
  state.plan = { items: [], total_size: 0, warnings: [], merge: false, key: "" };
  planSeq++;
  ["action-copy", "action-move", "action-delete", "action-1f"].forEach((id) => { $(id).disabled = true; });
  searchSig = null;

  // Fiche, personne, journal, identification : fermés et vidés.
  ["detail-drawer", "of-drawer", "person-drawer", "log-drawer"].forEach((id) => closeDrawer(id));
  clearBox($("detail-body"));
  clearBox($("of-drawer-body"));
  clearBox($("person-body"));
  clearBox($("log-content"));
  personData = null;
  personGroup = null;
  personReturnTo = null;
  personSeq++;
  creditsCache.clear();
  identifyCache.clear();
  closeIdentify();
  ["identify-query", "identify-year", "identify-name"].forEach((id) => { $(id).value = ""; });
  clearBox($("identify-results"));

  // Bibliothèque et sagas (les panneaux cachés aussi).
  clearBox($("results-grid"));
  clearBox($("list-body"));
  document.querySelectorAll("[data-sagas-panel]").forEach((p) => clearBox(p));

  // Disques et 1fichier.
  const d = state.disk;
  d.disks = [];
  d.loaded = false;
  d.error = null;
  d.integrity = null;
  d.hits = [];
  d.queued = null;
  d.adultOnly = false;
  d.selected.clear();
  const of = state.of;
  of.entries = [];
  of.queue = [];
  of.detailId = null;
  of.detailSig = null;
  of.disks = [];
  clearTimeout(of.pollTimer);
  clearBox($("of-grid"));
  clearBox($("of-pending"));
  clearBox($("of-genre"));

  // Tâches (cartes et connexions) et messages à l'écran.
  dropAllJobCards();
  clearBox($("toast-container"));

  // Les vues visibles repartent de cet état vide ; la relecture suit aussitôt.
  rebuildControls();
  render();
  renderDiskView();
  renderOfView();
  scheduleComputePlan();
}

function setAdultError(id, message) {
  const el = $(id);
  el.textContent = message;
  el.classList.toggle("hidden", !message);
}

function onAdultTitleTap() {
  if (adult.unlocked) return;
  const now = Date.now();
  adult.tapTimes = adult.tapTimes.filter((t) => now - t <= ADULT_TAP_WINDOW_MS);
  adult.tapTimes.push(now);
  if (adult.tapTimes.length < ADULT_TAP_COUNT) return;
  adult.tapTimes = [];
  openAdultUnlock();
}

function openAdultUnlock() {
  if (!adult.codeSet) {
    toast("Aucun code d'accès n'est défini. Crée-le dans Réglages › Comportement.", "info");
    return;
  }
  $("adult-unlock-code").value = "";
  setAdultError("adult-unlock-error", "");
  $("adult-unlock-modal").classList.remove("hidden");
  setTimeout(() => $("adult-unlock-code").focus(), 0);
}

function closeAdultUnlock() {
  $("adult-unlock-modal").classList.add("hidden");
  $("adult-unlock-code").value = "";
}

async function submitAdultUnlock(event) {
  event.preventDefault();
  const button = $("adult-unlock-ok");
  button.disabled = true;
  try {
    await api("/api/adult/unlock", { method: "POST", body: JSON.stringify({ code: $("adult-unlock-code").value }) });
    closeAdultUnlock();
    setAdultState({ code_set: true, unlocked: true });
  } catch (err) {
    setAdultError("adult-unlock-error", err.message);
    $("adult-unlock-code").focus();
    $("adult-unlock-code").select();
  } finally {
    button.disabled = false;
  }
}

async function lockAdult() {
  try {
    await api("/api/adult/lock", { method: "POST" });
    setAdultState({ code_set: adult.codeSet, unlocked: false });
  } catch (err) {
    toast(err.message, "error");
  }
}

function openAdultCode() {
  const change = adult.codeSet;
  $("adult-code-title").textContent = change ? "Changer le code" : "Créer le code";
  $("adult-code-hint").textContent = change
    ? "Saisis le code actuel. Les autres navigateurs devront ensuite saisir le nouveau code."
    : "Ce code est enregistré sur le serveur sous forme d'empreinte. Il sera demandé pour afficher les médias adultes.";
  $("adult-code-current-row").classList.toggle("hidden", !change);
  ADULT_CODE_FIELDS.forEach((id) => { $(id).value = ""; });
  setAdultError("adult-code-error", "");
  $("adult-code-modal").classList.remove("hidden");
  setTimeout(() => $(change ? "adult-code-current" : "adult-code-new").focus(), 0);
}

function closeAdultCode() {
  $("adult-code-modal").classList.add("hidden");
  ADULT_CODE_FIELDS.forEach((id) => { $(id).value = ""; });
}

async function submitAdultCode(event) {
  event.preventDefault();
  const next = $("adult-code-new").value.trim();
  if (next.length < 4 || next.length > 64) {
    setAdultError("adult-code-error", "Le code doit compter entre 4 et 64 caractères.");
    return;
  }
  if (next !== $("adult-code-confirm").value.trim()) {
    setAdultError("adult-code-error", "Les deux saisies ne sont pas identiques.");
    return;
  }
  const body = { new: next };
  if (adult.codeSet) body.current = $("adult-code-current").value;
  const button = $("adult-code-ok");
  button.disabled = true;
  try {
    await api("/api/adult/code", { method: "POST", body: JSON.stringify(body) });
    closeAdultCode();
    setAdultState({ code_set: true, unlocked: true });
    toast("Code enregistré.", "success");
  } catch (err) {
    setAdultError("adult-code-error", err.message);
  } finally {
    button.disabled = false;
  }
}

// Catégorie Adultes : déplacement d'un film du serveur (marque) ou d'un disque (sous-dossier adulte).
let categoryBusy = false;

async function setServerCategory(adultTarget) {
  if (categoryBusy) return;
  categoryBusy = true;
  try {
    const it = state.visible.find((x) => itemKey(x) === state.detailKey);
    if (!it || !it.path) {
      toast("Élément introuvable : relance la recherche.", "error");
      return;
    }
    await api("/api/library/category", { method: "POST", body: JSON.stringify({ paths: [it.path], adult: adultTarget }) });
    toast(adultTarget ? "Rangé dans la catégorie Adultes." : "Sorti de la catégorie Adultes.", "success");
    closeDrawer("detail-drawer");
    runSearch($("search-term").value.trim());
  } catch (err) {
    toast(err.message, "error");
  } finally {
    categoryBusy = false;
  }
}

async function diskCategory() {
  const rows = diskSelectedRows().filter((r) => r.connected);
  if (!rows.length) return;
  const toAdult = $("disk-category").dataset.adult === "1";
  try {
    const data = await api("/api/disk-library/category", {
      method: "POST",
      body: JSON.stringify({ paths: rows.map((r) => r.path), adult: toAdult }),
    });
    state.disk.selected.clear();
    const moved = (data.moved || []).length;
    const errors = (data.errors || []).length;
    if (errors) toast(`${moved} déplacé(s), ${errors} en erreur : ${data.errors[0].error}`, "error");
    else toast(`${moved} film(s) ${toAdult ? "rangé(s) dans Adultes" : "sorti(s) de Adultes"}.`, "success");
    loadDiskLibrary({ force: true, silent: true });
  } catch (err) {
    toast(err.message, "error");
  }
}

function initAdult() {
  // Une session expire côté serveur (24 h) : l'état est relu une fois par minute, appel léger.
  setInterval(loadAdultState, 60000);
  $("app-title").addEventListener("click", onAdultTitleTap);
  $("adult-chip").addEventListener("click", lockAdult);
  $("adult-lock-btn").addEventListener("click", lockAdult);
  $("adult-code-btn").addEventListener("click", openAdultCode);
  $("adult-unlock-form").addEventListener("submit", submitAdultUnlock);
  $("adult-unlock-cancel").addEventListener("click", closeAdultUnlock);
  $("adult-unlock-modal").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); closeAdultUnlock(); }
  });
  $("prot-token-btn").addEventListener("click", async () => {
    const saisi = await demanderJeton(false);
    if (saisi) enregistrerJeton(saisi);
    refreshDryRunFlag();
  });
  $("prot-token-forget").addEventListener("click", () => {
    enregistrerJeton("");
    refreshDryRunFlag();
    toast("Jeton oublié dans ce navigateur.", "info");
  });
  $("adult-code-form").addEventListener("submit", submitAdultCode);
  $("adult-code-cancel").addEventListener("click", closeAdultCode);
  $("adult-code-modal").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.stopPropagation(); closeAdultCode(); }
  });
}

// Test de connexion à Radarr ou Sonarr avec les réglages ENREGISTRÉS (version 49).
async function testArr(kind) {
  const out = $(`set-${kind}-result`);
  out.textContent = "Test en cours…";
  try {
    const data = await api("/api/arr/test", { method: "POST", body: JSON.stringify({ kind }) });
    out.textContent = `Connecté : ${data.app} ${data.version}`;
    out.className = "muted small";
  } catch (err) {
    out.textContent = err.message;
    out.className = "danger-text small";
  }
}
