"use strict";
// list.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Rendu : visibilité par type, statistiques, contrôles
// ---------------------------------------------------------------------

function applyMediaVisibility() {
  document.querySelectorAll("[data-mt]").forEach((node) => {
    const types = node.dataset.mt.split(/\s+/);
    node.classList.toggle("hidden", !types.includes(state.mediaType));
  });
  $("plex-seg").classList.toggle("hidden", state.mediaType === "ebooks" || !state.plexEnabled);
  $("filter-quality").closest(".select-wrap").classList.toggle("hidden", state.mediaType !== "films");
}

function rebuildControls() {
  // Appelé uniquement quand le type ou les résultats changent (évite de
  // perdre le focus d'un contrôle que l'utilisateur est en train d'utiliser).
  const r = state.results;

  const genres = [...new Set(r.flatMap((it) => it.genres || []))].sort((a, b) => a.localeCompare(b, "fr"));
  const gsel = $("filter-genre");
  gsel.innerHTML = `<option value="">Tous les genres</option>` + genres.map((g) => `<option value="${esc(g)}">${esc(g)}</option>`).join("");
  if (!genres.includes(state.filters.genre)) state.filters.genre = "";
  gsel.value = state.filters.genre;

  const present = new Set(r.map((it) => it.quality || "?"));
  const qsel = $("filter-quality");
  qsel.innerHTML = `<option value="">Toutes</option>` + QUALITY_ORDER.filter((q) => present.has(q))
    .map((q) => `<option value="${q}">${QUALITY_LABEL[q]}</option>`).join("");
  if (!present.has(state.filters.quality)) state.filters.quality = "";
  qsel.value = state.filters.quality;

  const opts = SORTS[state.mediaType];
  if (!opts.some((o) => o.key === state.sortKey)) { state.sortKey = "name"; state.sortDir = "asc"; }
  $("sort-select").innerHTML = opts.map((o) => `<option value="${o.key}">${o.label}</option>`).join("");
}

function renderSortControls() {
  $("sort-select").value = state.sortKey;
  const arrow = $("sort-dir").querySelector("svg");
  arrow.style.transform = state.sortDir === "desc" ? "scaleY(-1)" : "none";
  $("sort-dir").title = state.sortDir === "desc" ? "Ordre décroissant — inverser" : "Ordre croissant — inverser";
}

function stat(label, value, sub, pct) {
  const bar = pct === null || pct === undefined ? "" : `<div class="bar"><span style="width:${Math.max(0, Math.min(100, pct))}%"></span></div>`;
  return `<div class="stat"><span class="label">${esc(label)}</span><span class="value">${esc(value)}</span>` +
    `<span class="sub">${esc(sub || "\u00a0")}</span>${bar}</div>`;
}

