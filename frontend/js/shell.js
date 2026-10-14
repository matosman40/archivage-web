"use strict";
// shell.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Tiroirs, feuille mobile, arrière-plan
// ---------------------------------------------------------------------

function syncBackdrop() {
  const drawerOpen = DRAWERS.some((id) => !$(id).classList.contains("hidden"));
  const sheetOpen = $("side").classList.contains("is-open");
  $("backdrop").classList.toggle("hidden", !(drawerOpen || sheetOpen));
  updateSelbarVisibility();
}

function openDrawer(id) {
  DRAWERS.forEach((d) => { if (d !== id) $(d).classList.add("hidden"); });
  $("side").classList.remove("is-open");
  $(id).classList.remove("hidden");
  syncBackdrop();
  const focusTarget = { "detail-drawer": "detail-close", "log-drawer": "log-close-btn", "settings-drawer": "settings-close-btn", "person-drawer": "person-close" }[id];
  if (focusTarget) setTimeout(() => $(focusTarget).focus({ preventScroll: true }), 0);
}

function closeDrawer(id) {
  $(id).classList.add("hidden");
  if (id === "detail-drawer") state.detailKey = null;
  syncBackdrop();
}

function openSheet() {
  DRAWERS.forEach((d) => $(d).classList.add("hidden"));
  $("side").classList.add("is-open");
  syncBackdrop();
}

function closeSheet() {
  $("side").classList.remove("is-open");
  syncBackdrop();
}

function closeAllOverlays() {
  DRAWERS.forEach((d) => $(d).classList.add("hidden"));
  state.detailKey = null;
  closeSheet();
  syncBackdrop();
}

// ---------------------------------------------------------------------
// Recherche, types de média, Plex
// ---------------------------------------------------------------------

// Numéro de la recherche en cours : une réponse arrivée après un changement
// d'onglet ou une nouvelle recherche est ignorée (sinon elle écraserait la bonne).
let searchSeq = 0;
let searchSig = null;  // contenu de la dernière liste reçue : une relecture identique ne redessine rien

// Relit la liste pendant qu'elle se met à jour côté serveur (cache de recherche).
// `silent` : pas de vidange de la sélection ni d'écran de chargement.
async function runSearch(term, { silent = false } = {}) {
  const seq = ++searchSeq;
  const gen = adultGen;
  const mediaType = state.mediaType;
  clearTimeout(state.searchPoll);
  if (!silent) {
    state.loading = true;
    state.searched = true;
    state.error = null;
    state.selected.clear();
    state.plan = { items: [], total_size: 0, warnings: [], merge: false, key: "" };
    searchSig = null;
    render();
  }
  let results = null;
  let error = null;
  let refreshing = false;
  try {
    const data = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({ media_type: state.adultOnly ? "adultes" : mediaType, term }),
    });
    results = data.items;
    refreshing = !!data.refreshing;
  } catch (err) {
    error = err.message;
  }
  // Réponse périmée : nouvelle recherche, autre onglet, ou accès adulte changé depuis l'envoi.
  if (seq !== searchSeq || gen !== adultGen) return;
  state.loading = false;
  state.searchRefreshing = refreshing;
  const sig = JSON.stringify(results);
  const changed = !silent || sig !== searchSig || error !== state.error;
  if (changed) {
    state.results = results || [];
    state.error = error;
    searchSig = sig;
    rebuildControls();
    render();
    updateSelectionUi();
    computePlan();
  } else {
    renderToolbar();  // liste identique : seul le compteur (« en cours d'actualisation ») peut changer
  }
  if (refreshing && !error) {
    state.searchPoll = setTimeout(() => {
      if (state.mediaType === mediaType) runSearch($("search-term").value.trim(), { silent: true });
    }, 2000);
  }
}

function setMediaType(type) {
  closeAllOverlays();  // un tiroir ouvert ne reste pas par-dessus le nouvel onglet
  closeAutoView();
  if (type === "automatisations") { closeOneFichierView(); closeDiskView(); openAutoView(); return; }
  if (type === "1fichier") { closeDiskView(); openOneFichierView(); return; }
  if (type === "disques") { closeOneFichierView(); openDiskView(); return; }
  closeOneFichierView();
  closeDiskView();
  clearTimeout(state.searchPoll);
  state.searchRefreshing = false;
  state.adultOnly = type === "adultes";
  state.mediaType = type === "adultes" ? "films" : type;
  state.filters = { plex: "all", genre: "", quality: "", added: "all", tmdb: "all" };
  state.results = [];
  state.selected.clear();
  state.sortKey = "name";
  state.sortDir = "asc";
  document.querySelectorAll(".media-tab").forEach((b) => {
    const active = b.dataset.media === type;
    b.classList.toggle("is-active", active);
    b.setAttribute("aria-selected", String(active));
  });
  rebuildControls();
  runSearch($("search-term").value.trim());
}

