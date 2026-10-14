"use strict";

/* =====================================================================
   Archivage — interface
   État unique -> dérivation (filtres, tri) -> rendu. Les événements sont
   délégués au document : le rendu peut être remplacé sans perdre les
   gestionnaires, et reste rapide sur de grandes bibliothèques.
   ===================================================================== */

// ---------------------------------------------------------------------
// Configuration par type de média
// ---------------------------------------------------------------------

const SORTS = {
  films: [
    { key: "name", label: "Titre" }, { key: "year", label: "Année" }, { key: "added", label: "Date d'ajout" },
    { key: "size", label: "Taille" }, { key: "quality", label: "Qualité" }, { key: "audio", label: "Audio" },
    { key: "rating", label: "Note" },
  ],
  series: [
    { key: "name", label: "Titre" }, { key: "year", label: "Année" }, { key: "added", label: "Date d'ajout" },
    { key: "size", label: "Taille" }, { key: "episodes", label: "Épisodes" }, { key: "rating", label: "Note" },
  ],
  ebooks: [
    { key: "name", label: "Titre" }, { key: "author", label: "Auteur" }, { key: "year", label: "Année" },
    { key: "added", label: "Date d'ajout" }, { key: "size", label: "Taille" }, { key: "format", label: "Format" },
  ],
};

const DEFAULT_DIR = {
  name: "asc", author: "asc", format: "asc",
  year: "desc", added: "desc", size: "desc", quality: "desc", audio: "desc",
  rating: "desc", episodes: "desc",
};

const COLS = {
  films: [
    { id: "title", label: "Titre", sort: "name", cls: "col-title" },
    { id: "year", label: "Année", sort: "year", cls: "c-md" },
    { id: "added", label: "Ajouté", sort: "added", cls: "c-md" },
    { id: "size", label: "Taille", sort: "size", cls: "c-md num" },
    { id: "quality", label: "Qualité", sort: "quality", cls: "c-lg" },
    { id: "audio", label: "Audio", sort: "audio", cls: "c-lg" },
    { id: "rating", label: "Note", sort: "rating", cls: "c-md" },
    { id: "status", label: "Plex", cls: "c-md" },
  ],
  series: [
    { id: "title", label: "Titre", sort: "name", cls: "col-title" },
    { id: "year", label: "Année", sort: "year", cls: "c-md" },
    { id: "episodes", label: "Épisodes", sort: "episodes", cls: "c-md num" },
    { id: "seasons", label: "Saisons", cls: "c-lg num" },
    { id: "size", label: "Taille", sort: "size", cls: "c-md num" },
    { id: "added", label: "Ajouté", sort: "added", cls: "c-md" },
    { id: "rating", label: "Note", sort: "rating", cls: "c-md" },
    { id: "status", label: "Plex", cls: "c-md" },
  ],
  ebooks: [
    { id: "title", label: "Titre", sort: "name", cls: "col-title" },
    { id: "author", label: "Auteur", sort: "author", cls: "c-md" },
    { id: "format", label: "Format", sort: "format", cls: "" },
    { id: "year", label: "Année", sort: "year", cls: "c-lg" },
    { id: "size", label: "Taille", sort: "size", cls: "c-md num" },
    { id: "added", label: "Ajouté", sort: "added", cls: "c-md" },
  ],
};

const TYPE_LABEL = { films: "Film", series: "Série", ebooks: "Ebook" };
const OP_LABEL = { copy: "Copie", move: "Déplacement", delete: "Suppression", archive_1fichier: "Envoi 1fichier", restore_1fichier: "Restauration 1fichier" };
const QUALITY_ORDER = ["4K", "1080p", "720p", "SD", "?"];
const QUALITY_LABEL = { "4K": "4K", "1080p": "1080p", "720p": "720p", "SD": "SD", "?": "Inconnue" };
const SOURCE_LABEL = { plex: "Plex", tmdb: "TMDB", fichier: "nom du fichier" };