function renderStats() {
  const box = $("stats");
  const r = state.results;
  if (!state.searched || state.loading || r.length === 0) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const now = Date.now() / 1000;
  const recent = r.filter((it) => it.added_at && now - it.added_at < 30 * 86400).length;
  const total = r.reduce((s, it) => s + (it.size || 0), 0);
  const cards = [];

  if (state.mediaType === "films") {
    const known = r.filter((it) => it.watched !== null && it.watched !== undefined).length;
    const watched = r.filter((it) => it.watched === true).length;
    const pct = known ? Math.round((watched / known) * 100) : null;
    const durMin = r.reduce((s, it) => s + (it.duration_min || 0), 0);
    const durKnown = r.filter((it) => it.duration_min).length;
    const uhd = r.filter((it) => it.quality === "4K").length;
    cards.push(stat("Films", NF0.format(r.length), `${NF0.format(recent)} ajouté(s) il y a 30 j`));
    cards.push(stat("Volume", formatSize(total), `${formatSize(total / r.length)} en moyenne`));
    cards.push(stat("Vus (Plex)", known ? `${NF0.format(watched)} / ${NF0.format(known)}` : "—",
      known ? `${pct} % des titres connus` : "Plex non disponible", pct));
    cards.push(stat("Durée cumulée", durKnown ? fmtDuration(durMin) : "—",
      durKnown ? `sur ${durKnown} film(s) connu(s)` : "Durée fournie par Plex"));
    cards.push(stat("En 4K", NF0.format(uhd), "films en 2160p"));
  } else if (state.mediaType === "series") {
    const episodes = r.reduce((s, it) => s + (it.episodes || 0), 0);
    const seasons = r.reduce((s, it) => s + (it.seasons || 0), 0);
    const known = r.filter((it) => it.watched !== null && it.watched !== undefined).length;
    const done = r.filter((it) => it.watched === true).length;
    const partial = r.filter((it) => it.watched === false && (it.viewed_episodes || 0) > 0).length;
    cards.push(stat("Séries", NF0.format(r.length), `${NF0.format(recent)} ajoutée(s) il y a 30 j`));
    cards.push(stat("Épisodes", NF0.format(episodes), "sur le disque"));
    cards.push(stat("Saisons", NF0.format(seasons), "détectées dans les noms"));
    cards.push(stat("Terminées", known ? `${NF0.format(done)} / ${NF0.format(known)}` : "—",
      known ? "séries connues de Plex" : "Plex non disponible", known ? Math.round((done / known) * 100) : null));
    cards.push(stat("En cours", NF0.format(partial), "au moins un épisode vu"));
  } else {
    const authors = new Set(r.map((it) => it.author).filter(Boolean));
    const formats = [...new Set(r.map((it) => it.format))];
    cards.push(stat("Ebooks", NF0.format(r.length), `${NF0.format(recent)} ajouté(s) il y a 30 j`));
    cards.push(stat("Volume", formatSize(total), `${formatSize(total / r.length)} en moyenne`));
    cards.push(stat("Auteurs", NF0.format(authors.size), "déduits du nom de fichier"));
    cards.push(stat("Formats", NF0.format(formats.length), formats.slice(0, 4).join(" · ") || "—"));
    cards.push(stat("Plus récent", fmtAdded(Math.max(...r.map((it) => it.added_at || 0)) || null), ""));
  }

  box.innerHTML = cards.join("");
  box.hidden = false;
}

function countText() {
  if (state.loading) return "Recherche en cours…";
  if (!state.searched) return "Aucune recherche";
  const n = state.visible.length;
  const total = state.results.length;
  const base = filtersActive() && n !== total
    ? `${NF0.format(n)} sur ${NF0.format(total)}`
    : `${NF0.format(n)} résultat${n > 1 ? "s" : ""}`;
  const construite = state.searchBuiltAt ? ` · liste construite le ${fmtDateTime(state.searchBuiltAt)}` : "";
  return `${base}${construite}${state.searchRefreshing ? " · en cours d'actualisation…" : ""}`;
}

function renderToolbar() {
  document.querySelectorAll("#plex-seg .seg-btn").forEach((b) => {
    b.classList.toggle("is-active", b.dataset.plex === state.filters.plex);
  });
  document.querySelectorAll("#view-seg .seg-btn").forEach((b) => {
    const on = b.dataset.view === state.view;
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-pressed", String(on));
  });
  $("filter-genre").value = state.filters.genre;
  $("filter-quality").value = state.filters.quality;
  $("filter-added").value = state.filters.added;
  $("filter-tmdb").value = state.filters.tmdb;
  $("reset-filters").classList.toggle("hidden", !filtersActive());
  $("results-count").textContent = countText();
  $("plex-refresh-btn").classList.toggle("hidden", !state.plexEnabled);
}

// ---------------------------------------------------------------------
// Rendu : cartes, liste, états vides
// ---------------------------------------------------------------------

