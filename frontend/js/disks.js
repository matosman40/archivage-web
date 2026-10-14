"use strict";
// disks.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Onglet Disques : bibliothèque des disques de sauvegarde
// Les disques connectés sont scannés ; les disques hors ligne restent consultables
// (lecture seule). Une action ne touche qu'aux éléments d'un disque CONNECTÉ.
// ---------------------------------------------------------------------

const DISK_KIND_LABEL = { films: ["film", "films"], series: ["série", "séries"], ebooks: ["ebook", "ebooks"] };
const DISK_POLL_MS = 20000;

function diskTitle(r) {
  return (r.meta && r.meta.title) || r.name;
}

// Infos TMDB d'un élément de disque : la fiche porte l'identifiant (une fiche « introuvable » n'en a pas).
function diskTmdbPass(filter, kind, it) {
  return tmdbPass(filter, hasTmdb(kind, it.meta && it.meta.tmdb_id));
}

function diskRows() {
  const d = state.disk;
  const q = $("disk-search").value.trim().toLowerCase();
  const rows = [];
  for (const disk of d.disks) {
    if (d.diskFilter !== "all" && disk.path !== d.diskFilter) continue;
    for (const it of (disk.items && disk.items[d.kind]) || []) {
      if (d.adultOnly && !it.adult) continue;
      if (q && !diskMatches(it, q)) continue;
      if (!diskTmdbPass(d.tmdbFilter, d.kind, it)) continue;
      rows.push({ ...it, diskPath: disk.path, diskLabel: disk.label, connected: !!disk.connected });
    }
  }
  return rows.sort((a, b) => COLLATOR_NUMERIC.compare(diskTitle(a), diskTitle(b)));
}

// Recherche par nom, titre TMDB, titre original, auteur ou année.
function diskMatches(it, q) {
  const m = it.meta || {};
  const hay = [it.name, m.title, m.original_title, m.author, String(m.year || "")].join(" ").toLowerCase();
  return hay.includes(q);
}

// Recherche sur l'ensemble des disques connus, tous types confondus.
function diskEverywhereRows(q) {
  const out = [];
  for (const disk of state.disk.disks) {
    for (const kind of ["films", "series", "ebooks"]) {
      for (const it of (disk.items && disk.items[kind]) || []) {
        if (!diskMatches(it, q)) continue;
        if (!diskTmdbPass(state.disk.tmdbFilter, kind, it)) continue;
        out.push({ ...it, kind, diskPath: disk.path, diskLabel: disk.label, connected: !!disk.connected });
      }
    }
  }
  return out.sort((a, b) => COLLATOR_NUMERIC.compare(diskTitle(a), diskTitle(b)));
}

function diskQuery() {
  return $("disk-search").value.trim().toLowerCase();
}

function diskIsShown(h) {
  const d = state.disk;
  return h.kind === d.kind && (d.diskFilter === "all" || h.diskPath === d.diskFilter);
}

// Résultats : le dossier affiché (type et disque choisis) puis l'ensemble des disques.
function renderDiskSearch() {
  const d = state.disk;
  const q = diskQuery();
  const scope = $("disk-scope");
  const box = $("disk-everywhere");
  if (!d.loaded || !q) {
    d.hits = [];
    scope.classList.add("hidden");
    box.classList.add("hidden");
    $("disk-everywhere-list").innerHTML = "";
    return;
  }
  const disk = d.disks.find((x) => x.path === d.diskFilter);
  const where = disk ? disk.label : "tous les disques";
  const shown = diskRows().length;
  scope.textContent = `Dans ce dossier (${DISK_KIND_LABEL[d.kind][1]} · ${where}) : ${NF0.format(shown)} résultat(s)`;
  scope.classList.remove("hidden");

  const hits = diskEverywhereRows(q);
  d.hits = hits;
  box.classList.remove("hidden");
  $("disk-everywhere-title").textContent = hits.length
    ? `Ensemble des disques : ${NF0.format(hits.length)} résultat(s)`
    : "Ensemble des disques : aucun résultat";
  $("disk-everywhere-list").innerHTML = hits.map((h, i) => {
    const here = diskIsShown(h) ? " · affiché ci-dessus" : "";
    return `<li><button type="button" class="disk-hit${here ? " is-here" : ""}" data-disk-hit="${i}"
      title="Afficher cet élément dans son disque et son type">
      <strong class="disk-hit-title">${esc(diskTitle(h))}</strong>
      <span class="muted small">${esc(DISK_KIND_LABEL[h.kind][0])} · ${esc(h.diskLabel)} · ${esc(h.folder || "")}${h.connected ? "" : " · hors ligne"}${here} · ${esc(formatSize(h.size))}</span>
    </button></li>`;
  }).join("");
}

// Clic sur un résultat hors du dossier affiché : on y va, la recherche reste.
function openDiskHit(index) {
  const h = state.disk.hits[index];
  if (!h) return;
  state.disk.kind = h.kind;
  state.disk.diskFilter = h.diskPath;
  state.disk.selected.clear();
  renderDiskView();
  $("disk-list").scrollIntoView({ block: "start", behavior: "smooth" });
}