async function loadPlexStatus() {
  try {
    const s = await api("/api/plex/status");
    state.plexEnabled = !!s.enabled;
    if (!s.enabled) {
      $("plex-status").textContent = "Plex non configuré";
    } else if (s.loaded) {
      $("plex-status").textContent = `Plex : ${NF0.format(s.movie_titles)} films (${NF0.format(s.movies_watched)} vus) · ${NF0.format(s.series_titles)} séries (${NF0.format(s.series_watched)} terminées)`;
    } else {
      $("plex-status").textContent = "Plex configuré — actualise le cache";
    }
  } catch (_) {
    $("plex-status").textContent = "";
  }
  applyMediaVisibility();
  renderToolbar();
}

async function refreshPlex() {
  $("plex-refresh-btn").disabled = true;
  try {
    await api("/api/plex/refresh", { method: "POST" });
    await loadPlexStatus();
    toast("Plex actualisé.", "success");
    if (state.searched) runSearch($("search-term").value.trim());
  } catch (err) {
    toast(err.message, "error");
  } finally {
    $("plex-refresh-btn").disabled = false;
  }
}

// ---------------------------------------------------------------------
// Rendu global
// ---------------------------------------------------------------------

function render() {
  derive();
  applyMediaVisibility();
  renderSortControls();
  renderStats();
  renderToolbar();
  renderContent();
  refreshSelectionDom();
  updateSelectionUi();
  if (!$("detail-drawer").classList.contains("hidden") && state.detailKey) {
    const it = currentItem();
    if (it) renderDetail(it);
    else closeDrawer("detail-drawer");
  }
}

// ---------------------------------------------------------------------
// Actions (copier / déplacer / supprimer) + confirmation
// ---------------------------------------------------------------------

let actionBusy = false;   // une action à la fois : un clic de plus pendant qu'elle se prépare ne relance rien
let confirmOpen = false;  // une seule fenêtre de confirmation à la fois

// Titres affichés dans la confirmation : ceux de la sélection (à défaut, le nom du dossier ou du fichier).
function planNames(items) {
  const names = new Map();
  for (const it of state.results) {
    const title = itemTitle(it);
    names.set(itemKey(it), title);
    if (it.path) names.set(it.path, title);
    for (const p of it.paths || []) names.set(p, title);
  }
  return items.map((p) => names.get(p) || String(p).split("/").pop());
}

async function startAction(operation, lot = null) {
  if (actionBusy) return;
  actionBusy = true;
  const buttons = [$("action-copy"), $("action-move"), $("action-delete"), $("action-1f")];
  buttons.forEach((b) => (b.disabled = true));
  try {
    await runAction(operation, lot);
  } finally {
    actionBusy = false;
    buttons.forEach((b) => (b.disabled = state.plan.items.length === 0));
  }
}