function coverHtml(it) {
  const title = itemTitle(it);
  const h = hueOf(title);
  const style = `--cover-bg: linear-gradient(150deg, hsl(${h} 42% 36%), hsl(${(h + 52) % 360} 46% 13%))`;
  const isBook = state.mediaType === "ebooks";
  const sub = isBook ? it.author || "" : it.year ? String(it.year) : "";
  return `<span class="cover${isBook ? " book" : ""}" style="${style}" aria-hidden="true">` +
    `<span class="cover-mark">${esc(title.charAt(0).toUpperCase())}</span>` +
    `<span class="cover-title">${esc(title)}${sub ? `<small>${esc(sub)}</small>` : ""}</span></span>`;
}

// Affiche (ou couverture générée). tag = "button" pour ouvrir la fiche, "span" sinon.
function mediaHtml(it, idx, overlay = "", { tag = "button", thumb = false } = {}) {
  const hasImg = !!it.poster_url;
  const img = hasImg ? `<img class="poster-img" src="${esc(it.poster_url)}" alt="" loading="lazy" decoding="async">` : "";
  const cls = `media${hasImg ? "" : " no-img"}`;
  const attrs = tag === "button"
    ? ` type="button" data-open="${idx}" aria-label="Ouvrir la fiche de ${esc(itemTitle(it))}"${thumb ? ' tabindex="-1"' : ""}`
    : "";
  return `<${tag} class="${cls}"${attrs}>${img}${coverHtml(it)}${overlay}</${tag}>`;
}

function cardHtml(it, idx) {
  const key = itemKey(it);
  const sel = state.selected.has(key);
  const title = itemTitle(it);
  const st = statusOf(it);
  const overlay = [
    topChip(it) ? `<span class="chip-top">${topChip(it)}</span>` : "",
    st ? `<div class="status-overlay"><span class="chip ${st.cls}">${esc(st.label)}</span></div>` : "",
    st && st.pct !== undefined && st.pct !== null ? `<div class="progress"><span style="width:${st.pct}%"></span></div>` : "",
  ].join("");
  const rating = it.rating !== null && it.rating !== undefined
    ? `<span class="rating">${fmtRating(it.rating)}</span>`
    : `<span class="rating none">Note —</span>`;
  return `<article class="card${sel ? " is-selected" : ""}">
      ${mediaHtml(it, idx, overlay)}
      <label class="card-check"><input type="checkbox" data-select="${idx}" ${sel ? "checked" : ""} aria-label="Sélectionner ${esc(title)}"></label>
      <div class="card-body">
        <button type="button" class="card-title-btn" data-open="${idx}">${esc(title)}</button>
        <span class="card-meta">${it.adult ? `<span class="adult-tag">Adulte</span> ` : ""}${esc(metaLine(it))}</span>
        <div class="card-foot">${rating}<span title="${esc(addedTitle(it))}">${esc(fmtAdded(it.added_at))}</span></div>
      </div>
    </article>`;
}

function cellHtml(col, it, idx) {
  switch (col.id) {
    case "title":
      return `<div class="row-title"><span class="thumb">${mediaHtml(it, idx, "", { tag: "button", thumb: true })}</span>` +
        `<div class="txt"><button type="button" class="link-btn" data-open="${idx}">${esc(itemTitle(it))}</button>` +
        `<div class="row-sub">${esc(metaLine(it))}</div></div></div>`;
    case "year": return esc(it.year ?? "—");
    case "added": return `<span title="${esc(addedTitle(it))}">${esc(fmtAdded(it.added_at))}</span>`;
    case "size": return formatSize(it.size);
    case "quality": return it.quality && it.quality !== "?" ? esc(it.quality) : "—";
    case "audio": return it.audio && it.audio !== "?" ? esc(it.audio) : "—";
    case "rating": return it.rating !== null && it.rating !== undefined ? esc(fmtRating(it.rating)) : "—";
    case "status": {
      const st = statusOf(it);
      return st ? `<span class="chip ${st.cls}">${esc(st.label)}</span>` : "—";
    }
    case "episodes": return esc(it.episodes ?? "—");
    case "seasons": return esc(it.seasons ?? "—");
    case "author": return esc(it.author || "—");
    case "format": return `<span class="chip info">${esc(it.format)}</span>`;
    default: return "";
  }
}