// La recherche de l'en-tête pilote celle de l'onglet Disques (et réciproquement).
function applyDiskSearch(text) {
  $("disk-search").value = text;
  $("search-term").value = text;
  renderDiskSearch();
  renderDiskList();
  renderDiskActions();
}

// Seuls les éléments visibles, sur un disque connecté, sont concernés par une action.
function diskSelectedRows() {
  return diskRows().filter((r) => r.connected && state.disk.selected.has(r.path));
}

function diskPruneSelection() {
  const valid = new Set();
  for (const disk of state.disk.disks) {
    if (!disk.connected) continue;
    for (const list of Object.values(disk.items || {})) for (const it of list) valid.add(it.path);
  }
  for (const p of [...state.disk.selected]) if (!valid.has(p)) state.disk.selected.delete(p);
}

function diskTotalSize(rows) {
  return rows.reduce((sum, r) => sum + (r.size || 0), 0);
}

function diskCardHtml(disk) {
  const kinds = disk.items || {};
  const count = (k) => (kinds[k] || []).length;
  const total = Object.values(kinds).flat().reduce((sum, it) => sum + (it.size || 0), 0);
  const active = state.disk.diskFilter === disk.path;
  const free = disk.connected && disk.free_bytes !== null && disk.free_bytes !== undefined
    ? ` · ${formatSize(disk.free_bytes)} libres` : "";
  const seen = disk.connected ? "connecté" : `vu le ${fmtDate(disk.last_seen)}`;
  const badge = disk.connected
    ? `<span class="chip ok disk-live"><span class="disk-dot" aria-hidden="true"></span>Connecté</span>`
    : `<span class="chip">Hors ligne</span>`;
  const forget = disk.connected
    ? ""
    : `<div class="disk-card-foot"><button type="button" class="btn btn-ghost small" data-disk-forget="${esc(disk.path)}">Retirer de la liste</button></div>`;
  const integrity = diskIntegrityHtml(disk);
  const destinations = diskDestinationsHtml(disk);
  return `
    <article class="disk-card ${disk.connected ? "is-online" : "is-offline"}${active ? " is-active" : ""}">
      <button type="button" class="disk-card-main" data-disk-filter="${esc(disk.path)}" aria-pressed="${active}"
        title="${active ? "Afficher tous les disques" : "Afficher seulement ce disque"}">
        <span class="disk-card-head"><strong class="disk-label">${esc(disk.label)}</strong>${badge}</span>
        <span class="disk-path muted small">${esc(disk.path)}</span>
        <span class="disk-counts">
          <span><b>${NF0.format(count("films"))}</b> films</span>
          <span><b>${NF0.format(count("series"))}</b> séries</span>
          <span><b>${NF0.format(count("ebooks"))}</b> ebooks</span>
        </span>
        <span class="disk-foot muted small">${esc(formatSize(total))}${free} · ${esc(seen)}</span>
      </button>
      ${forget}
      ${destinations}
      ${integrity}
    </article>`;
}

// Empreintes SHA-256 du disque : calcul, vérification, derniers résultats.
function diskIntegrityHtml(disk) {
  const info = state.disk.integrity || {};
  const rec = (info.roots || {})[disk.path];
  const run = info.running && info.running.root === disk.path ? info.running : null;
  const lines = [];
  if (run) {
    const what = run.running === "build" ? "calcul des empreintes" : "vérification";
    lines.push(`<span class="muted small">En cours : ${what} (${NF0.format(run.done)}/${NF0.format(run.total)}).</span>`);
  } else if (info.running) {
    lines.push(`<span class="muted small">Une opération est en cours sur un autre disque.</span>`);
  }
  if (rec) {
    lines.push(`<span class="muted small">Empreintes : ${NF0.format(rec.files)} fichier(s), ${esc(formatSize(rec.size))}, du ${esc(fmtDateTime(rec.built_at))}.</span>`);
    const v = rec.last_verify;
    if (v) {
      const bad = v.corrupt || v.missing;
      lines.push(`<span class="small ${bad ? "danger-text" : "muted"}">Dernière vérification du ${esc(fmtDateTime(v.at))} : ${NF0.format(v.ok)} OK, ${NF0.format(v.corrupt)} corrompu(s), ${NF0.format(v.modified)} modifié(s), ${NF0.format(v.missing)} absent(s), ${NF0.format(v.new)} nouveau(x).</span>`);
    }
  } else {
    lines.push(`<span class="muted small">Pas encore d'empreintes pour ce disque.</span>`);
  }
  const buttons = disk.connected ? `
    <div class="disk-card-foot">
      <button type="button" class="btn btn-ghost small" data-disk-integrity="build" data-disk-path="${esc(disk.path)}" ${run || info.running ? "disabled" : ""}>Empreintes</button>
      <button type="button" class="btn btn-ghost small" data-disk-integrity="verify" data-disk-path="${esc(disk.path)}" ${!rec || run || info.running ? "disabled" : ""}>Vérifier l'intégrité</button>
    </div>` : "";
  return `<div class="disk-integrity">${lines.join("<br>")}${buttons}</div>`;
}