// ---------------------------------------------------------------------
// État
// ---------------------------------------------------------------------

const state = {
  searchRefreshing: false, searchPoll: null,
  mediaType: "films",
  results: [],          // résultats de la dernière recherche (non filtrés)
  visible: [],          // après filtres et tri : ce qui est affiché
  selected: new Set(),  // clés : chemin (films, ebooks) ou nom (séries)
  searched: false,
  identify: null,  // fenêtre « Identifier » en cours (films/séries du serveur ou disque)
  loading: false,
  error: null,
  sortKey: "name",
  sortDir: "asc",
  view: ["list", "sagas"].includes(localStorage.getItem("archivage.view")) ? localStorage.getItem("archivage.view") : "grid",
  surface: "library",
  adultOnly: false,   // onglet Adultes : films adultes seulement (session déverrouillée)   // "library" (films, séries, ebooks), "of" (bibliothèque 1fichier) ou "disk" (disques)
  disk: { loaded: false, loading: false, error: null, disks: [], serverDirs: {}, kind: "films",
          diskFilter: "all", tmdbFilter: "all", selected: new Set(), deleteConfirmText: "SUPPRIMER",
          onefichierConfigured: false, timer: null, metaTimer: null, identify: null, sagasOn: false },
  of: { entries: [], configured: false, indexFile: "", detailId: null, dir: "asc",
        restoreDirs: {}, metaRunning: false, pollTimer: null, disks: [], sagasOn: false },
  filters: { plex: "all", genre: "", quality: "", added: "all", tmdb: "all" },
  plan:{ items: [], total_size: 0, warnings: [], merge: false },
  deleteConfirmText: "SUPPRIMER",
  plexEnabled: false,
  tmdbEnabled: false,
  detailKey: null,
  runningJobs: 0,
};

const $ = (id) => document.getElementById(id);
const DRAWERS = ["detail-drawer", "log-drawer", "settings-drawer", "of-drawer", "person-drawer"];

// ---------------------------------------------------------------------
// Utilitaires d'affichage
// ---------------------------------------------------------------------

const NF0 = new Intl.NumberFormat("fr-FR");
const NF1 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const RTF = new Intl.RelativeTimeFormat("fr", { numeric: "auto" });
// Formateurs et comparateurs créés une seule fois. `toLocaleString(…, options)` en construit un à
// chaque appel, et une liste de milliers de cartes en fait plusieurs par carte.
const NF_SIZE_0 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 0, maximumFractionDigits: 0 });
const NF_SIZE_1 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 0, maximumFractionDigits: 1 });
const DF_SHORT = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "short", year: "numeric" });
const DF_LONG = new Intl.DateTimeFormat("fr-FR", { day: "numeric", month: "long", year: "numeric" });
const COLLATOR_TITLE = new Intl.Collator("fr", { sensitivity: "base", numeric: true });
const COLLATOR_TEXT = new Intl.Collator("fr", { sensitivity: "base" });
const COLLATOR_NUMERIC = new Intl.Collator("fr", { numeric: true });
// Au-delà de ce nombre de cartes, la grille passe en mode « grande liste » (style.css) : le
// navigateur ne met en page que les cartes visibles. En dessous, rien ne change.
const LARGE_GRID = 120;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// Valeur d'une URL CSS entre guillemets : les caractères qui sortiraient de la chaîne sont encodés.
function cssUrl(url) {
  return `url("${String(url).replace(/[\\"()\r\n]/g, (c) => encodeURIComponent(c))}")`;
}

// Zones rendues par innerHTML. Une zone n'est réécrite que si son contenu a changé, et jamais pendant
// que l'utilisateur y a le focus (menu déroulant ouvert, bouton au clavier) : le rendu reprend au tour suivant.
const paintedBoxes = new WeakMap();
function paintBox(box, html) {
  if (!box || paintedBoxes.get(box) === html) return false;
  if (box.contains(document.activeElement)) return false;
  box.innerHTML = html;
  paintedBoxes.set(box, html);
  return true;
}