async function runAction(operation, lot) {
  if (!lot) {
    if (state.selected.size === 0) {
      toast("Aucun élément sélectionné.", "info");
      return;
    }
    // Le plan doit correspondre à la sélection actuelle : sinon on ne supprime pas « à peu près ».
    if (state.plan.key !== selectionKey()) {
      toast("Le plan de la sélection est en cours de calcul : relance l'action dans un instant.", "info");
      scheduleComputePlan();
      return;
    }
  }
  const plan = lot || state.plan;
  if (plan.items.length === 0) return;

  let destination = null;
  if (operation === "copy" || operation === "move") {
    destination = currentDestination();
    if (!isPlausibleDestination(destination)) {
      toast('Choisis ou saisis un vrai dossier de disque externe (pas juste "/" ou une racine de scan).', "error");
      return;
    }
  }
  const archiveMode = operation === "archive_1fichier" && (state.mediaType === "films" || state.mediaType === "series");
  if (operation === "archive_1fichier") destination = currentDestination();

  // Série(s) : on propose les saisons à envoyer (une archive par saison) ou la série entière
  const seasonGroups = operation === "archive_1fichier" && state.mediaType === "series" && !lot
    ? await loadSeasonGroups(plan.items)
    : null;
  const ok = await confirmAction(operation, destination, {
    archiveMode, count: plan.items.length, total: plan.total_size, seasonGroups, names: planNames(plan.items),
  });
  if (!ok.confirmed) return;

  try {
    let data;
    if (operation === "archive_1fichier") {
      const body = { paths: ok.paths || plan.items };
      if (ok.after && ok.after !== "keep") body.after = ok.after;
      if (ok.after === "move") body.destination = destination;
      if (ok.after === "delete") body.confirm_delete_text = state.deleteConfirmText;
      data = await api("/api/1fichier/archive", { method: "POST", body: JSON.stringify(body) });
    } else {
      const body = {
        operation,
        media_type: state.mediaType,
        items: plan.items,
        merge: plan.merge,
      };
      if (operation !== "delete") body.destination = destination;
      if (operation === "delete") body.confirm_delete_text = state.deleteConfirmText;
      data = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    }
    trackJob(data.job_id, operation, { live: true });
    toast("Opération démarrée.", "success");
    if (window.matchMedia("(max-width: 1099px)").matches) openSheet();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function loadSeasonGroups(items) {
  const groups = await Promise.all(items.map(async (p) => {
    try {
      const r = await api(`/api/series/seasons?path=${encodeURIComponent(p)}`);
      return { name: r.name, seasons: (r.seasons || []).filter((s) => s.season !== null && s.season !== undefined) };
    } catch (err) {
      toast(err.message, "error");
      return { name: p.split("/").pop(), seasons: [] };
    }
  }));
  return groups.some((g) => g.seasons.length) ? groups : null;
}

// Liste des éléments concernés, sous le texte de la confirmation (créée à la demande).
function confirmNamesBox() {
  let box = $("confirm-names");
  if (!box) {
    box = document.createElement("ul");
    box.id = "confirm-names";
    box.className = "confirm-names";
    box.style.maxHeight = "30vh";
    box.style.overflowY = "auto";
    box.style.margin = "8px 0";
    box.style.paddingLeft = "1.2em";
    $("confirm-body").insertAdjacentElement("afterend", box);
  }
  return box;
}

function fillConfirmNames(names) {
  const box = confirmNamesBox();
  const shown = names.slice(0, 200);
  box.innerHTML = shown.map((n) => `<li>${esc(n)}</li>`).join("")
    + (names.length > shown.length ? `<li class="muted">… et ${NF0.format(names.length - shown.length)} autre(s)</li>` : "");
  box.classList.toggle("hidden", names.length === 0);
  return box;
}

function clearConfirmNames() {
  const box = $("confirm-names");
  if (box) { box.innerHTML = ""; box.classList.add("hidden"); }
}

function confirmAction(operation, destination, { archiveMode = false, count = state.plan.items.length,
    total = state.plan.total_size, seasonGroups = null, names = [] } = {}) {
  return new Promise((resolve) => {
    if (confirmOpen) { resolve({ confirmed: false }); return; }
    confirmOpen = true;
    const overlay = $("confirm-modal");
    const okBtn = $("confirm-ok");
    const cancelBtn = $("confirm-cancel");
    const input = $("confirm-delete-input");
    const afterBox = $("confirm-after");
    const seasonBox = $("confirm-seasons");
    const seasonList = $("confirm-season-list");
    const namesBox = fillConfirmNames(names);
    const scopeRadios = [...document.querySelectorAll('input[name="confirm-scope"]')];
    const radios = [...document.querySelectorAll('input[name="confirm-after"]')];
    const moveRadio = radios.find((r) => r.value === "move");
    const showAfter = operation === "archive_1fichier" && archiveMode;
    const canMove = isPlausibleDestination(destination);
    const usesSeasons = operation === "archive_1fichier" && !!seasonGroups;

    $("confirm-title").textContent = {
      copy: "Copier la sélection",
      move: "Déplacer la sélection",
      delete: "Supprimer définitivement",
      archive_1fichier: "Envoyer sur 1fichier",
    }[operation];

    seasonList.innerHTML = usesSeasons ? seasonGroups.map((g) => `
      <div class="confirm-show"><strong>${esc(g.name)}</strong>${g.seasons.map((s) => `
        <label class="check-row confirm-season">
          <input type="checkbox" data-season-path="${esc(s.path)}" data-season-size="${Number(s.size) || 0}" ${s.sent ? "" : "checked"}>
          <span>Saison ${esc(s.season)} · ${esc(s.episodes)} épisode${s.episodes > 1 ? "s" : ""} · ${esc(formatSize(s.size))}${s.sent ? ` <span class="chip ok">déjà sur 1fichier</span>` : ""}</span>
        </label>`).join("")}</div>`).join("") : "";
    seasonBox.classList.toggle("hidden", !usesSeasons);
    scopeRadios.forEach((r) => { r.checked = r.value === "seasons"; });

    const seasonsOn = () => usesSeasons && (scopeRadios.find((r) => r.checked) || { value: "seasons" }).value === "seasons";
    const selectedSeasons = () => [...seasonList.querySelectorAll('input[type="checkbox"]:checked')];
    const summary = () => {
      if (seasonsOn()) {
        const boxes = selectedSeasons();
        return { n: boxes.length, size: boxes.reduce((sum, b) => sum + (Number(b.dataset.seasonSize) || 0), 0) };
      }
      return { n: count, size: total };
    };
    const noneSelected = () => seasonsOn() && summary().n === 0;

    const baseBody = () => {
      const sm = summary();
      const unit = seasonsOn() ? "saison" : "élément";
      let body = noneSelected()
        ? "Aucune saison sélectionnée."
        : `${NF0.format(sm.n)} ${unit}${sm.n > 1 ? "s" : ""}, ${formatSize(sm.size)}.`;
      if (destination && operation !== "archive_1fichier") body += ` Destination : ${destination}.`;
      if (operation === "delete") body += " Cette action est irréversible.";
      if (operation === "archive_1fichier") {
        body += seasonsOn()
          ? " Une archive 7z chiffrée par saison : tmdb-tv-<id>-S<nn>.7z, rangée dans le dossier de la série."
          : " Une archive 7z chiffrée par élément : un film → dossier tmdb-<id>, une série entière → tmdb-tv-<id>, un dossier d'ebooks → ebook-<empreinte> (jaquette du premier livre).";
      }
      return body;
    };
    const afterText = {
      keep: " Les fichiers locaux ne sont pas supprimés.",
      delete: " La copie locale sera supprimée après la sauvegarde vérifiée. Tape " + state.deleteConfirmText + " pour confirmer.",
      move: ` La copie locale sera copiée sur ${destination || "le disque choisi"}, vérifiée, puis retirée du serveur.`,
    };
    const currentAfter = () => (radios.find((r) => r.checked) || { value: "keep" }).value;
    const after = () => (showAfter ? currentAfter() : "keep");
    const needsText = () => operation === "delete" || (showAfter && after() === "delete");

    afterBox.classList.toggle("hidden", !showAfter);
    radios.forEach((r) => { r.checked = r.value === "keep"; });
    moveRadio.disabled = !canMove;
    moveRadio.parentElement.title = canMove ? "" : "Choisis d'abord un disque de destination";

    function refresh() {
      const text = needsText();
      input.classList.toggle("hidden", !text);
      if (!text) input.value = "";
      seasonList.classList.toggle("hidden", !seasonsOn());
      namesBox.classList.toggle("hidden", seasonsOn() || names.length === 0);
      okBtn.disabled = noneSelected() || (text && input.value.trim() !== state.deleteConfirmText);
      $("confirm-body").textContent = baseBody() + (showAfter ? afterText[after()] : (operation === "archive_1fichier" ? afterText.keep : ""))
        + (operation === "delete" || (!showAfter && needsText()) ? ` Tape ${state.deleteConfirmText} pour confirmer.` : "");
    }

    const onInput = () => { okBtn.disabled = noneSelected() || (needsText() && input.value.trim() !== state.deleteConfirmText); };
    const onKey = (e) => {
      if (e.key === "Enter" && !okBtn.disabled) onOk();
    };
    // Échap ferme la fenêtre où que se trouve le focus (pas seulement dans le champ de saisie).
    const onEsc = (e) => {
      if (e.key === "Escape") onCancel();
    };

    function cleanup() {
      overlay.classList.add("hidden");
      input.removeEventListener("input", onInput);
      input.removeEventListener("keydown", onKey);
      document.removeEventListener("keydown", onEsc);
      radios.forEach((r) => r.removeEventListener("change", refresh));
      scopeRadios.forEach((r) => r.removeEventListener("change", refresh));
      seasonList.removeEventListener("change", refresh);
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      okBtn.disabled = false;
      input.classList.add("hidden");
      afterBox.classList.add("hidden");
      seasonBox.classList.add("hidden");
      seasonList.innerHTML = "";
      clearConfirmNames();
      confirmOpen = false;
    }
    function onOk() {
      if (okBtn.disabled) return;
      const chosen = after();
      const paths = seasonsOn() ? selectedSeasons().map((b) => b.dataset.seasonPath) : null;
      cleanup();
      resolve({ confirmed: true, after: showAfter ? chosen : null, paths });
    }
    function onCancel() {
      cleanup();
      resolve({ confirmed: false });
    }

    refresh();
    overlay.classList.remove("hidden");
    input.addEventListener("input", onInput);
    input.addEventListener("keydown", onKey);
    document.addEventListener("keydown", onEsc);
    radios.forEach((r) => r.addEventListener("change", refresh));
    scopeRadios.forEach((r) => r.addEventListener("change", refresh));
    seasonList.addEventListener("change", refresh);
    if (needsText()) setTimeout(() => input.focus(), 0);
    else setTimeout(() => okBtn.focus(), 0);
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
  });
}