function headerHtml() {
  const cols = COLS[state.mediaType];
  const head = cols.map((c) => {
    const cls = c.cls ? ` class="${c.cls}"` : "";
    if (!c.sort) return `<th${cls}>${esc(c.label)}</th>`;
    const active = state.sortKey === c.sort;
    const arrow = active ? (state.sortDir === "asc" ? "▲" : "▼") : "";
    return `<th${cls} class="${c.cls || ""}${active ? " is-sorted" : ""}" aria-sort="${active ? (state.sortDir === "asc" ? "ascending" : "descending") : "none"}">` +
      `<button type="button" class="th-btn" data-sort="${c.sort}">${esc(c.label)}<span class="arrow">${arrow}</span></button></th>`;
  }).join("");
  return `<tr><th class="col-check"><span class="sr-only">Sélection</span></th>${head}</tr>`;
}

function rowHtml(it, idx) {
  const key = itemKey(it);
  const sel = state.selected.has(key);
  const cells = COLS[state.mediaType].map((c) => {
    const cls = c.cls ? ` class="${c.cls}"` : "";
    return `<td${cls}>${cellHtml(c, it, idx)}</td>`;
  }).join("");
  return `<tr data-row="${idx}" class="${sel ? "is-selected" : ""}">
      <td class="col-check"><input type="checkbox" data-select="${idx}" ${sel ? "checked" : ""} aria-label="Sélectionner ${esc(itemTitle(it))}"></td>
      ${cells}</tr>`;
}

function skeletonHtml() {
  return Array.from({ length: 10 }, () => `<div class="skeleton" aria-hidden="true"></div>`).join("");
}

function setEmpty(html) {
  const box = $("empty-state");
  box.innerHTML = html;
  box.classList.remove("hidden");
}

// ---------------------------------------------------------------------
// Sagas de films : une jaquette par saga, films possédés et manquants
// ---------------------------------------------------------------------

const SAGAS = { data: null, loaded: false, loading: false, error: null, timer: null, token: 0 };

async function loadSagas({ silent = false } = {}) {
  clearTimeout(SAGAS.timer);
  const token = ++SAGAS.token;
  SAGAS.loading = true;
  if (!silent) paintSagas();
  let data = null;
  let failed = false;
  let message = null;
  try {
    data = await api("/api/sagas");
  } catch (err) {
    failed = true;
    message = err.message;
  }
  // Lecture remplacée, ou accès adulte changé entre-temps : la réponse est ignorée.
  if (token !== SAGAS.token) return;
  if (failed) {
    SAGAS.error = message;
  } else {
    SAGAS.data = data;
    SAGAS.error = null;
  }
  SAGAS.loading = false;
  SAGAS.loaded = true;
  paintSagas();
  // Les fiches TMDB qui manquent se récupèrent en arrière-plan : on relit tant que ça tourne.
  if (SAGAS.data && SAGAS.data.running && sagasVisible()) {
    SAGAS.timer = setTimeout(() => loadSagas({ silent: true }), 3000);
  }
}

function sagaPartHtml(p) {
  const owned = p.owned || [];
  const status = owned.length
    ? owned.map((o) => `<span class="chip ok">${esc(o.label)}</span>`).join(" ")
    : `<span class="chip warn">Manquant</span>`;
  const thumb = p.poster_url
    ? `<img class="saga-thumb" src="${esc(p.poster_url)}" alt="" loading="lazy" decoding="async">`
    : `<span class="saga-thumb saga-thumb-empty" aria-hidden="true"></span>`;
  return `<li class="saga-part${owned.length ? "" : " is-missing"}">${thumb}
      <span class="saga-part-title"><strong>${esc(p.title || "—")}</strong>${p.year ? ` <span class="muted small">(${esc(p.year)})</span>` : ""}</span>
      <span class="saga-part-status">${status}</span></li>`;
}