async function startDiskIntegrity(path, mode) {
  try {
    await api("/api/disks/integrity", { method: "POST", body: JSON.stringify({ root: path, mode }) });
    toast(mode === "build" ? "Calcul des empreintes lancé (cela peut durer longtemps)." : "Vérification lancée.", "success");
    loadDiskLibrary({ silent: true });
  } catch (err) {
    toast(err.message, "error");
  }
}

function diskItemSub(r) {
  const m = r.meta || {};
  const kind = state.disk.kind;
  if (kind === "ebooks") {
    return [m.author, m.books ? `${m.books} livre${m.books > 1 ? "s" : ""}` : null, (m.formats || []).join(" / ") || null];
  }
  if (kind === "series") {
    return [m.year, m.seasons ? `${m.seasons} saison${m.seasons > 1 ? "s" : ""}` : null];
  }
  return [m.year, m.runtime ? fmtDuration(m.runtime) : null];
}

function diskItemHtml(r) {
  const m = r.meta || {};
  const title = diskTitle(r);
  const checked = state.disk.selected.has(r.path);
  const hue = hueOf(title);
  const img = m.poster_url ? `<img class="of-img" src="${esc(m.poster_url)}" alt="" loading="lazy" decoding="async">` : "";
  const sub = diskItemSub(r).filter(Boolean).map(esc).join(" · ");
  const genres = (m.genres || []).slice(0, 2).map((g) => `<span class="chip">${esc(g)}</span>`).join("");
  const where = `${esc(r.diskLabel)} · ${esc(r.folder || "")}${r.connected ? "" : " · hors ligne"}`;
  return `
    <label class="of-card disk-item${checked ? " is-selected" : ""}${r.connected ? "" : " is-readonly"}" title="${esc(m.summary || r.path)}">
      <span class="of-poster" style="background: linear-gradient(150deg, hsl(${hue} 40% 30%), hsl(${(hue + 50) % 360} 40% 10%))">
        ${img}
        <span class="of-initial" aria-hidden="true">${esc(title.charAt(0).toUpperCase())}</span>
        <input class="disk-pick" type="checkbox" data-disk-select="${esc(r.path)}"${checked ? " checked" : ""}${r.connected ? "" : " disabled"}
          aria-label="Sélectionner ${esc(title)}">
        ${m.rating ? `<span class="of-rating">${esc(fmtRating(m.rating))}</span>` : ""}
        ${r.on_1fichier ? `<span class="of-badge ok">Sur 1fichier</span>` : ""}
        ${r.adult ? `<span class="of-badge warn">Adulte</span>` : ""}
      </span>
      <span class="of-meta">
        <strong class="of-title">${esc(title)}</strong>
        <span class="muted small">${sub || "&nbsp;"}</span>
        <span class="of-genres">${genres}</span>
        <span class="muted small disk-where">${where} · ${esc(formatSize(r.size))}</span>
        ${canIdentify(r) ? `<button type="button" class="btn btn-ghost small disk-identify" data-disk-identify="${esc(r.path)}"
          title="Choisir le bon titre sur TMDB et renommer l'élément en conséquence">${r.meta ? "Corriger le titre" : "Identifier"}</button>` : ""}
        ${r.connected && state.disk.kind === "ebooks" ? `<button type="button" class="btn btn-ghost small disk-identify" data-disk-cover="${esc(r.path)}"
          title="Importer une image comme jaquette de ce dossier">Changer la jaquette</button>` : ""}
      </span>
    </label>`;
}

function renderDiskCards() {
  const box = $("disk-cards");
  const d = state.disk;
  if (d.error && !d.loaded) {
    box.innerHTML = `<div class="disk-error"><p>${esc(d.error)}</p></div>`;
    return;
  }
  if (!d.loaded) {
    box.innerHTML = `<p class="muted">Lecture des disques…</p>`;
    return;
  }
  box.innerHTML = d.disks.length
    ? d.disks.map(diskCardHtml).join("")
    : `<p class="muted small">Aucun disque connu. Branche un disque externe monté sous /media ou /mnt.</p>`;
}

