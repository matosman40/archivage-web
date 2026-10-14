// Automatisations (version 47) : des règles « quand … alors … » que le serveur applique seul.
// L'interface ne décide rien : elle affiche les règles, aide à les construire et les envoie à l'API,
// qui valide tout (y compris les droits : suppression, 1fichier, notifications).

const AUTO_POLL_MS = 15000;
const AUTO_TRIGGER_PHRASE = {
  plex_vu: "un film est vu dans Plex",
  nouveau_film: "un nouveau film arrive",
};

state.auto = {
  rules: [], history: [], catalogue: null, capacites: null, disks: [],
  loaded: false, editingId: null, editingActive: true, actions: [], pollTimer: null, busy: false,
};

function fmtAutoDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("fr-FR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

function autoActionSentence(a) {
  if (a.type === "copier_disque") return `copier sur ${a.destination || "un disque à choisir"}`;
  if (a.type === "deplacer_disque") return `déplacer vers ${a.destination || "un disque à choisir"} (retire la source)`;
  if (a.type === "envoyer_1fichier") {
    return a.apres === "liberer" ? "envoyer sur 1fichier (libère le serveur)" : "envoyer sur 1fichier (copie gardée)";
  }
  return "envoyer une notification";
}

// Phrase qui résume une règle, lue telle quelle : c'est la vérification la plus simple qu'on puisse faire.
function autoSentence(r) {
  const filtres = [];
  if (r.note_min !== null && r.note_min !== undefined && r.note_min !== "") filtres.push(`note ≥ ${r.note_min}`);
  if (r.genre) filtres.push(`genre ${r.genre}`);
  const condition = filtres.length ? ` (${filtres.join(", ")})` : "";
  const moment = r.quand === "nuit" ? `la nuit à ${r.heure}` : "dès la détection";
  const actions = (r.actions || []).map(autoActionSentence).join(", puis ");
  return `Quand ${AUTO_TRIGGER_PHRASE[r.declencheur] || r.declencheur}${condition} → ${actions} · ${moment}.`;
}

function renderAutoNotice() {
  const c = state.auto.capacites || {};
  const lignes = [];
  if (c.allow_delete === false) lignes.push("Les actions qui retirent un fichier de sa source sont désactivées sur ce serveur (ALLOW_DELETE=false).");
  if (c.onefichier === false) lignes.push("1fichier n'est pas prêt : clé API et TMDB à renseigner (Réglages › 1fichier).");
  if (c.notify === false) lignes.push("Aucune URL de notification : l'action « notification » est indisponible (Réglages).");
  const box = $("auto-notice");
  box.innerHTML = lignes.map((l) => esc(l)).join("<br>");
  box.classList.toggle("hidden", lignes.length === 0);
}

function autoCardHtml(r) {
  const e = r.etat;
  const attente = e.en_attente
    ? `${e.en_attente} film(s) en attente${r.quand === "nuit" ? ` · traités à ${r.heure}` : ""}`
    : "Rien en attente";
  const apercu = e.fichiers_en_attente.length
    ? `<span class="muted small" title="${esc(e.fichiers_en_attente.join("\n"))}">${esc(e.fichiers_en_attente.slice(0, 3).join(", "))}${e.en_attente > 3 ? "…" : ""}</span>`
    : "";
  const derniere = e.derniere_execution
    ? `Dernière exécution : ${fmtAutoDate(e.derniere_execution)}${e.dernier_resultat && e.dernier_resultat !== "ok" ? ` · ${esc(e.dernier_resultat)}` : ""}`
    : "Jamais exécutée";
  const releve = e.premiere_lecture_faite ? "" : `<span class="chip">premier relevé en cours</span>`;
  return `
    <li class="of-row auto-card${r.actif ? "" : " auto-off"}" data-auto-id="${esc(r.id)}">
      <div class="of-main">
        <div class="auto-card-top">
          <strong>${esc(r.nom)}</strong>
          <span class="chip ${r.actif ? "ok" : ""}">${r.actif ? "active" : "en pause"}</span>
          ${releve}
        </div>
        <p class="auto-sentence">${esc(autoSentence(r))}</p>
        <div class="of-chips"><span class="chip">${esc(attente)}</span></div>
        ${apercu}
        <span class="muted small">${derniere}</span>
      </div>
      <div class="auto-card-actions">
        <button class="btn btn-ghost small" type="button" data-auto-action="run">Lancer maintenant</button>
        <button class="btn btn-ghost small" type="button" data-auto-action="edit">Modifier</button>
        <button class="btn btn-ghost small" type="button" data-auto-action="toggle">${r.actif ? "Mettre en pause" : "Reprendre"}</button>
        <button class="btn btn-ghost small" type="button" data-auto-action="delete">Supprimer</button>
      </div>
    </li>`;
}

function renderAutoList() {
  const list = $("auto-list");
  const empty = $("auto-empty");
  if (!state.auto.rules.length) {
    list.innerHTML = "";
    empty.classList.remove("hidden");
    empty.innerHTML = `<div class="glyph" aria-hidden="true">⚡</div>
      <h3>Aucune automatisation</h3>
      <p>Exemple : « un film est vu dans Plex → copier sur un disque, puis envoyer sur 1fichier, la nuit ». Tu la crées une fois, le serveur s'occupe du reste.</p>
      <button class="btn btn-primary small" type="button" data-auto-action="new">Créer ma première automatisation</button>`;
    return;
  }
  empty.classList.add("hidden");
  list.innerHTML = state.auto.rules.map(autoCardHtml).join("");
}

function renderAutoHistory() {
  const h = state.auto.history;
  $("auto-history").innerHTML = h.length
    ? h.map((e) => `
      <li class="of-row auto-hist${e.ok ? "" : " auto-hist-fail"}">
        <div class="of-main">
          <strong>${esc(e.regle)}</strong>
          <span class="muted small">${esc(fmtAutoDate(e.at))} · ${esc(e.action)} · ${e.elements} élément(s)</span>
          <span class="small ${e.ok ? "" : "auto-fail-text"}">${e.ok ? "OK" : "Échec"} — ${esc(e.detail)}</span>
        </div>
      </li>`).join("")
    : `<li class="muted small">Aucune exécution pour l'instant.</li>`;
}

async function loadAuto({ silent = false } = {}) {
  try {
    const data = await api("/api/workflows");
    Object.assign(state.auto, {
      rules: data.regles, history: data.historique, catalogue: data.catalogue,
      capacites: data.capacites, loaded: true,
    });
    renderAutoNotice();
    renderAutoList();
    renderAutoHistory();
  } catch (err) {
    if (!silent) toast(err.message, "error");
  }
}

async function refreshAutoDisks() {
  try {
    state.auto.disks = (await api("/api/disks")).disks || [];
    if (!$("auto-editor").classList.contains("hidden")) {
      lireActionsDom();
      renderAutoActions();
    }
  } catch (_) {
    // Sans la liste des disques, les destinations déjà choisies restent affichées.
  }
}

// ---------------------------------------------------------------------
// Éditeur
// ---------------------------------------------------------------------

function fillAutoSelects() {
  const cat = state.auto.catalogue;
  if (!cat) return;
  $("auto-trigger").innerHTML = Object.entries(cat.declencheurs)
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  $("auto-when").innerHTML = Object.entries(cat.moments)
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
}

function autoActionAvailable(type) {
  const c = state.auto.capacites || {};
  if (type === "deplacer_disque") return c.allow_delete !== false;
  if (type === "envoyer_1fichier") return c.onefichier !== false;
  if (type === "notifier") return c.notify !== false;
  return true;
}

function autoRetiresSource(a) {
  return a.type === "deplacer_disque" || (a.type === "envoyer_1fichier" && a.apres === "liberer");
}

function renderAutoActions() {
  const cat = state.auto.catalogue;
  if (!cat) return;
  const actions = state.auto.actions;
  const disks = state.auto.disks;
  $("auto-actions").innerHTML = actions.map((a, i) => {
    const options = Object.entries(cat.actions).map(([k, label]) => {
      const dispo = autoActionAvailable(k);
      const selected = k === a.type ? " selected" : "";
      const disabled = !dispo && k !== a.type ? " disabled" : "";
      return `<option value="${esc(k)}"${selected}${disabled}>${esc(label)}${dispo ? "" : " (indisponible)"}</option>`;
    }).join("");

    let champ = `<span class="muted small">Aucun réglage</span>`;
    if (a.type === "copier_disque" || a.type === "deplacer_disque") {
      const connu = a.destination && !disks.some((d) => d.path === a.destination);
      champ = `<select data-auto-field="destination" aria-label="Disque de destination">
          <option value="">Choisir un disque…</option>
          ${disks.map((d) => `<option value="${esc(d.path)}"${d.path === a.destination ? " selected" : ""}>${esc(d.path)} — libre ${formatSize(d.free_bytes)}</option>`).join("")}
          ${connu ? `<option value="${esc(a.destination)}" selected>${esc(a.destination)} (non détecté)</option>` : ""}
        </select>`;
    } else if (a.type === "envoyer_1fichier") {
      champ = `<select data-auto-field="apres" aria-label="Après l'envoi">
          <option value="garder"${a.apres !== "liberer" ? " selected" : ""}>Garder la copie locale</option>
          <option value="liberer"${a.apres === "liberer" ? " selected" : ""}>Libérer le serveur après l'envoi</option>
        </select>`;
    }

    const confirmation = autoRetiresSource(a)
      ? `<label class="auto-confirm"><input type="checkbox" data-auto-field="confirme"${a.confirme ? " checked" : ""}> Je confirme : le fichier sera retiré de sa source une fois l'action faite.</label>`
      : "";

    return `
      <li class="auto-action" data-auto-index="${i}">
        <select data-auto-field="type" aria-label="Type d'action">${options}</select>
        ${champ}
        <div class="auto-action-tools">
          <button type="button" class="btn btn-ghost small" data-auto-move="-1" aria-label="Monter cette action"${i === 0 ? " disabled" : ""}>↑</button>
          <button type="button" class="btn btn-ghost small" data-auto-move="1" aria-label="Descendre cette action"${i === actions.length - 1 ? " disabled" : ""}>↓</button>
          <button type="button" class="btn btn-ghost small" data-auto-remove aria-label="Retirer cette action"${actions.length === 1 ? " disabled" : ""}>×</button>
        </div>
        ${confirmation}
      </li>`;
  }).join("");
  $("auto-add-action").disabled = actions.length >= 6;
}

// Relit les lignes d'action affichées dans l'état (la source de vérité reste le formulaire).
function lireActionsDom() {
  state.auto.actions = [...$("auto-actions").querySelectorAll(".auto-action")].map((li) => ({
    type: li.querySelector('[data-auto-field="type"]').value,
    destination: li.querySelector('[data-auto-field="destination"]')?.value || "",
    apres: li.querySelector('[data-auto-field="apres"]')?.value || "garder",
    confirme: !!li.querySelector('[data-auto-field="confirme"]')?.checked,
  }));
}

function corpsDepuisFormulaire() {
  lireActionsDom();
  const note = $("auto-note").value.trim();
  return {
    nom: $("auto-name").value.trim(),
    actif: state.auto.editingActive !== false,
    declencheur: $("auto-trigger").value,
    note_min: note === "" ? null : Number(note),
    genre: $("auto-genre").value.trim(),
    inclure_deja_vus: $("auto-seen").checked,
    quand: $("auto-when").value,
    heure: $("auto-time").value || "01:00",
    actions: state.auto.actions.map((a) => {
      const out = { type: a.type };
      if (a.type === "copier_disque" || a.type === "deplacer_disque") out.destination = a.destination || "";
      if (a.type === "envoyer_1fichier") out.apres = a.apres || "garder";
      if (autoRetiresSource(out)) out.confirme = !!a.confirme;
      return out;
    }),
  };
}

function updateAutoPreview() {
  if ($("auto-editor").classList.contains("hidden")) return;
  const corps = corpsDepuisFormulaire();
  $("auto-time-wrap").classList.toggle("hidden", corps.quand !== "nuit");
  $("auto-preview").textContent = autoSentence(corps);
}

function openAutoEditor(rule = null) {
  if (!state.auto.catalogue) {
    toast("Les automatisations se chargent : réessaie dans un instant.", "info");
    return;
  }
  state.auto.editingId = rule ? rule.id : null;
  state.auto.editingActive = rule ? rule.actif : true;
  $("auto-editor-title").textContent = rule ? "Modifier l'automatisation" : "Nouvelle automatisation";
  $("auto-error").classList.add("hidden");
  fillAutoSelects();
  const r = rule || {
    nom: "", declencheur: "plex_vu", note_min: null, genre: "", inclure_deja_vus: false,
    quand: "nuit", heure: "01:00", actions: [{ type: "copier_disque", destination: "" }],
  };
  $("auto-name").value = r.nom;
  $("auto-trigger").value = r.declencheur;
  $("auto-note").value = r.note_min ?? "";
  $("auto-genre").value = r.genre || "";
  $("auto-seen").checked = !!r.inclure_deja_vus;
  $("auto-when").value = r.quand;
  $("auto-time").value = r.heure || "01:00";
  state.auto.actions = (r.actions || []).map((a) => ({ ...a }));
  renderAutoActions();
  $("auto-editor").classList.remove("hidden");
  updateAutoPreview();
  $("auto-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  $("auto-name").focus();
  refreshAutoDisks();
}

function closeAutoEditor() {
  state.auto.editingId = null;
  $("auto-editor").classList.add("hidden");
}

function showAutoError(message) {
  const box = $("auto-error");
  box.textContent = message;
  box.classList.remove("hidden");
}

function erreurFormulaire(corps) {
  if (!corps.nom) return "Donne un nom à l'automatisation.";
  if (!corps.actions.length) return "Ajoute au moins une action.";
  for (const a of corps.actions) {
    if ((a.type === "copier_disque" || a.type === "deplacer_disque") && !a.destination) {
      return "Choisis le disque de destination de chaque action de copie.";
    }
    if (autoRetiresSource(a) && !a.confirme) {
      return "Coche la confirmation pour l'action qui retire le fichier de sa source.";
    }
  }
  return null;
}

async function saveAutoRule(event) {
  event.preventDefault();
  if (state.auto.busy) return;
  const corps = corpsDepuisFormulaire();
  const erreur = erreurFormulaire(corps);
  if (erreur) {
    showAutoError(erreur);
    return;
  }
  state.auto.busy = true;
  $("auto-save").disabled = true;
  const id = state.auto.editingId;
  try {
    await api(id ? `/api/workflows/${encodeURIComponent(id)}` : "/api/workflows", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(corps),
    });
    toast(id ? "Automatisation modifiée." : "Automatisation créée.", "success");
    closeAutoEditor();
    await loadAuto();
  } catch (err) {
    showAutoError(err.message);
  } finally {
    state.auto.busy = false;
    $("auto-save").disabled = false;
  }
}

function addAutoAction() {
  lireActionsDom();
  if (state.auto.actions.length >= 6) return;
  state.auto.actions.push({ type: "copier_disque", destination: "" });
  renderAutoActions();
  updateAutoPreview();
}

function onAutoActionChange(event) {
  lireActionsDom();
  if (event.target.dataset.autoField === "type") {
    const i = Number(event.target.closest(".auto-action").dataset.autoIndex);
    state.auto.actions[i] = { type: event.target.value, destination: "", apres: "garder", confirme: false };
    renderAutoActions();
  }
  updateAutoPreview();
}

function onAutoActionClick(event) {
  const move = event.target.closest("[data-auto-move]");
  const remove = event.target.closest("[data-auto-remove]");
  if (!move && !remove) return;
  lireActionsDom();
  const i = Number(event.target.closest(".auto-action").dataset.autoIndex);
  if (move) {
    const j = i + Number(move.dataset.autoMove);
    if (j < 0 || j >= state.auto.actions.length) return;
    [state.auto.actions[i], state.auto.actions[j]] = [state.auto.actions[j], state.auto.actions[i]];
  } else if (state.auto.actions.length > 1) {
    state.auto.actions.splice(i, 1);
  }
  renderAutoActions();
  updateAutoPreview();
}

// ---------------------------------------------------------------------
// Actions sur les règles
// ---------------------------------------------------------------------

// Corps d'une règle pour l'API (sans l'état d'exécution), avec des changements facultatifs.
function corpsDeRegle(rule, changements = {}) {
  return {
    nom: rule.nom,
    actif: rule.actif,
    declencheur: rule.declencheur,
    note_min: rule.note_min,
    genre: rule.genre || "",
    inclure_deja_vus: !!rule.inclure_deja_vus,
    quand: rule.quand,
    heure: rule.heure,
    actions: rule.actions,
    ...changements,
  };
}

async function onAutoClick(event) {
  const bouton = event.target.closest("[data-auto-action]");
  if (!bouton) return;
  const action = bouton.dataset.autoAction;
  if (action === "new") {
    openAutoEditor(null);
    return;
  }
  const rule = state.auto.rules.find((r) => r.id === bouton.closest("[data-auto-id]")?.dataset.autoId);
  if (!rule) return;
  try {
    if (action === "edit") {
      openAutoEditor(rule);
    } else if (action === "run") {
      await api(`/api/workflows/${encodeURIComponent(rule.id)}/lancer`, { method: "POST" });
      toast("Lancé : le résultat apparaît dans les dernières exécutions.", "success");
      setTimeout(() => loadAuto({ silent: true }), 3000);
    } else if (action === "toggle") {
      await api(`/api/workflows/${encodeURIComponent(rule.id)}`, {
        method: "PUT", body: JSON.stringify(corpsDeRegle(rule, { actif: !rule.actif })),
      });
      await loadAuto();
    } else if (action === "delete") {
      const ok = await askConfirm({
        title: `Supprimer « ${rule.nom} » ?`,
        body: "Les films déjà traités ne sont pas touchés. Les films en attente sont abandonnés.",
      });
      if (!ok) return;
      await api(`/api/workflows/${encodeURIComponent(rule.id)}`, { method: "DELETE" });
      toast("Automatisation supprimée.", "success");
      await loadAuto();
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------------------------------------------------------------------
// Vue
// ---------------------------------------------------------------------

function startAutoPolling() {
  stopAutoPolling();
  state.auto.pollTimer = setInterval(() => {
    if (state.surface === "auto") loadAuto({ silent: true });
  }, AUTO_POLL_MS);
}

function stopAutoPolling() {
  clearInterval(state.auto.pollTimer);
  state.auto.pollTimer = null;
}

function openAutoView() {
  state.surface = "auto";
  document.querySelectorAll(".media-tab").forEach((b) => {
    const on = b.dataset.media === "automatisations";
    b.classList.toggle("is-active", on);
    b.setAttribute("aria-selected", String(on));
  });
  document.querySelector(".app-main").classList.add("of-mode");
  $("auto-view").classList.remove("hidden");
  state.selected.clear();
  refreshSelectionDom();
  updateSelectionUi();
  loadAuto();
  startAutoPolling();
}

function closeAutoView() {
  if (state.surface !== "auto") return;
  state.surface = "library";
  stopAutoPolling();
  closeAutoEditor();
  document.querySelector(".app-main").classList.remove("of-mode");
  $("auto-view").classList.add("hidden");
}

function bindAutomations() {
  $("auto-new-btn").addEventListener("click", () => openAutoEditor(null));
  $("auto-list").addEventListener("click", onAutoClick);
  $("auto-empty").addEventListener("click", onAutoClick);
  $("auto-form").addEventListener("submit", saveAutoRule);
  $("auto-form").addEventListener("input", updateAutoPreview);
  $("auto-form").addEventListener("change", updateAutoPreview);
  $("auto-cancel").addEventListener("click", closeAutoEditor);
  $("auto-add-action").addEventListener("click", addAutoAction);
  $("auto-actions").addEventListener("change", onAutoActionChange);
  $("auto-actions").addEventListener("click", onAutoActionClick);
}