function sagaCardHtml(g) {
  const hue = hueOf(g.name || "saga");
  const poster = g.poster_url
    ? `<img class="saga-poster-img" src="${esc(g.poster_url)}" alt="" loading="lazy" decoding="async">`
    : `<span class="saga-poster-fallback" style="background: linear-gradient(150deg, hsl(${hue} 40% 30%), hsl(${(hue + 50) % 360} 40% 10%))">${esc((g.name || "?").charAt(0).toUpperCase())}</span>`;
  const state_ = g.missing_count ? `${g.missing_count} manquant${g.missing_count > 1 ? "s" : ""}` : "saga complète";
  return `<details class="saga-card">
    <summary>
      <span class="saga-poster">${poster}</span>
      <span class="saga-meta">
        <strong class="saga-name">${esc(g.name || "Saga")}</strong>
        <span class="muted small">${g.owned_count} / ${g.total} films possédés · ${esc(state_)}</span>
      </span>
    </summary>
    ${g.overview ? `<p class="saga-overview">${esc(g.overview)}</p>` : ""}
    <ul class="saga-parts">${g.parts.map(sagaPartHtml).join("")}</ul>
  </details>`;
}

// Sagas visibles dans la vue en cours (bibliothèque, 1fichier ou Disques).
function sagasVisible() {
  if (state.surface === "of") return !!state.of.sagasOn;
  if (state.surface === "disk") return !!state.disk.sagasOn;
  return state.view === "sagas" && state.mediaType === "films";
}

// Sagas depuis la vue 1fichier ou Disques : le panneau remplace la liste, les filtres restent.
// Les données sont les mêmes que dans la bibliothèque (toutes les sources, chaque film dit où il est).
function setSagasSurface(surface, on) {
  const key = surface === "of" ? "of" : "disk";
  state[key].sagasOn = on;
  const btn = $(`${surface}-sagas-btn`);
  btn.classList.toggle("is-active", on);
  btn.setAttribute("aria-pressed", String(on));
  $(`${surface}-view`).classList.toggle("sagas-mode", on);
  $(`${surface}-sagas`).classList.toggle("hidden", !on);
  if (on) loadSagas();
  else clearTimeout(SAGAS.timer);
}

function paintSagas() {
  document.querySelectorAll("[data-sagas-panel]").forEach(paintSagasInto);
}

// Remplit un panneau des sagas (même contenu partout).
function paintSagasInto(panel) {
  if (!panel) return;
  if (SAGAS.loading && !SAGAS.data) {
    panel.innerHTML = `<p class="muted">Chargement des sagas…</p>`;
    return;
  }
  if (SAGAS.error) {
    panel.innerHTML = `<div class="empty"><h3>Sagas indisponibles</h3><p>${esc(SAGAS.error)}</p>
      <button class="btn btn-primary" type="button" data-action="sagas-retry">Réessayer</button></div>`;
    return;
  }
  const d = SAGAS.data || {};
  if (!d.tmdb_enabled) {
    panel.innerHTML = `<div class="empty"><h3>Sagas indisponibles</h3>
      <p>Active TMDB dans les réglages : c'est lui qui donne la saga (collection) de chaque film.</p></div>`;
    return;
  }
  const notes = [];
  if (d.running) notes.push("récupération des fiches TMDB en cours…");
  if (d.films_without_tmdb) notes.push(`${d.films_without_tmdb} film(s) sans identifiant TMDB ne sont pas classés : identifie-les depuis leur fiche`);
  const head = notes.length ? `<p class="muted small sagas-note">${esc(notes.join(" · "))}</p>` : "";
  panel.innerHTML = head + (d.sagas && d.sagas.length
    ? `<div class="saga-grid">${d.sagas.map(sagaCardHtml).join("")}</div>`
    : `<div class="empty"><div class="glyph" aria-hidden="true">◎</div><h3>Aucune saga pour l'instant</h3>
       <p>Une saga apparaît dès qu'un de ses films est possédé et identifié sur TMDB.</p></div>`);
}

