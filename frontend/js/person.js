"use strict";
// person.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Fiche technique, distribution et fiche des personnes (version 38)
// ---------------------------------------------------------------------

const creditsCache = new Map();  // identifiant TMDB du film -> fiche technique (un seul appel par film)
const PERSON_GROUPS = [["jeu", "Jeu"], ["realisation", "Réalisation"], ["scenario", "Scénario"], ["autres", "Autres fonctions"]];
const KNOWN_FOR_LABEL = { Acting: "Acteur", Directing: "Réalisateur", Writing: "Scénariste", Production: "Producteur",
  Editing: "Monteur", Sound: "Son", Camera: "Image" };
const PLACE_LABEL = { serveur: "Serveur", disque: "Disque", "1fichier": "1fichier" };
let personData = null;      // fiche de la personne affichée
let personGroup = null;     // rôle affiché : jeu, realisation, scenario ou autres
let personSeq = 0;          // une réponse arrivée après l'ouverture d'une autre fiche est ignorée
let personReturnTo = null;  // tiroir à rouvrir avec « Retour » (fiche du film ou de 1fichier)

function castSectionHtml(tmdbId) {
  const id = Number(tmdbId);  // l'index 1fichier peut garder l'identifiant sous forme de texte
  if (!Number.isInteger(id) || id <= 0) return "";
  return `<section class="detail-section credits-section hidden" data-cast-id="${id}">
      <div class="crew-body"></div>
      <div class="cast-block hidden"><h3>Distribution</h3><div class="cast-body"></div></div>
    </section>`;
}

function castInitials(name) {
  return String(name).split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0]).join("").toUpperCase();
}

// Photo sur fond d'initiales : si l'image manque, elle disparaît et les initiales restent visibles.
function photoHtml(url, name, cls) {
  return `<span class="${cls}"><span class="cast-initials" aria-hidden="true">${esc(castInitials(name || "?"))}</span>${url
    ? `<img src="${esc(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ""}</span>`;
}

function castListHtml(cast) {
  return `<ul class="cast-strip">${cast.map((c) => `<li class="cast-item">
      <button type="button" class="cast-btn" data-person-id="${Number(c.id) || 0}" data-person-name="${esc(c.name)}"${c.id ? "" : " disabled"}
        title="Voir la filmographie de ${esc(c.name)}">
        ${photoHtml(c.photo_url, c.name, "cast-photo")}
        <span class="cast-name">${esc(c.name)}</span>
        ${c.character ? `<span class="cast-role">${esc(c.character)}</span>` : ""}
      </button>
    </li>`).join("")}</ul>`;
}

function personChipHtml(p) {
  return `<button type="button" class="person-chip" data-person-id="${Number(p.id)}" data-person-name="${esc(p.name)}"
      title="Voir la filmographie de ${esc(p.name)}">${photoHtml(p.photo_url, p.name, "person-chip-photo")}<span>${esc(p.name)}</span></button>`;
}