function clearBox(box) {
  if (!box) return;
  box.innerHTML = "";
  paintedBoxes.delete(box);
}

function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return "?";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const digits = i === 0 ? 0 : v < 10 ? 1 : 0;
  return `${(digits ? NF_SIZE_1 : NF_SIZE_0).format(v)} ${units[i]}`;
}

function fmtDate(ts) {
  if (!ts) return "—";
  return DF_SHORT.format(new Date(ts * 1000));
}

// Date d'ajout : relative pour les 30 derniers jours (lisible), absolue sinon.
function fmtAdded(ts) {
  if (!ts) return "—";
  const days = (Date.now() / 1000 - ts) / 86400;
  if (days >= 0 && days < 30) return RTF.format(-Math.floor(days), "day");
  return fmtDate(ts);
}

function fmtDuration(min) {
  if (!min) return "—";
  const h = Math.floor(min / 60);
  const m = min % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min`;
}

function fmtRating(r) {
  return r === null || r === undefined ? "" : `★ ${NF1.format(r)}`;
}

function hueOf(text) {
  let h = 0;
  for (const c of String(text)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function toast(message, kind = "info") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = message;
  $("toast-container").appendChild(t);
  setTimeout(() => t.remove(), kind === "error" ? 8000 : 5000);
}

window.addEventListener("error", (event) => {
  console.error("Erreur JS non interceptée :", event.error || event.message);
  toast(`Erreur JS : ${event.message}`, "error");
});
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  toast(`Erreur : ${reason && reason.message ? reason.message : String(reason)}`, "error");
});

// Une image d'affiche qui ne charge pas bascule sur la couverture générée.
document.addEventListener("error", (event) => {
  const img = event.target;
  if (img && img.tagName === "IMG" && img.classList.contains("poster-img")) {
    const media = img.closest(".media");
    if (media) media.classList.add("no-img");
  }
}, true);

// Jeton d'accès (version 46) : demandé seulement si le serveur protège ses modifications (ACCESS_TOKEN).
// Gardé dans ce navigateur (localStorage) ; en navigation privée, il ne reste que pour la page ouverte.
const JETON_CLE = "archivage.jeton";
let jetonMemoire = null;
let demandeJeton = null;

function jetonCourant() {
  if (jetonMemoire !== null) return jetonMemoire;
  try { return localStorage.getItem(JETON_CLE) || ""; } catch (_) { return ""; }
}

function enregistrerJeton(valeur) {
  jetonMemoire = valeur || "";
  try {
    if (valeur) localStorage.setItem(JETON_CLE, valeur);
    else localStorage.removeItem(JETON_CLE);
  } catch (_) { /* stockage indisponible : le jeton reste en mémoire pour cette page */ }
}

function enTetesModification() {
  const jeton = jetonCourant();
  return jeton ? { "X-Access-Token": jeton } : {};
}

// Dialogue de saisie du jeton. Une seule demande à la fois, même si plusieurs modifications échouent.
function demanderJeton(invalide = false) {
  if (demandeJeton) return demandeJeton;
  demandeJeton = new Promise((resolve) => {
    const modal = $("token-modal");
    const form = $("token-form");
    const input = $("token-input");
    const erreur = $("token-error");
    const annuler = $("token-cancel");
    erreur.textContent = invalide ? "Jeton incorrect. Réessaie." : "";
    erreur.classList.toggle("hidden", !invalide);
    input.value = "";
    modal.classList.remove("hidden");
    setTimeout(() => input.focus(), 0);
    const fin = (valeur) => {
      modal.classList.add("hidden");
      form.removeEventListener("submit", onSubmit);
      annuler.removeEventListener("click", onCancel);
      demandeJeton = null;
      resolve(valeur);
    };
    const onSubmit = (event) => {
      event.preventDefault();
      const valeur = input.value.trim();
      if (valeur) fin(valeur);
    };
    const onCancel = () => fin(null);
    form.addEventListener("submit", onSubmit);
    annuler.addEventListener("click", onCancel);
  });
  return demandeJeton;
}

async function api(path, options = {}, tentative = 0) {
  const { headers: entetes = {}, ...reste } = options;
  const method = (reste.method || "GET").toUpperCase();
  const headers = {
    "Content-Type": "application/json",
    ...(method === "GET" ? {} : enTetesModification()),
    ...entetes,
  };
  const resp = await fetch(path, { ...reste, headers });
  let data = null;
  try { data = await resp.json(); } catch (_) { /* pas de corps JSON */ }
  if (!resp.ok) {
    const jetonRefuse = resp.status === 401 && data && (data.code === "token_required" || data.code === "token_invalid");
    if (jetonRefuse && tentative < 3) {
      // Jeton absent ou refusé : on le redemande (jusqu'à trois essais) avec le motif.
      const saisi = await demanderJeton(data.code === "token_invalid");
      if (!saisi) throw new Error("Modification annulée : jeton d'accès requis (Réglages › Protection).");
      enregistrerJeton(saisi);
      return api(path, options, tentative + 1);
    }
    const message = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `Erreur HTTP ${resp.status}`;
    throw new Error(message);
  }
  return data;
}

function copyText(text) {
  const done = () => toast("Copié dans le presse-papiers.", "success");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  // En HTTP sur le réseau local, l'API Clipboard n'est pas disponible.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { if (document.execCommand("copy")) done(); else toast("Copie impossible : sélectionne le texte manuellement.", "error"); }
  catch (_) { toast("Copie impossible : sélectionne le texte manuellement.", "error"); }
  ta.remove();
}

// ---------------------------------------------------------------------
// Données : clés, titres, statuts, méta
// ---------------------------------------------------------------------

function itemKey(it) {
  return state.mediaType === "series" ? it.name : it.path;
}

function itemTitle(it) {
  return it.title || it.name;
}

// Statut lisible d'un élément (films et séries). null = inconnu (pas dans Plex).
function statusOf(it) {
  if (state.mediaType === "films") {
    if (it.watched === true) return { label: "Vu", cls: "ok", kind: "watched" };
    if (it.watched === false) return { label: "Non vu", cls: "warn", kind: "unwatched" };
    return null;
  }
  if (state.mediaType === "series") {
    if (it.watched === true) return { label: "Terminée", cls: "ok", kind: "watched" };
    if (it.watched === false) {
      const viewed = it.viewed_episodes || 0;
      if (viewed > 0) {
        const total = it.plex_episodes || it.episodes || 0;
        const pct = total ? Math.round((viewed / total) * 100) : null;
        return { label: `En cours ${viewed}/${total}`, cls: "accent", kind: "partial", pct };
      }
      return { label: "Non vue", cls: "warn", kind: "unwatched" };
    }
    return null;
  }
  return null;
}

function statusLong(it) {
  if (state.mediaType === "series") {
    if (it.watched === null || it.watched === undefined) return "Non trouvée dans Plex";
    const total = it.plex_episodes || 0;
    if (it.watched) return `Terminée (${total} épisode(s))`;
    return `${it.viewed_episodes || 0} / ${total} épisode(s) vus`;
  }
  if (it.watched === null || it.watched === undefined) return "Non trouvé dans Plex";
  return it.watched ? "Vu" : "Non vu";
}

function metaLine(it) {
  if (state.mediaType === "films") {
    return `${it.year ?? "année ?"} · ${formatSize(it.size)}`;
  }
  if (state.mediaType === "series") {
    const seasons = it.seasons ? ` · ${it.seasons} saison${it.seasons > 1 ? "s" : ""}` : "";
    return `${it.year ?? "—"} · ${it.episodes} épisode${it.episodes > 1 ? "s" : ""}${seasons} · ${formatSize(it.size)}`;
  }
  const books = it.books && it.books > 1 ? `${NF0.format(it.books)} livres · ` : "";
  return `${it.author || "Auteur inconnu"} · ${books}${it.format}`;
}

function addedTitle(it) {
  if (!it.added_at) return "Date inconnue";
  const src = it.added_source ? ` (${SOURCE_LABEL[it.added_source] || it.added_source})` : "";
  return `Ajouté le ${fmtDate(it.added_at)}${src}`;
}

function qualityChip(it) {
  if (state.mediaType !== "films" || !it.quality || it.quality === "?") return "";
  return `<span class="chip accent">${esc(it.quality)}</span>`;
}

function topChip(it) {
  if (state.mediaType === "films") return qualityChip(it);
  if (state.mediaType === "ebooks") return `<span class="chip info">${esc(it.format)}</span>`;
  if (state.mediaType === "series" && it.seasons) {
    return `<span class="chip dark">${it.seasons} saison${it.seasons > 1 ? "s" : ""}</span>`;
  }
  return "";
}

// ---------------------------------------------------------------------
// Filtres, tri, dérivation
// ---------------------------------------------------------------------

// Filtre « Infos TMDB » : "all" | "without" (sans infos) | "with" (avec infos).
// Un élément a des infos TMDB s'il porte un identifiant TMDB connu ; les ebooks
// n'en ont jamais (leurs infos viennent d'Open Library).
function hasTmdb(kind, tmdbId) {
  return kind !== "ebooks" && kind !== "ebook" && tmdbId !== null && tmdbId !== undefined && tmdbId !== "";
}

function tmdbPass(filter, has) {
  if (filter === "without") return !has;
  if (filter === "with") return has;
  return true;
}

function applyFilters(items) {
  const f = state.filters;
  const now = Date.now() / 1000;
  return items.filter((it) => {
    if (state.mediaType !== "ebooks" && f.plex !== "all") {
      if (f.plex === "watched" && it.watched !== true) return false;
      if (f.plex === "unwatched" && it.watched !== false) return false;
      if (f.plex === "partial" && !(it.watched === false && (it.viewed_episodes || 0) > 0)) return false;
    }
    if (f.genre && !(it.genres || []).includes(f.genre)) return false;
    if (state.mediaType === "films" && f.quality && (it.quality || "?") !== f.quality) return false;
    if (f.added !== "all") {
      if (!it.added_at || now - it.added_at > Number(f.added) * 86400) return false;
    }
    if (state.mediaType !== "ebooks" && !tmdbPass(f.tmdb, hasTmdb(state.mediaType, it.tmdb_id))) return false;
    return true;
  });
}

function sortValue(it, key) {
  switch (key) {
    case "year": return it.year ?? null;
    case "added": return it.added_at ?? null;
    case "size": return it.size ?? null;
    case "quality": return it.quality_score ?? null;
    case "audio": return it.audio_score ?? null;
    case "rating": return it.rating ?? null;
    case "episodes": return it.episodes ?? null;
    case "format": return it.format || "";
    case "author": return it.author || "";
    default: return null;
  }
}

function sortItems(items) {
  const dir = state.sortDir === "desc" ? -1 : 1;
  const key = state.sortKey;
  const byTitle = (a, b) => COLLATOR_TITLE.compare(itemTitle(a), itemTitle(b));
  return [...items].sort((a, b) => {
    if (key === "name") return byTitle(a, b) * dir;
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    const na = va === null || va === "";
    const nb = vb === null || vb === "";
    if (na && nb) return byTitle(a, b);
    if (na) return 1;          // valeurs absentes toujours en fin de liste
    if (nb) return -1;
    if (typeof va === "string") return COLLATOR_TEXT.compare(va, vb) * dir || byTitle(a, b);
    return (va - vb) * dir || byTitle(a, b);
  });
}

function filtersActive() {
  const f = state.filters;
  return f.plex !== "all" || f.genre !== "" || f.quality !== "" || f.added !== "all" || f.tmdb !== "all";
}

function derive() {
  state.visible = sortItems(applyFilters(state.results));
}