function renderContent() {
  const grid = $("results-grid");
  const listWrap = $("results-list");
  const isList = state.view === "list";
  const sagasOn = state.view === "sagas" && state.mediaType === "films";
  $("sagas-panel").classList.toggle("hidden", !sagasOn);
  if (sagasOn) {
    grid.classList.add("hidden");
    listWrap.classList.add("hidden");
    $("empty-state").classList.add("hidden");
    if (!SAGAS.loaded && !SAGAS.loading) loadSagas();
    paintSagas();
    return;
  }

  if (state.loading) {
    grid.classList.remove("hidden");
    listWrap.classList.add("hidden");
    $("empty-state").classList.add("hidden");
    grid.innerHTML = skeletonHtml();
    return;
  }

  if (state.error) {
    grid.innerHTML = "";
    grid.classList.add("hidden");
    listWrap.classList.add("hidden");
    setEmpty(`<div class="glyph">⚠</div><h3>Recherche impossible</h3><p>${esc(state.error)}</p>` +
      `<button class="btn btn-primary" data-action="retry" type="button">Réessayer</button>`);
    return;
  }

  if (!state.searched) {
    grid.innerHTML = "";
    grid.classList.add("hidden");
    listWrap.classList.add("hidden");
    setEmpty(`<div class="glyph">◎</div><h3>Ta médiathèque</h3>` +
      `<p>Lance une recherche, ou affiche tout le contenu.</p>` +
      `<button class="btn btn-primary" data-action="search-all" type="button">Tout afficher</button>`);
    return;
  }

  if (state.results.length === 0) {
    grid.innerHTML = "";
    grid.classList.add("hidden");
    listWrap.classList.add("hidden");
    const noun = { films: "Aucun film trouvé.", series: "Aucune série trouvée.", ebooks: "Aucun ebook trouvé." }[state.mediaType];
    setEmpty(`<div class="glyph">∅</div><h3>${noun}</h3>` +
      `<p>Vérifie le terme recherché et les dossiers sources dans les réglages.</p>`);
    return;
  }

  if (state.visible.length === 0) {
    grid.innerHTML = "";
    grid.classList.add("hidden");
    listWrap.classList.add("hidden");
    setEmpty(`<div class="glyph">⌕</div><h3>Aucun résultat avec ces filtres</h3>` +
      `<p>Élargis les filtres ou remets-les à zéro.</p>` +
      `<button class="btn btn-ghost" data-action="reset-filters" type="button">Réinitialiser les filtres</button>`);
    return;
  }

  $("empty-state").classList.add("hidden");
  if (isList) {
    grid.classList.add("hidden");
    listWrap.classList.remove("hidden");
    $("list-head").innerHTML = headerHtml();
    $("list-body").innerHTML = state.visible.map((it, i) => rowHtml(it, i)).join("");
  } else {
    listWrap.classList.add("hidden");
    grid.classList.remove("hidden");
    grid.innerHTML = state.visible.map((it, i) => cardHtml(it, i)).join("");
    grid.classList.toggle("is-large", state.visible.length > LARGE_GRID);
  }
}

// ---------------------------------------------------------------------
// Sélection
// ---------------------------------------------------------------------

function setSelected(idx, on) {
  const it = state.visible[idx];
  if (!it) return;
  const key = itemKey(it);
  if (on) state.selected.add(key); else state.selected.delete(key);
  refreshSelectionDom();
  updateSelectionUi();
  scheduleComputePlan();
}

function refreshSelectionDom() {
  document.querySelectorAll("[data-select]").forEach((cb) => {
    const it = state.visible[Number(cb.dataset.select)];
    if (!it) return;
    const on = state.selected.has(itemKey(it));
    cb.checked = on;
    const host = cb.closest(".card, tr");
    if (host) host.classList.toggle("is-selected", on);
  });
  syncSelectAll();
}