function studioHtml(s) {
  const label = `${s.name}${s.country ? ` (${s.country})` : ""}`;
  const logo = s.logo_url ? `<img src="${esc(s.logo_url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : "";
  return `<span class="studio${logo ? " has-logo" : ""}" title="${esc(label)}"${logo ? ` role="img" aria-label="${esc(label)}"` : ""}>${logo}<span class="studio-name">${esc(s.name)}</span></span>`;
}

function crewRowHtml(label, html) {
  return `<div><dt>${esc(label)}</dt><dd>${html}</dd></div>`;
}

// Fiche technique : réalisation, scénario et studios (logos sur fond blanc). Vide si TMDB ne renvoie rien.
function crewHtml(c) {
  const rows = [];
  if (c.directors.length) rows.push(crewRowHtml("Réalisation", c.directors.map(personChipHtml).join("")));
  if (c.writers.length) rows.push(crewRowHtml("Scénario", c.writers.map(personChipHtml).join("")));
  if (c.studios.length) rows.push(crewRowHtml(c.studios.length > 1 ? "Studios" : "Studio", c.studios.map(studioHtml).join("")));
  if (!rows.length) return "";
  return `<h3>Fiche technique</h3><dl class="credits-list">${rows.join("")}</dl>`;
}

// Repli sur les initiales quand une image ne charge pas (photo, affiche, logo de studio).
function wirePhotos(root) {
  root.querySelectorAll(".cast-photo img, .person-chip-photo img, .person-photo img, .studio img, .pf-poster img").forEach((img) =>
    img.addEventListener("error", () => {
      const studio = img.closest(".studio");
      if (studio) studio.classList.remove("has-logo");
      img.remove();
    }, { once: true }));
}

// Remplit la fiche technique et la distribution d'un film. Sans données (TMDB désactivé, film inconnu,
// panne), la section reste masquée. Une réponse vide ou une panne n'est jamais gardée en cache.
async function hydrateCast(container) {
  const gen = adultGen;
  for (const section of container.querySelectorAll("[data-cast-id]")) {
    const id = Number(section.dataset.castId);
    let credits = creditsCache.get(id) || null;
    if (!credits) {
      try {
        credits = await api(`/api/tmdb/credits?tmdb_id=${id}`);
        if (gen !== adultGen) return;  // accès adulte changé : cette fiche n'est plus à l'écran, rien n'est gardé
        if (credits.cast.length || credits.directors.length || credits.writers.length || credits.studios.length) {
          creditsCache.set(id, credits);
        }
      } catch (_) {
        if (gen !== adultGen) return;
        credits = null;
      }
    }
    if (!section.isConnected || !credits) continue;  // fiche fermée ou changée entre-temps
    const crew = crewHtml(credits);
    const hasCast = credits.cast.length > 0;
    if (!crew && !hasCast) continue;
    section.querySelector(".crew-body").innerHTML = crew;
    if (hasCast) {
      section.querySelector(".cast-body").innerHTML = castListHtml(credits.cast);
      section.querySelector(".cast-block").classList.remove("hidden");
    }
    wirePhotos(section);
    section.classList.remove("hidden");
  }
}

function fmtIsoDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return Number.isNaN(d.getTime()) ? iso : DF_LONG.format(d);
}

// ---- Fiche personne : biographie, dates et filmographie, avec présence dans la bibliothèque ----

function openPerson(personId, name) {
  const id = Number(personId);
  if (!Number.isInteger(id) || id <= 0) return;
  personReturnTo = DRAWERS.find((d) => d !== "person-drawer" && !$(d).classList.contains("hidden")) || null;
  const seq = ++personSeq;
  personData = null;
  personGroup = null;
  $("person-body").innerHTML = personLoadingHtml(name);
  openDrawer("person-drawer");
  loadPerson(id, seq);
}

function personLoadingHtml(name) {
  return `<div class="person-hero" aria-busy="true">
      <div class="person-photo skeleton" aria-hidden="true"></div>
      <div class="person-head"><h2>${esc(name || "Personne")}</h2><span class="muted small">Chargement de la fiche…</span></div>
    </div>
    <div class="person-grid" aria-hidden="true">${'<div class="pf-skeleton skeleton"></div>'.repeat(6)}</div>`;
}

async function loadPerson(id, seq) {
  try {
    const data = await api(`/api/tmdb/person?person_id=${id}`);
    if (seq !== personSeq) return;
    personData = data;
    renderPerson();
  } catch (err) {
    if (seq !== personSeq) return;
    $("person-body").innerHTML = `<div class="person-empty"><h2>Fiche indisponible</h2><p class="muted">${esc(err.message)}</p></div>`;
  }
}

function renderPerson() {
  const p = personData;
  if (!p) return;
  const groups = PERSON_GROUPS
    .map(([key, label]) => ({ key, label, films: p.films.filter((f) => f.roles.some((r) => r.group === key)) }))
    .filter((g) => g.films.length);
  const current = groups.find((g) => g.key === personGroup) || groups[0] || null;
  personGroup = current ? current.key : null;

  const facts = [];
  if (p.birthday) facts.push(`Né(e) le ${fmtIsoDate(p.birthday)}${p.place_of_birth ? ` à ${p.place_of_birth}` : ""}`);
  else if (p.place_of_birth) facts.push(`Lieu de naissance : ${p.place_of_birth}`);
  if (p.deathday) facts.push(`Décédé(e) le ${fmtIsoDate(p.deathday)}`);
  const longBio = (p.biography || "").length > 420;
  const bio = p.biography ? `<section class="detail-section"><h3>Biographie</h3>
      <div class="person-bio${longBio ? " is-clamped" : ""}" id="person-bio"><p>${esc(p.biography)}</p></div>
      ${longBio ? `<button type="button" class="btn btn-ghost small" data-bio-toggle="1" aria-expanded="false">Lire la suite</button>` : ""}
    </section>` : "";
  const owned = p.owned_count || 0;
  const count = p.films.length;

  $("person-body").innerHTML = `
    <header class="person-hero">
      ${photoHtml(p.photo_url, p.name, "person-photo")}
      <div class="person-head">
        <span class="eyebrow">${esc(KNOWN_FOR_LABEL[p.known_for] || "Personne")}</span>
        <h2 id="person-title">${esc(p.name || "Sans nom")}</h2>
        ${facts.length ? `<ul class="person-facts">${facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      </div>
    </header>
    <div class="person-stats">
      <span>${NF0.format(count)} film${count > 1 ? "s" : ""}</span>
      <span class="sep" aria-hidden="true">·</span>
      <span class="${owned ? "is-owned" : ""}">${NF0.format(owned)} dans ta bibliothèque</span>
      <a class="person-tmdb" href="${esc(p.tmdb_url)}" target="_blank" rel="noopener noreferrer">Voir sur TMDB ↗</a>
    </div>
    ${bio}
    ${groups.length > 1
      ? `<div class="seg person-groups" role="group" aria-label="Rôle dans la filmographie">${groups.map((g) => `<button type="button"
          class="seg-btn${g.key === personGroup ? " is-active" : ""}" data-person-group="${g.key}" aria-pressed="${g.key === personGroup}">${esc(g.label)}
          <span class="seg-count">${NF0.format(g.films.length)}</span></button>`).join("")}</div>`
      : current ? `<h3 class="person-group-title">${esc(current.label)}<span class="seg-count">${NF0.format(current.films.length)}</span></h3>` : ""}
    <div class="person-grid">${current
      ? current.films.map((f) => personFilmHtml(f, current.key)).join("")
      : `<p class="muted person-none">Aucune filmographie connue pour cette personne.</p>`}</div>`;
  wirePhotos($("person-body"));
}

function personFilmHtml(f, group) {
  const role = f.roles.find((r) => r.group === group);
  const places = f.owned || [];
  const meta = [f.year || "Sans date", role ? role.label : null].filter(Boolean).join(" · ");
  const badges = places.length
    ? places.map((o) => `<span class="pf-badge ok">${esc(PLACE_LABEL[o.where] || o.where)}</span>`).join("")
    : `<span class="pf-badge">Absent</span>`;
  const body = `
    <span class="pf-poster">
      <span class="pf-initial" aria-hidden="true">${esc((f.title || "?").charAt(0).toUpperCase())}</span>${f.poster_url
        ? `<img src="${esc(f.poster_url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ""}
      ${f.rating ? `<span class="pf-rating">★ ${NF1.format(f.rating)}</span>` : ""}
      ${f.adult ? `<span class="pf-adult">Adulte</span>` : ""}
    </span>
    <span class="pf-title">${esc(f.title)}</span>
    <span class="pf-meta">${esc(meta)}</span>
    <span class="pf-badges">${badges}</span>`;
  return places.length
    ? `<button type="button" class="pf-card is-owned" data-film-id="${Number(f.tmdb_id)}"
        title="Chercher « ${esc(f.title)} » dans la bibliothèque">${body}</button>`
    : `<div class="pf-card">${body}</div>`;
}

// Ouvre un film possédé dans l'onglet où il se trouve, avec son titre comme recherche.
function openOwnedFilm(film) {
  const first = film && (film.owned || [])[0];  // le serveur passe en premier
  if (!first) return;
  const where = first.where === "serveur" ? "films" : first.where === "disque" ? "disques" : "1fichier";
  personData = null;
  personGroup = null;
  $("search-term").value = first.term || film.title;  // nom du fichier : c'est ce que la bibliothèque cherche
  setMediaType(where);  // ferme les tiroirs, change d'onglet et relance la recherche
}

function backFromPerson() {
  personData = null;
  if (personReturnTo) openDrawer(personReturnTo);
  else closeAllOverlays();
}

function initPerson() {
  $("person-back").addEventListener("click", backFromPerson);
  $("person-close").addEventListener("click", closeAllOverlays);
  for (const host of [$("detail-body"), $("of-drawer-body")]) {
    host.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-person-id]");
      if (btn && !btn.disabled) openPerson(btn.dataset.personId, btn.dataset.personName);
    });
  }
  $("person-body").addEventListener("click", (e) => {
    const group = e.target.closest("[data-person-group]");
    if (group) { personGroup = group.dataset.personGroup; renderPerson(); return; }
    const owned = e.target.closest("[data-film-id].is-owned");
    if (owned && personData) {
      openOwnedFilm(personData.films.find((f) => f.tmdb_id === Number(owned.dataset.filmId)));
      return;
    }
    const toggle = e.target.closest("[data-bio-toggle]");
    if (toggle) {
      const clamped = $("person-bio").classList.toggle("is-clamped");
      toggle.textContent = clamped ? "Lire la suite" : "Réduire";
      toggle.setAttribute("aria-expanded", String(!clamped));
    }
  });
}

function renderDetail(it) {
  const title = itemTitle(it);
  const key = itemKey(it);
  const sel = state.selected.has(key);
  const st = statusOf(it);
  const paths = state.mediaType === "series" ? it.paths : [it.path];

  const chips = [];
  if (state.mediaType === "films") {
    if (it.quality && it.quality !== "?") chips.push(`<span class="chip accent">${esc(it.quality)}</span>`);
    if (it.audio && it.audio !== "?") chips.push(`<span class="chip info">${esc(it.audio)}</span>`);
    if (it.content_rating) chips.push(`<span class="chip">${esc(it.content_rating)}</span>`);
  } else if (state.mediaType === "series") {
    if (it.seasons) chips.push(`<span class="chip">${it.seasons} saison${it.seasons > 1 ? "s" : ""}</span>`);
    chips.push(`<span class="chip">${it.episodes} épisode${it.episodes > 1 ? "s" : ""}</span>`);
  } else {
    chips.push(`<span class="chip info">${esc(it.format)}</span>`);
  }
  if (it.identified) chips.push(`<span class="chip info" title="Identifié dans l'application : le fichier du serveur garde son nom">Identifié</span>`);
  if (st) chips.push(`<span class="chip ${st.cls}">${esc(st.label)}</span>`);

  const sub = state.mediaType === "films"
    ? [it.year, it.duration_min ? fmtDuration(it.duration_min) : null].filter(Boolean).join(" · ")
    : state.mediaType === "series"
      ? [it.year, `${it.episodes} épisode${it.episodes > 1 ? "s" : ""}`].filter(Boolean).join(" · ")
      : it.author || "Auteur non détecté";

  const eyebrow = TYPE_LABEL[state.mediaType];
  const rating = it.rating !== null && it.rating !== undefined
    ? `<span class="rating">${fmtRating(it.rating)}</span>${it.rating_source ? sourceNote(it.rating_source) : ""}`
    : `<span class="muted">Pas de note disponible</span>`;

  const summary = it.summary
    ? `<p class="summary">${esc(it.summary)}</p>`
    : `<p class="summary none">Aucun résumé disponible${state.tmdbEnabled ? "" : " — active TMDB dans les réglages pour en obtenir un"}.</p>`;

  const genres = (it.genres || []).length
    ? `<section class="detail-section"><h3>Genres</h3><div class="genres">${it.genres.map((g) => `<span class="chip">${esc(g)}</span>`).join("")}</div></section>`
    : "";

  const pathsList = paths.map((p) => `<li><span>${esc(p)}</span><button type="button" class="btn btn-ghost" data-copy="${esc(p)}">Copier</button></li>`).join("");

  $("detail-body").innerHTML = `
    <div class="detail-hero">
      <div class="detail-bg" aria-hidden="true"></div>
      <div class="detail-hero-inner">
        <div class="detail-poster">${mediaHtml(it, -1, "", { tag: "span" })}</div>
        <div class="detail-head">
          <span class="eyebrow">${esc(eyebrow)}</span>
          <h2 id="detail-title">${esc(title)}</h2>
          <span class="sub">${esc(sub)}</span>
          <div class="chips">${chips.join("")}</div>
        </div>
      </div>
    </div>
    <div class="detail-actions">
      <button type="button" class="btn ${sel ? "btn-ghost" : "btn-primary"}" data-detail-toggle="1">
        ${sel ? "Retirer de la sélection" : "Ajouter à la sélection"}
      </button>
      <button type="button" class="btn btn-ghost" data-copy="${esc(title)}">Copier le titre</button>
      ${state.mediaType === "ebooks" ? `<button type="button" class="btn btn-ghost" data-ebook-cover="${esc(it.path)}">Changer la jaquette</button>` : ""}
      ${state.mediaType === "films" || state.mediaType === "series" ? `<button type="button" class="btn btn-ghost" data-server-identify="1"
        title="Choisir le bon titre sur TMDB et renommer en conséquence">Identifier / renommer</button>` : ""}
      ${state.mediaType === "films" && adult.unlocked ? `<button type="button" class="btn btn-ghost" data-server-category="${it.adult ? "0" : "1"}"
        title="${it.adult ? "Retirer ce film de la catégorie Adultes" : "Ranger ce film dans la catégorie Adultes, masquée sans code"}">${it.adult ? "Sortir de la catégorie Adultes" : "Déplacer dans Adultes"}</button>` : ""}
    </div>
    <section class="detail-section"><h3>Note</h3><p>${rating}</p></section>
    <section class="detail-section"><h3>Résumé</h3>${summary}</section>
    ${genres}
    ${castSectionHtml(state.mediaType === "films" ? it.tmdb_id : null)}
    <section class="detail-section"><h3>Informations</h3><dl class="facts">${factsHtml(it)}</dl></section>
    <section class="detail-section"><h3>Emplacement${paths.length > 1 ? "s" : ""}</h3><ul class="paths">${pathsList}</ul></section>
    ${state.mediaType === "series" ? `<section class="detail-section" id="detail-seasons"><h3>Saisons</h3><p class="muted small">Chargement des saisons…</p></section>` : ""}
  `;

  const bg = $("detail-body").querySelector(".detail-bg");
  if (bg && it.poster_url) {
    bg.style.backgroundImage = cssUrl(it.poster_url);
  } else if (bg) {
    bg.style.backgroundImage = `linear-gradient(150deg, hsl(${hueOf(title)} 40% 30%), hsl(${(hueOf(title) + 50) % 360} 40% 10%))`;
  }
  updateDetailNav();
  if (state.mediaType === "series") loadDetailSeasons(it, key);
  hydrateCast($("detail-body"));
}

async function loadDetailSeasons(it, key) {
  const paths = it.paths || [];
  try {
    const results = await Promise.all(paths.map((p) => api(`/api/series/seasons?path=${encodeURIComponent(p)}`)));
    if (state.detailKey !== key) return;  // une autre fiche est ouverte entre-temps
    renderDetailSeasons(results, paths);
  } catch (err) {
    if (state.detailKey !== key) return;
    const box = $("detail-seasons");
    if (box) box.innerHTML = `<h3>Saisons</h3><p class="muted small">${esc(err.message)}</p>`;
  }
}

function seasonRowHtml(s, sourceNo, multi) {
  const numbered = s.season !== null && s.season !== undefined;
  const label = numbered ? `Saison ${s.season}` : s.name;
  const sub = [`${s.episodes} épisode${s.episodes > 1 ? "s" : ""}`, formatSize(s.size),
               multi ? `source ${sourceNo + 1}` : null].filter(Boolean).join(" · ");
  const sent = s.sent ? `<span class="chip ok">Sur 1fichier</span>` : `<span class="chip">Non envoyée</span>`;
  const data = `data-season-path="${esc(s.path)}" data-season-size="${Number(s.size) || 0}"`;
  const actions = numbered
    ? `<button type="button" class="btn btn-ghost" data-season-action="archive_1fichier" ${data}
          title="Envoyer cette saison sur 1fichier">1fichier</button>
       <button type="button" class="btn btn-ghost" data-season-action="copy" ${data}
          title="Copier cette saison vers le disque choisi">Copier</button>
       <button type="button" class="btn btn-ghost" data-season-action="move" ${data}
          title="Déplacer cette saison vers le disque choisi">Déplacer</button>`
    : `<span class="muted small">Dossier sans numéro : renomme-le « Saison N » pour l'envoyer par saison.</span>`;
  return `<li class="season-row">
      <div class="season-main"><strong>${esc(label)}</strong> ${sent} <span class="muted small">${esc(sub)}</span></div>
      <div class="season-actions">${actions}</div>
    </li>`;
}

function renderDetailSeasons(results, paths) {
  const box = $("detail-seasons");
  if (!box) return;
  const multi = paths.length > 1;
  const rows = [];
  let rootEpisodes = 0;
  results.forEach((res, no) => {
    (res.seasons || []).forEach((s) => rows.push(seasonRowHtml(s, no, multi)));
    rootEpisodes += res.root_episodes || 0;
  });
  const list = rows.length
    ? `<ul class="season-list">${rows.join("")}</ul>`
    : `<p class="muted small">Aucun dossier de saison avec des vidéos dans cette série.</p>`;
  const root = rootEpisodes
    ? `<p class="muted small">${rootEpisodes} fichier(s) vidéo posé(s) directement dans le dossier de la série : ils ne sont rattachés à aucune saison et partent avec la série entière. Range-les dans un dossier « Saison N » pour les envoyer par saison.</p>`
    : "";
  box.innerHTML = `<h3>Saisons</h3>${list}${root}
    <p class="muted small">Pour la série entière, sélectionne-la et utilise les boutons d'envoi habituels.</p>`;
}

async function seasonAction(btn) {
  const lot = {
    items: [btn.dataset.seasonPath],
    total_size: Number(btn.dataset.seasonSize) || 0,
    merge: true,
  };
  await startAction(btn.dataset.seasonAction, lot);
}

function toggleDetailSelection() {
  const it = currentItem();
  if (!it) return;
  const key = itemKey(it);
  if (state.selected.has(key)) state.selected.delete(key); else state.selected.add(key);
  refreshSelectionDom();
  updateSelectionUi();
  scheduleComputePlan();
  renderDetail(it);
}