function renderDiskFilters() {
  const d = state.disk;
  document.querySelectorAll("[data-disk-kind]").forEach((b) => {
    const on = b.dataset.diskKind === (d.adultOnly ? "adultes" : d.kind);
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  const sel = $("disk-disk");
  sel.innerHTML = `<option value="all">Tous les disques</option>` + d.disks.map((x) =>
    `<option value="${esc(x.path)}">${esc(x.label)}${x.connected ? "" : " (hors ligne)"}</option>`).join("");
  sel.value = d.disks.some((x) => x.path === d.diskFilter) ? d.diskFilter : "all";
  d.diskFilter = sel.value;
  $("disk-tmdb").value = d.tmdbFilter;
}

function diskEmptyHtml() {
  const d = state.disk;
  if (d.disks.length === 0) {
    return `<div class="glyph" aria-hidden="true">⊟</div><h3>Aucun disque de sauvegarde</h3>
      <p>Branche un disque externe. Une fois monté, il est inventorié et reste consultable même débranché.</p>`;
  }
  if ($("disk-search").value.trim() || d.tmdbFilter !== "all") {
    return `<div class="glyph" aria-hidden="true">⌕</div><h3>Aucun résultat</h3><p>Modifie le filtre par nom ou par infos TMDB.</p>`;
  }
  const [singular, plural] = DISK_KIND_LABEL[d.kind];
  const disk = d.disks.find((x) => x.path === d.diskFilter);
  const subdirs = disk
    ? (disk.subdirs || {})[d.kind] || []
    : [...new Set(d.disks.flatMap((x) => (x.subdirs || {})[d.kind] || []))];
  const where = disk ? `sur ${disk.label}` : "sur les disques";
  return `<div class="glyph" aria-hidden="true">⊟</div><h3>Aucun ${esc(singular)} ${esc(where)}</h3>
    <p>Dossiers cherchés : ${subdirs.map((x) => `<code>${esc(x)}</code>`).join(" ") || "aucun"}.
    Si tes ${esc(plural)} sont rangés ailleurs, indique le bon sous-dossier dans Réglages.</p>`;
}

function renderDiskList() {
  const d = state.disk;
  const list = $("disk-list");
  const empty = $("disk-empty");
  const banner = $("disk-banner");
  if (!d.loaded) {
    list.innerHTML = "";
    empty.classList.add("hidden");
    banner.classList.add("hidden");
    return;
  }
  const rows = diskRows();
  const anyOffline = rows.some((r) => !r.connected);
  banner.classList.toggle("hidden", !anyOffline);
  banner.textContent = "Disque hors ligne : lecture seule. Rebranche le disque pour copier, déplacer ou supprimer ses éléments.";
  list.innerHTML = rows.map(diskItemHtml).join("");
  if (rows.length === 0 && !(diskQuery() && (d.hits || []).length)) {
    empty.classList.remove("hidden");
    empty.innerHTML = diskEmptyHtml();
  } else {
    empty.classList.add("hidden");
  }
  const connectedVisible = rows.filter((r) => r.connected);
  $("disk-select-all").disabled = connectedVisible.length === 0;
  $("disk-select-all").textContent = connectedVisible.length && connectedVisible.every((r) => d.selected.has(r.path))
    ? "Tout désélectionner" : "Tout sélectionner";
}

function renderDiskActions() {
  const d = state.disk;
  const sel = diskSelectedRows();
  const bar = $("disk-actions");
  bar.classList.toggle("hidden", sel.length === 0);
  $("disk-sel-count").textContent = NF0.format(sel.length);
  $("disk-sel-size").textContent = sel.length ? `· ${formatSize(diskTotalSize(sel))}` : "";

  const dirs = d.serverDirs[d.kind] || [];
  const dirSel = $("disk-server-dir");
  const previous = dirSel.value;
  dirSel.innerHTML = dirs.length
    ? dirs.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("")
    : `<option value="">Aucun dossier configuré</option>`;
  if (dirs.includes(previous)) dirSel.value = previous;

  // Même disque, autre dossier : seuls les dossiers reconnus par la bibliothèque sont proposés.
  const selDisks = [...new Set(sel.map((r) => r.diskPath))];
  const withinDisk = selDisks.length === 1 ? d.disks.find((x) => x.path === selDisks[0]) : null;
  const here = new Set(sel.map((r) => r.folder || ""));
  const withinOptions = withinDisk
    ? ((withinDisk.subdirs || {})[d.kind] || []).filter((o) => !(here.size === 1 && here.has(o)))
    : [];
  const withinSel = $("disk-within-dir");
  const withinPrevious = withinSel.value;
  withinSel.innerHTML = withinOptions.length
    ? withinOptions.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("")
    : `<option value="">Aucun autre dossier reconnu</option>`;
  if (withinOptions.includes(withinPrevious)) withinSel.value = withinPrevious;
  $("disk-within").disabled = !sel.length || selDisks.length !== 1 || !withinOptions.length;
  $("disk-within").title = selDisks.length > 1 ? "Sélectionne des éléments d'un seul disque." : "";

  const any = sel.length > 0;
  const serverTitle = dirs.length ? "" : "Configure le dossier source de ce type dans Réglages.";
  $("disk-copy").disabled = !any || !dirs.length;
  $("disk-move").disabled = !any || !dirs.length;
  $("disk-copy").title = serverTitle;
  $("disk-move").title = serverTitle;
  $("disk-1f").disabled = !any || !d.onefichierConfigured;
  $("disk-1f").title = d.onefichierConfigured ? "" : "Clé API 1fichier non renseignée (Réglages › 1fichier).";
  $("disk-delete").disabled = !any;
  const cat = $("disk-category");
  const allAdult = any && sel.every((r) => r.adult);
  cat.classList.toggle("hidden", !(adult.unlocked && d.kind === "films" && any && sel.every((r) => r.connected)));
  cat.textContent = allAdult ? "Sortir de la catégorie Adultes" : "Déplacer vers Adultes";
  cat.dataset.adult = allAdult ? "0" : "1";

  const isEbook = d.kind === "ebooks";
  $("disk-1f-remove-label").classList.toggle("hidden", isEbook);
  if (isEbook) $("disk-1f-remove").checked = false;
}

function renderDiskView() {
  const d = state.disk;
  const connected = d.disks.filter((x) => x.connected).length;
  // Une erreur s'affiche toujours, y compris au premier chargement : sinon « Lecture… » restait affiché
  // pour toujours quand la lecture échouait ou dépassait le délai (version 43).
  $("disk-sub").textContent = d.error
    ? d.error
    : d.loaded
      ? `${NF0.format(connected)} disque(s) connecté(s) · ${NF0.format(d.disks.length)} connu(s)`
      : "Lecture des disques…";
  renderDiskCards();
  renderDiskFilters();
  renderDiskSearch();
  renderDiskList();
  renderDiskActions();
}

async function loadDiskLibrary({ force = false, silent = false } = {}) {
  if (state.surface !== "disk") return;
  const d = state.disk;
  if (d.loading) {
    // Une lecture est déjà en cours : la demande est gardée et relancée à sa fin (une demande forcée ne se perd pas).
    const queued = d.queued || { force: false, silent: true };
    d.queued = { force: queued.force || force, silent: queued.silent && silent };
    return;
  }
  d.loading = true;
  const gen = adultGen;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), force ? 120000 : 30000);
  try {
    const data = await api(`/api/disk-library${force ? "?force=true" : ""}`, { signal: ctrl.signal });
    const integrity = await api("/api/disks/integrity").catch(() => d.integrity || null);
    if (gen !== adultGen) return;  // d'avant un changement d'accès adulte : jetée (la relecture est en file)
    d.disks = data.disks || [];
    d.integrity = integrity;
    d.serverDirs = data.server_dirs || {};
    d.deleteConfirmText = data.delete_confirm_text || d.deleteConfirmText;
    d.onefichierConfigured = !!data.onefichier_configured;
    d.loaded = true;
    d.error = null;
    loadDiskMoves();
    // Fiches TMDB et jaquettes : on relit la liste tant que le calcul tourne.
    clearTimeout(d.metaTimer);
    if (data.meta_running) d.metaTimer = setTimeout(() => loadDiskLibrary({ silent: true }), 3000);
    // Empreintes en cours : on suit l'avancement.
    if (d.integrity && d.integrity.running) d.metaTimer = setTimeout(() => loadDiskLibrary({ silent: true }), 5000);
  } catch (err) {
    if (gen !== adultGen) return;
    // Une erreur de relecture est toujours affichée, même si la liste est déjà là.
    d.error = err.name === "AbortError" ? "Les disques ne répondent pas (délai dépassé)." : err.message;
  } finally {
    clearTimeout(timer);
    d.loading = false;
    const next = d.queued;
    d.queued = null;
    if (next) {
      loadDiskLibrary(next);
      return;
    }
  }
  if (state.surface !== "disk") return;
  diskPruneSelection();
  renderDiskView();
}