function syncSelectAll() {
  const box = $("select-all");
  const n = state.visible.length;
  const selected = state.visible.filter((it) => state.selected.has(itemKey(it))).length;
  box.checked = n > 0 && selected === n;
  box.indeterminate = selected > 0 && selected < n;
  box.disabled = n === 0;
}

function selectedSize() {
  let total = 0;
  for (const it of state.results) {
    if (state.selected.has(itemKey(it))) total += it.size || 0;
  }
  return total;
}

function updateSelectionUi() {
  const n = state.selected.size;
  const size = formatSize(selectedSize());
  $("sel-count").textContent = NF0.format(n);
  $("sel-size").textContent = size;
  $("selbar-count").textContent = NF0.format(n);
  $("selbar-size").textContent = n ? `· ${size}` : "";
  updateSelbarVisibility();
}

function updateSelbarVisibility() {
  const sheetOpen = $("side").classList.contains("is-open");
  $("selbar").classList.toggle("hidden", state.selected.size === 0 || sheetOpen);
}

let planTimer = null;
let planSeq = 0;  // numéro du dernier calcul de plan : une réponse plus ancienne est ignorée
function scheduleComputePlan() {
  clearTimeout(planTimer);
  planTimer = setTimeout(computePlan, 150);
}

// Clé de la sélection : un plan n'est utilisable que s'il a été calculé pour cette sélection.
function selectionKey() {
  return [...state.selected].sort().join("\u0000");
}

async function computePlan() {
  const buttons = [$("action-copy"), $("action-move"), $("action-delete"), $("action-1f")];
  const seq = ++planSeq;
  const key = selectionKey();
  if (state.selected.size === 0) {
    state.plan = { items: [], total_size: 0, warnings: [], merge: false, key };
    $("plan-warnings").classList.add("hidden");
    buttons.forEach((b) => (b.disabled = true));
    return;
  }
  try {
    let data;
    if (state.mediaType === "films" || state.mediaType === "ebooks") {
      const mode = document.querySelector('input[name="mode"]:checked').value;
      const endpoint = state.mediaType === "films" ? "/api/plan/films" : "/api/plan/ebooks";
      data = await api(endpoint, { method: "POST", body: JSON.stringify({ mode, paths: [...state.selected] }) });
    } else {
      const namesPaths = {};
      for (const it of state.results) {
        if (state.selected.has(it.name)) namesPaths[it.name] = it.paths;
      }
      data = await api("/api/plan/series", { method: "POST", body: JSON.stringify({ names_paths: namesPaths }) });
    }
    if (seq !== planSeq) return;  // réponse périmée : la sélection a changé depuis
    // Sans session adulte, le compte des films adultes ignorés ne s'affiche pas (il révélerait leur présence).
    const warnings = (data.warnings || []).filter((w) => adult.unlocked || !/adulte/i.test(w));
    state.plan = { ...data, warnings, key };
    const warnBox = $("plan-warnings");
    if (warnings.length) {
      warnBox.innerHTML = `<ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`;
      warnBox.classList.remove("hidden");
    } else {
      warnBox.classList.add("hidden");
    }
    buttons.forEach((b) => (b.disabled = data.items.length === 0));
  } catch (err) {
    if (seq !== planSeq) return;
    console.error("computePlan() a échoué :", err);
    toast(err.message, "error");
  }
}

function selectWatchedVisible() {
  const watched = state.visible.filter((it) => it.watched === true);
  if (watched.length === 0) {
    toast("Aucun élément vu dans la liste affichée (Plex doit être configuré et à jour).", "info");
    return;
  }
  for (const it of watched) state.selected.add(itemKey(it));
  refreshSelectionDom();
  updateSelectionUi();
  scheduleComputePlan();
  toast(`${NF0.format(watched.length)} élément(s) vu(s) sélectionné(s).`, "success");
}

