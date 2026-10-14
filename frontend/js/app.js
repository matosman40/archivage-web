"use strict";
// app.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Événements
// ---------------------------------------------------------------------

function bindEvents() {
  // Onglets de média
  document.querySelectorAll(".media-tab").forEach((b) => {
    b.addEventListener("click", () => setMediaType(b.dataset.media));
  });

  // Recherche
  $("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.surface === "disk") { applyDiskSearch($("search-term").value); return; }
    runSearch($("search-term").value.trim());
  });
  $("search-term").addEventListener("input", () => {
    if (state.surface === "disk") applyDiskSearch($("search-term").value);
  });

  // Filtres
  document.querySelectorAll("#plex-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      state.filters.plex = b.dataset.plex;
      render();
    });
  });
  $("filter-genre").addEventListener("change", (e) => { state.filters.genre = e.target.value; render(); });
  $("filter-quality").addEventListener("change", (e) => { state.filters.quality = e.target.value; render(); });
  $("filter-added").addEventListener("change", (e) => { state.filters.added = e.target.value; render(); });
  $("filter-tmdb").addEventListener("change", (e) => { state.filters.tmdb = e.target.value; render(); });
  $("reset-filters").addEventListener("click", resetFilters);

  // Tri
  $("sort-select").addEventListener("change", (e) => {
    state.sortKey = e.target.value;
    state.sortDir = DEFAULT_DIR[state.sortKey] || "asc";
    render();
  });
  $("sort-dir").addEventListener("click", () => {
    state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    render();
  });

  // Affichage
  document.querySelectorAll("#view-seg .seg-btn").forEach((b) => {
    b.addEventListener("click", () => {
      state.view = b.dataset.view;
      localStorage.setItem("archivage.view", state.view);
      render();
    });
  });

  // Sélection globale
  $("select-all").addEventListener("change", (e) => toggleSelectAllVisible(e.target.checked));

  // Actions
  $("action-copy").addEventListener("click", () => startAction("copy"));
  $("action-move").addEventListener("click", () => startAction("move"));
  $("action-delete").addEventListener("click", () => startAction("delete"));
  $("action-1f").addEventListener("click", () => startAction("archive_1fichier"));
  $("select-watched").addEventListener("click", selectWatchedVisible);
  document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", scheduleComputePlan));
  $("selbar-clear").addEventListener("click", () => {
    state.selected.clear();
    refreshSelectionDom();
    updateSelectionUi();
    scheduleComputePlan();
  });
  $("selbar-open").addEventListener("click", openSheet);
  $("sheet-close").addEventListener("click", closeSheet);
  $("backdrop").addEventListener("click", closeAllOverlays);

  $("destination-select").addEventListener("change", updateDestinationSpace);
  $("disks-refresh-btn").addEventListener("click", loadDisks);

  $("jobs-indicator").addEventListener("click", () => {
    if (window.matchMedia("(max-width: 1099px)").matches) openSheet();
    else $("jobs-list").scrollIntoView({ behavior: "smooth", block: "center" });
  });

  // Fiche
  $("detail-close").addEventListener("click", () => closeDrawer("detail-drawer"));
  $("detail-drawer").addEventListener("click", (e) => {
    const seasonBtn = e.target.closest("[data-season-action]");
    if (seasonBtn) { seasonAction(seasonBtn); return; }
    if (e.target.closest("[data-server-identify]")) { openServerIdentify(); return; }
    const category = e.target.closest("[data-server-category]");
    if (category) { setServerCategory(category.dataset.serverCategory === "1"); return; }
    const btn = e.target.closest("[data-ebook-cover]");
    if (btn) chooseEbookCover(btn.dataset.ebookCover, (url) => afterEbookCover(btn.dataset.ebookCover, url));
  });
  $("detail-prev").addEventListener("click", () => detailNav(-1));
  $("detail-next").addEventListener("click", () => detailNav(1));

  // Journal et réglages
  $("tab-log-btn").addEventListener("click", openLogDrawer);
  $("log-close-btn").addEventListener("click", () => closeDrawer("log-drawer"));
  $("tab-settings-btn").addEventListener("click", openSettingsDrawer);
  $("set-1f-test").addEventListener("click", test1fichier);
  $("set-1f-export").addEventListener("click", exportOneFichierIndex);
  $("set-1f-checks-run").addEventListener("click", runOneFichierChecks);

  // Onglet 1fichier : filtres, tri, grille, envois incomplets, fiche
  $("of-search").addEventListener("input", renderOfView);
  $("of-kind").addEventListener("change", renderOfView);
  $("of-status").addEventListener("change", renderOfView);
  $("of-genre").addEventListener("change", renderOfView);
  $("of-tmdb").addEventListener("change", renderOfView);
  $("of-cache-clear").addEventListener("click", clearOfCache);
  $("of-sagas-btn").addEventListener("click", () => setSagasSurface("of", !state.of.sagasOn));
  $("disk-sagas-btn").addEventListener("click", () => setSagasSurface("disk", !state.disk.sagasOn));
  $("of-sort").addEventListener("change", () => {
    // chaque critère a son sens naturel (note, taille… du plus grand au plus petit)
    state.of.dir = (OF_SORTS[$("of-sort").value] || OF_SORTS.title).dir;
    renderOfView();
  });
  $("of-sort-dir").addEventListener("click", () => {
    state.of.dir = state.of.dir === "asc" ? "desc" : "asc";
    renderOfView();
  });
  $("of-refresh-meta").addEventListener("click", refreshOfMeta);
  $("of-verify").addEventListener("click", verifyOfFiles);
  $("of-settings").addEventListener("click", openSettingsDrawer);
  $("of-view").addEventListener("click", onOfClick);
  $("of-drawer-body").addEventListener("click", onOfClick);

  // Onglet Disques
  $("disk-view").addEventListener("click", onDiskClick);
  $("disk-view").addEventListener("change", onDiskChange);
  bindAutomations();
  initIdentify();
  initPerson();
  initAdult();
  $("disk-search").addEventListener("input", () => applyDiskSearch($("disk-search").value));
  $("disk-everywhere-list").addEventListener("click", (e) => {
    const hit = e.target.closest("[data-disk-hit]");
    if (hit) openDiskHit(Number(hit.dataset.diskHit));
  });
  $("disk-refresh").addEventListener("click", () => loadDiskLibrary({ force: true }));
  $("disk-select-all").addEventListener("click", toggleDiskSelectAll);
  $("disk-copy").addEventListener("click", () => diskTransfer("copy"));
  $("disk-move").addEventListener("click", () => diskTransfer("move"));
  $("disk-category").addEventListener("click", diskCategory);
  $("disk-1f").addEventListener("click", diskToOneFichier);
  bindDiskExtras();
  $("set-radarr-test").addEventListener("click", () => testArr("radarr"));
  $("set-sonarr-test").addEventListener("click", () => testArr("sonarr"));
  $("disk-delete").addEventListener("click", diskDelete);
  $("of-drawer-close").addEventListener("click", () => closeDrawer("of-drawer"));
  // Une affiche qui ne charge pas est retirée : le dégradé reste visible à la place.
  document.addEventListener("error", (event) => {
    if (event.target && event.target.classList && event.target.classList.contains("of-img")) event.target.remove();
  }, true);
  $("settings-close-btn").addEventListener("click", () => closeDrawer("settings-drawer"));
  $("settings-save-btn").addEventListener("click", saveSettings);
  $("set-tw-add").addEventListener("click", () => {
    $("set-tw-list").insertAdjacentHTML("beforeend", twRowHtml(TW_NEW_ROW));
  });
  $("set-tw-defaults").addEventListener("click", () => renderTransferWindows(TW_DEFAULTS));
  $("set-tw-list").addEventListener("click", (e) => {
    const remove = e.target.closest("[data-tw-remove]");
    if (remove) remove.closest("[data-tw-row]").remove();
  });
  $("settings-reset-btn").addEventListener("click", resetSettings);
  document.querySelectorAll("[data-check-target]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const resultEl = btn.parentElement.querySelector(".path-check-result");
      checkPaths(btn.dataset.checkTarget, resultEl);
    });
  });

  $("plex-refresh-btn").addEventListener("click", refreshPlex);

  // Délégation : cartes, lignes, en-têtes, états vides, copies
  document.addEventListener("click", onDelegatedClick);
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.matches && t.matches("input[data-select]")) {
      setSelected(Number(t.dataset.select), t.checked);
    }
    if (t.matches && t.matches("select[data-job-priority]")) {
      changeJobPriority(t.dataset.jobPriority, t.value, t);
    }
  });

  // Clic sur une ligne (hors case à cocher et boutons) = ouvrir la fiche
  $("list-body").addEventListener("click", (e) => {
    if (e.target.closest("input, button, a")) return;
    const tr = e.target.closest("tr[data-row]");
    if (tr) openDetail(Number(tr.dataset.row));
  });

  // Clavier
  document.addEventListener("keydown", onKeyDown);
}