function setDiskKind(kind) {
  state.disk.adultOnly = kind === "adultes";
  state.disk.kind = kind === "adultes" ? "films" : kind;
  state.disk.selected.clear();
  renderDiskView();
}

function toggleDiskSelectAll() {
  const visible = diskRows().filter((r) => r.connected);
  const allOn = visible.length && visible.every((r) => state.disk.selected.has(r.path));
  for (const r of visible) {
    if (allOn) state.disk.selected.delete(r.path);
    else state.disk.selected.add(r.path);
  }
  renderDiskList();
  renderDiskActions();
}

// Confirmation simple (la fenêtre partagée #confirm-modal, sans choix « après sauvegarde »).
function askConfirm({ title, body, typed = null, names = [] }) {
  return new Promise((resolve) => {
    if (confirmOpen) { resolve(false); return; }
    confirmOpen = true;
    const overlay = $("confirm-modal");
    const okBtn = $("confirm-ok");
    const cancelBtn = $("confirm-cancel");
    const input = $("confirm-delete-input");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body + (typed ? ` Tape ${typed} pour confirmer.` : "");
    $("confirm-after").classList.add("hidden");
    input.value = "";
    input.classList.toggle("hidden", !typed);
    fillConfirmNames(names);

    const check = () => { okBtn.disabled = !!typed && input.value.trim() !== typed; };
    const onOk = () => { if (!okBtn.disabled) finish(true); };
    const onCancel = () => finish(false);
    const onKey = (e) => {
      if (e.key === "Enter" && !okBtn.disabled) onOk();
    };
    // Échap ferme la fenêtre où que se trouve le focus (pas seulement dans le champ de saisie).
    const onEsc = (e) => {
      if (e.key === "Escape") onCancel();
    };
    function finish(value) {
      overlay.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      input.removeEventListener("input", check);
      input.removeEventListener("keydown", onKey);
      document.removeEventListener("keydown", onEsc);
      okBtn.disabled = false;
      input.classList.add("hidden");
      clearConfirmNames();
      confirmOpen = false;
      resolve(value);
    }

    check();
    overlay.classList.remove("hidden");
    input.addEventListener("input", check);
    input.addEventListener("keydown", onKey);
    document.addEventListener("keydown", onEsc);
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    setTimeout(() => (typed ? input.focus() : okBtn.focus()), 0);
  });
}

function afterDiskJob(jobId, operation) {
  trackJob(jobId, operation, { live: true, container: $("disk-transfers-list") });
  state.disk.selected.clear();
  renderDiskList();
  renderDiskActions();
  toast("Opération démarrée.", "success");
}