function toggleSelectAllVisible(on) {
  for (const it of state.visible) {
    const key = itemKey(it);
    if (on) state.selected.add(key); else state.selected.delete(key);
  }
  refreshSelectionDom();
  updateSelectionUi();
  scheduleComputePlan();
}

// ---------------------------------------------------------------------
// Fiche détaillée
// ---------------------------------------------------------------------

function currentItem() {
  return state.visible.find((it) => itemKey(it) === state.detailKey) || null;
}

function openDetail(idx) {
  const it = state.visible[idx];
  if (!it) return;
  state.detailKey = itemKey(it);
  renderDetail(it);
  openDrawer("detail-drawer");
  $("detail-body").scrollTop = 0;  // une nouvelle fiche commence en haut ; un rafraîchissement la garde
}

function detailNav(delta) {
  const i = state.visible.findIndex((it) => itemKey(it) === state.detailKey);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= state.visible.length) return;
  openDetail(j);
}

function updateDetailNav() {
  const i = state.visible.findIndex((it) => itemKey(it) === state.detailKey);
  $("detail-prev").disabled = i <= 0;
  $("detail-next").disabled = i < 0 || i >= state.visible.length - 1;
}

function factRow(label, valueHtml, wide = false) {
  return `<div class="${wide ? "wide" : ""}"><dt>${esc(label)}</dt><dd>${valueHtml}</dd></div>`;
}

function sourceNote(src) {
  return src ? ` <span class="muted small">(${esc(SOURCE_LABEL[src] || src)})</span>` : "";
}

function factsHtml(it) {
  const rows = [];
  rows.push(factRow("Année", it.year ? `${esc(it.year)}${sourceNote(it.year_source)}` : "—"));
  rows.push(factRow("Ajouté le", it.added_at ? `${fmtDate(it.added_at)}${sourceNote(it.added_source)}` : "—"));
  if (state.mediaType === "films") {
    rows.push(factRow("Durée", fmtDuration(it.duration_min)));
    rows.push(factRow("Classification", esc(it.content_rating || "—")));
    rows.push(factRow("Taille", formatSize(it.size)));
    rows.push(factRow("Qualité", esc(it.quality && it.quality !== "?" ? it.quality : "—")));
    rows.push(factRow("Audio", esc(it.audio && it.audio !== "?" ? it.audio : "—")));
    rows.push(factRow("Statut Plex", esc(statusLong(it))));
    rows.push(factRow("Dernière lecture", it.last_viewed_at ? fmtDate(it.last_viewed_at) : "—"));
    rows.push(factRow("Dossier", esc(it.folder || "—")));
    rows.push(factRow("Fichier", `<code>${esc(it.name)}</code>`, true));
  } else if (state.mediaType === "series") {
    rows.push(factRow("Sur le disque", `${esc(it.episodes)} épisode(s) · ${esc(it.seasons ?? "?")} saison(s)`));
    rows.push(factRow("Dans Plex", it.plex_episodes ? `${esc(it.viewed_episodes || 0)} / ${esc(it.plex_episodes)} vus · ${esc(it.plex_seasons ?? "?")} saison(s)` : "Non trouvée"));
    rows.push(factRow("Statut Plex", esc(statusLong(it))));
    rows.push(factRow("Dernière lecture", it.last_viewed_at ? fmtDate(it.last_viewed_at) : "—"));
    rows.push(factRow("Taille", formatSize(it.size)));
    rows.push(factRow("Dossiers source", esc(it.source_count)));
  } else {
    rows.push(factRow("Auteur", esc(it.author || "non détecté dans le nom")));
    rows.push(factRow("Format", esc(it.format)));
    rows.push(factRow("Taille", formatSize(it.size)));
    rows.push(factRow("Dossier", esc(it.folder || "—")));
    rows.push(factRow("Fichier", `<code>${esc(it.name)}</code>`, true));
  }
  return rows.join("");
}

// Distribution d'un film (version 35) : chargée à l'ouverture de la fiche, une fois par film.