function resetFilters() {
  state.filters = { plex: "all", genre: "", quality: "", added: "all", tmdb: "all" };
  render();
}

function onDelegatedClick(e) {
  const t = e.target;

  const open = t.closest("[data-open]");
  if (open && !open.closest("#detail-drawer")) {
    openDetail(Number(open.dataset.open));
    return;
  }
  const sortBtn = t.closest("[data-sort]");
  if (sortBtn) {
    const key = sortBtn.dataset.sort;
    if (state.sortKey === key) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    else { state.sortKey = key; state.sortDir = DEFAULT_DIR[key] || "asc"; }
    render();
    return;
  }
  const copy = t.closest("[data-copy]");
  if (copy) { copyText(copy.dataset.copy); return; }
  if (t.closest("[data-detail-toggle]")) { toggleDetailSelection(); return; }
  const action = t.closest("[data-action]");
  if (action) {
    const name = action.dataset.action;
    if (name === "search-all") { $("search-term").value = ""; runSearch(""); }
    if (name === "reset-filters") resetFilters();
    if (name === "retry") runSearch($("search-term").value.trim());
    if (name === "sagas-retry") loadSagas();
  }
}

function onKeyDown(e) {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");

  if (e.key === "Escape") {
    if (!$("confirm-modal").classList.contains("hidden")) return; // géré par la modale
    if (!$("person-drawer").classList.contains("hidden")) { backFromPerson(); return; }
    if (!$("detail-drawer").classList.contains("hidden")) { closeDrawer("detail-drawer"); return; }
    if (DRAWERS.some((d) => !$(d).classList.contains("hidden"))) { closeAllOverlays(); return; }
    if ($("side").classList.contains("is-open")) { closeSheet(); return; }
    return;
  }

  if (typing) return;

  if (e.key === "/") {
    e.preventDefault();
    $("search-term").focus();
    $("search-term").select();
    return;
  }

  if (!$("detail-drawer").classList.contains("hidden")) {
    if (e.key === "ArrowLeft") { e.preventDefault(); detailNav(-1); }
    if (e.key === "ArrowRight") { e.preventDefault(); detailNav(1); }
  }
}

// ---------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------

async function init() {
  bindEvents();
  document.querySelectorAll("#view-seg .seg-btn").forEach((b) => {
    const on = b.dataset.view === state.view;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  await refreshDryRunFlag();
  await loadAdultState();
  await loadPlexStatus();
  loadDisks();
  loadRecentJobs();
  rebuildControls();
  // Affiche la bibliothèque dès l'ouverture (recherche vide = tout le contenu).
  runSearch("");
}

document.addEventListener("DOMContentLoaded", init);