async function diskTransfer(mode) {
  const rows = diskSelectedRows();
  if (!rows.length) return;
  const dir = $("disk-server-dir").value;
  if (!dir) { toast("Aucun dossier serveur configuré pour ce type.", "error"); return; }
  const size = formatSize(diskTotalSize(rows));
  const body = `${NF0.format(rows.length)} élément(s), ${size}. Destination : ${dir}. ` + (mode === "move"
    ? "Chaque élément est retiré du disque après une copie vérifiée."
    : "Le disque conserve sa copie.");
  const ok = await askConfirm({
    title: mode === "move" ? "Déplacer vers le serveur" : "Copier vers le serveur", body, names: rows.map(diskTitle),
  });
  if (!ok) return;
  try {
    const data = await api("/api/disk-library/transfer", {
      method: "POST",
      body: JSON.stringify({ items: rows.map((r) => r.path), mode, server_dir: dir }),
    });
    afterDiskJob(data.job_id, mode);
  } catch (err) {
    toast(err.message, "error");
  }
}

async function diskToOneFichier() {
  const rows = diskSelectedRows();
  if (!rows.length) return;
  const remove = $("disk-1f-remove").checked && state.disk.kind !== "ebooks";
  const body = `${NF0.format(rows.length)} élément(s), ${formatSize(diskTotalSize(rows))}. `
    + "Une archive 7z chiffrée par élément (film → tmdb-<id>, série → tmdb-tv-<id>, ebook → ebook-<empreinte>). "
    + (remove ? "La copie du disque sera retirée après la sauvegarde vérifiée." : "Le disque conserve sa copie.");
  const ok = await askConfirm({
    title: "Envoyer sur 1fichier",
    body,
    typed: remove ? state.disk.deleteConfirmText : null,
    names: rows.map(diskTitle),
  });
  if (!ok) return;
  const payload = { paths: rows.map((r) => r.path) };
  if (remove) {
    payload.after = "delete";
    payload.confirm_delete_text = state.disk.deleteConfirmText;
  }
  try {
    const data = await api("/api/1fichier/archive", { method: "POST", body: JSON.stringify(payload) });
    afterDiskJob(data.job_id, "archive_1fichier");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function diskDelete() {
  const rows = diskSelectedRows();
  if (!rows.length) return;
  const ok = await askConfirm({
    title: "Supprimer du disque",
    body: `${NF0.format(rows.length)} élément(s), ${formatSize(diskTotalSize(rows))}, seront supprimés définitivement du disque. Cette action est irréversible.`,
    typed: state.disk.deleteConfirmText,
    names: rows.map(diskTitle),
  });
  if (!ok) return;
  try {
    const data = await api("/api/disk-library/delete", {
      method: "POST",
      body: JSON.stringify({ items: rows.map((r) => r.path), confirm_delete_text: state.disk.deleteConfirmText }),
    });
    afterDiskJob(data.job_id, "delete");
  } catch (err) {
    toast(err.message, "error");
  }
}

async function forgetDisk(path) {
  const ok = await askConfirm({
    title: "Retirer de la liste",
    body: `Le disque ${path} ne figurera plus dans la liste. Ses fichiers ne sont pas touchés ; il réapparaîtra s'il est rebranché.`,
  });
  if (!ok) return;
  try {
    await api("/api/disk-library/forget", { method: "POST", body: JSON.stringify({ path }) });
    toast("Disque retiré de la liste.", "success");
    loadDiskLibrary({ silent: true });
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------------------------------------------------------------------
// Version 48 : dossier de destination par disque, déplacement dans le même disque, suivi
// ---------------------------------------------------------------------

const DISK_KIND_NAME = { films: "Films", series: "Séries", ebooks: "Ebooks" };

// Dossiers de destination effectifs d'un disque connecté (le dossier propre au disque est signalé).
function diskDestinationsHtml(disk) {
  if (!disk.connected) return "";
  const dest = disk.destinations || {};
  const lines = Object.keys(DISK_KIND_NAME).map((kind) => {
    const x = dest[kind] || {};
    const custom = x.custom ? ` <span class="chip accent">propre à ce disque</span>` : "";
    return `<li><span class="muted small">${esc(DISK_KIND_NAME[kind])}</span> <code>${esc(x.folder || "—")}</code>${custom}</li>`;
  }).join("");
  return `
    <div class="disk-destinations">
      <span class="muted small">Dossiers de destination</span>
      <ul class="disk-dest-list">${lines}</ul>
      <div class="disk-card-foot">
        <button type="button" class="btn btn-ghost small" data-disk-folders="${esc(disk.path)}">Choisir les dossiers</button>
      </div>
    </div>`;
}

// --- Fenêtre de choix du dossier de destination ---------------------------------

async function openDiskFolders(path) {
  const disk = state.disk.disks.find((x) => x.path === path);
  if (!disk || !disk.connected) { toast("Ce disque n'est pas connecté.", "error"); return; }
  state.disk.picker = { path, label: disk.label, kind: state.disk.kind, folder: "", data: null };
  $("disk-folder-kind").value = state.disk.kind;
  $("disk-folder-new").value = "";
  $("disk-folder-modal").classList.remove("hidden");
  await loadDiskPicker("");
}

function closeDiskFolders() {
  state.disk.picker = null;
  $("disk-folder-modal").classList.add("hidden");
}

async function loadDiskPicker(folder) {
  const pk = state.disk.picker;
  if (!pk) return;
  try {
    const q = `path=${encodeURIComponent(pk.path)}&folder=${encodeURIComponent(folder || "")}`;
    pk.data = await api(`/api/disks/folders?${q}`);
    pk.folder = pk.data.folder;
    renderDiskPicker();
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderDiskPicker() {
  const pk = state.disk.picker;
  if (!pk || !pk.data) return;
  const disk = state.disk.disks.find((x) => x.path === pk.path);
  const kindName = DISK_KIND_NAME[pk.kind] || pk.kind;
  $("disk-folder-title").textContent = `Dossier des ${kindName.toLowerCase()}`;
  $("disk-folder-disk").textContent = `Disque : ${pk.label}`;
  const current = disk && disk.destinations ? disk.destinations[pk.kind] : null;
  $("disk-folder-current").innerHTML = current
    ? `Actuellement : <code>${esc(current.folder)}</code> ${current.custom ? "(propre à ce disque)" : "(dossier global de Réglages)"}`
    : "";
  // Fil d'Ariane : racine du disque, puis chaque dossier traversé (cliquable).
  const parts = pk.folder ? pk.folder.split("/") : [];
  const crumbs = [`<button type="button" class="btn btn-ghost small" data-picker-go="">${esc(pk.data.label)}</button>`];
  parts.forEach((name, i) => {
    crumbs.push(`<span class="muted">/</span><button type="button" class="btn btn-ghost small" data-picker-go="${esc(parts.slice(0, i + 1).join("/"))}">${esc(name)}</button>`);
  });
  $("disk-folder-crumbs").innerHTML = crumbs.join("");
  const folders = pk.data.folders || [];
  $("disk-folder-list").innerHTML = folders.length
    ? folders.map((f) => `<li><button type="button" class="folder-item" data-picker-go="${esc(f.folder)}">${esc(f.name)}</button></li>`).join("")
    : `<li class="muted small">Aucun sous-dossier ici.</li>`;
  const recognized = !!disk && !!pk.folder && ((disk.subdirs || {})[pk.kind] || []).includes(pk.folder);
  let note;
  if (!pk.folder) note = "Entre dans un dossier du disque pour le choisir. La racine du disque n'est pas une destination.";
  else if (recognized) note = "Ce dossier est reconnu par la bibliothèque : les éléments qui y sont copiés y apparaissent.";
  else note = "Ce dossier n'est pas reconnu par la bibliothèque : les éléments copiés ici n'y seront pas listés.";
  if (pk.data.truncated) note += " Liste tronquée : affiche moins de dossiers.";
  $("disk-folder-note").textContent = note;
  $("disk-folder-choose").disabled = !pk.folder;
  $("disk-folder-create").disabled = false;
}

// Enregistre le dossier du type choisi sur ce disque (vide = retour au dossier global).
async function saveDiskFolder(folder, create) {
  const pk = state.disk.picker;
  if (!pk) return null;
  try {
    const data = await api("/api/disks/folder", {
      method: "PUT",
      body: JSON.stringify({ path: pk.path, kind: pk.kind, folder, create }),
    });
    toast(`${DISK_KIND_NAME[pk.kind]} sur ${pk.label} : ${data.effective}`, "success");
    await loadDiskLibrary({ silent: true });
    return data;
  } catch (err) {
    toast(err.message, "error");
    return null;
  }
}

async function chooseDiskFolder() {
  const pk = state.disk.picker;
  if (!pk || !pk.folder) return;
  if (await saveDiskFolder(pk.folder, false)) await loadDiskPicker(pk.folder);
}

async function createDiskFolder() {
  const pk = state.disk.picker;
  if (!pk) return;
  const name = $("disk-folder-new").value.trim();
  if (!name || name.includes("/") || name === "." || name === "..") {
    toast("Nom de dossier invalide (sans « / », ni « . » ou « .. »).", "error");
    return;
  }
  const folder = pk.folder ? `${pk.folder}/${name}` : name;
  if (await saveDiskFolder(folder, true)) {
    $("disk-folder-new").value = "";
    await loadDiskPicker(folder);
  }
}

async function resetDiskFolder() {
  const pk = state.disk.picker;
  if (!pk) return;
  if (await saveDiskFolder("", false)) await loadDiskPicker(pk.folder);
}

// --- Déplacement dans le même disque ---------------------------------------------

async function diskWithinMove() {
  const rows = diskSelectedRows();
  const folder = $("disk-within-dir").value;
  if (!rows.length || !folder) return;
  const disk = state.disk.disks.find((x) => x.path === rows[0].diskPath);
  const label = disk ? disk.label : "ce disque";
  const ok = await askConfirm({
    title: "Déplacer dans le disque",
    body: `${NF0.format(rows.length)} élément(s) vers ${folder} sur ${label}. Rien n'est copié : chaque élément change de dossier, sans relecture. Si un élément porte déjà ce nom là-bas, rien n'est déplacé.`,
    names: rows.map(diskTitle),
  });
  if (!ok) return;
  try {
    const data = await api("/api/disk-library/move", {
      method: "POST",
      body: JSON.stringify({ paths: rows.map((r) => r.path), folder }),
    });
    state.disk.selected.clear();
    const moved = (data.moved || []).length;
    const errors = (data.errors || []).length;
    if (data.dry_run) toast(`Simulation : ${moved} déplacement(s) prévu(s). Rien n'a été déplacé.`, "success");
    else if (errors) toast(`${moved} déplacé(s), ${errors} en erreur : ${data.errors[0].error}`, "error");
    else toast(`${moved} élément(s) déplacé(s) vers ${folder}.`, "success");
    await loadDiskLibrary({ force: true, silent: true });
    loadDiskMoves();
  } catch (err) {
    toast(err.message, "error");
  }
}

// --- Suivi des copies et déplacements --------------------------------------------

async function loadDiskMoves() {
  if (state.surface !== "disk") return;
  const filter = $("disk-moves-filter").value;
  const seq = (state.disk.movesSeq || 0) + 1;
  state.disk.movesSeq = seq;
  try {
    const data = await api(`/api/disk-moves?status=${encodeURIComponent(filter)}&limit=200`);
    if (seq !== state.disk.movesSeq) return;  // une relecture plus récente a déjà répondu
    state.disk.moves = data;
    renderDiskMoves();
  } catch (err) {
    $("disk-moves-count").textContent = `Suivi indisponible : ${err.message}`;
  }
}

function renderDiskMoves() {
  const data = state.disk.moves;
  if (!data) return;
  const c = data.counts || {};
  const problems = (c.erreur || 0) + (c.partiel || 0) + (c.interrompu || 0);
  $("disk-moves-count").textContent = `${NF0.format(c.en_cours || 0)} en cours · ${NF0.format(c.ok || 0)} terminé(s) · `
    + `${NF0.format(problems)} problème(s) · ${NF0.format(data.total || 0)} au total`;
  $("disk-moves-clear").disabled = !(c.ok || c.erreur || c.partiel || c.ignore || c.interrompu);
  $("disk-moves-list").innerHTML = data.items.length
    ? data.items.map(diskMoveHtml).join("")
    : `<p class="muted small empty-msg">Aucun transfert à afficher. Les copies et déplacements vers ou depuis un disque, et les déplacements dans un disque, sont suivis ici.</p>`;
}

function diskMoveHtml(e) {
  const trail = e.destination ? `<span class="disk-move-arrow" aria-hidden="true">→</span> ${esc(e.destination)}` : "";
  return `
    <article class="disk-move status-${esc(e.status)}">
      <div class="disk-move-head">
        <span class="move-status s-${esc(e.status)}">${esc(e.status_label)}</span>
        <strong class="disk-move-name">${esc(e.name)}</strong>
        <span class="muted small">${esc(e.operation)} · ${esc(fmtDateTime(e.updated || e.at))}</span>
      </div>
      <div class="muted small disk-move-path">${esc(e.source || "")} ${trail}</div>
      ${e.detail ? `<div class="small disk-move-detail">${esc(e.detail)}</div>` : ""}
    </article>`;
}

async function clearDiskMoves() {
  const ok = await askConfirm({
    title: "Effacer l'historique",
    body: "Les transferts terminés ou en erreur sont retirés du suivi. Les transferts en cours ne sont pas touchés.",
  });
  if (!ok) return;
  try {
    const data = await api("/api/disk-moves/clear", { method: "POST" });
    toast(`${NF0.format(data.removed || 0)} entrée(s) effacée(s).`, "success");
    loadDiskMoves();
  } catch (err) {
    toast(err.message, "error");
  }
}

// Branchements : fenêtre des dossiers, déplacement dans le disque, suivi.
function bindDiskExtras() {
  $("disk-folder-kind").addEventListener("change", (e) => {
    if (!state.disk.picker) return;
    state.disk.picker.kind = e.target.value;
    renderDiskPicker();
  });
  const go = (e) => {
    const target = e.target.closest("[data-picker-go]");
    if (target) loadDiskPicker(target.dataset.pickerGo);
  };
  $("disk-folder-crumbs").addEventListener("click", go);
  $("disk-folder-list").addEventListener("click", go);
  $("disk-folder-choose").addEventListener("click", chooseDiskFolder);
  $("disk-folder-create").addEventListener("click", createDiskFolder);
  $("disk-folder-reset").addEventListener("click", resetDiskFolder);
  $("disk-folder-close").addEventListener("click", closeDiskFolders);
  $("disk-folder-modal").addEventListener("click", (e) => {
    if (e.target.id === "disk-folder-modal") closeDiskFolders();
  });
  $("disk-folder-new").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); createDiskFolder(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.disk.picker) closeDiskFolders();
  });
  $("disk-within").addEventListener("click", diskWithinMove);
  $("disk-moves-filter").addEventListener("change", loadDiskMoves);
  $("disk-moves-clear").addEventListener("click", clearDiskMoves);
}
