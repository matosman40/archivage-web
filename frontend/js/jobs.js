"use strict";
// jobs.js : partie de l'interface. index.html la charge dans cet ordre : ne pas le réordonner.

// ---------------------------------------------------------------------
// Suivi des jobs (WebSocket)
// ---------------------------------------------------------------------

// Tâches affichées : identifiant -> entrée de suivi. Une tâche n'a qu'une carte et au plus une
// connexion : recharger la liste ne la duplique pas.
const trackedJobs = new Map();

function updateJobsIndicator() {
  const btn = $("jobs-indicator");
  btn.classList.toggle("hidden", state.runningJobs <= 0);
  $("jobs-count").textContent = String(state.runningJobs);
  btn.setAttribute("aria-label", `${state.runningJobs} opération(s) en cours`);
}

// Compte relu depuis l'état réel (tâches suivies et non terminées), jamais incrémenté à la main.
function recountRunningJobs() {
  state.runningJobs = [...trackedJobs.values()].filter((entry) => !entry.finished).length;
  updateJobsIndicator();
}

function shortText(text, max = 150) {
  const t = String(text || "").replace(/\s+/g, " ").trim();
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

// Ferme la connexion d'une tâche et ignore ce qui arrive encore. La carte reste à l'écran.
function stopJob(entry) {
  entry.closing = true;
  clearTimeout(entry.timer);
  const ws = entry.ws;
  entry.ws = null;
  if (ws) {
    ws.onclose = null;
    ws.onmessage = null;
    ws.close();
  }
}

// Retire une tâche de l'écran : carte, connexion, compte.
function forgetJob(jobId) {
  const entry = trackedJobs.get(jobId);
  if (!entry) return;
  stopJob(entry);
  entry.card.remove();
  trackedJobs.delete(jobId);
  recountRunningJobs();
}

// Verrouillage des médias adultes : toutes les cartes et connexions partent. loadRecentJobs relit
// ensuite la liste. Le client ne sait pas reconnaître une tâche adulte : c'est au serveur de ne plus
// donner ces tâches (dans /api/jobs et sur /ws/jobs) tant que la session est fermée.
function dropAllJobCards() {
  for (const jobId of [...trackedJobs.keys()]) forgetJob(jobId);
  document.querySelectorAll("#jobs-list .job-card, #disk-transfers-list .job-card").forEach((card) => card.remove());
  recountRunningJobs();
}

function trackJob(jobId, operation, { live = false, alreadyDone = false, container = null, priority = "normal" } = {}) {
  if (trackedJobs.has(jobId)) return;  // déjà affichée : pas de deuxième carte, pas de deuxième connexion
  const list = container || $("jobs-list");
  const emptyMsg = list.querySelector(".empty-msg");
  if (emptyMsg) emptyMsg.remove();

  const card = document.createElement("div");
  card.className = "job-card";
  card.innerHTML = `
    <div class="job-card-header">
      <span class="job-op">${esc(OP_LABEL[operation] || operation)}</span>
      <span class="job-status">en cours…</span>
    </div>
    <div class="job-overall">
      <div class="job-bar" role="progressbar" aria-label="Avancement de l'opération" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
        <span class="job-bar-fill" style="width:0%"></span>
      </div>
      <span class="job-pct">0 %</span>
    </div>
    <p class="job-stage muted small"></p>
    ${alreadyDone ? "" : `<div class="job-prio-wrap"><label class="job-prio muted small">Priorité
      <select class="job-priority" aria-label="Priorité de la tâche" data-previous="${esc(priority)}">${priorityOptions(priority)}</select></label></div>`}
    <div class="job-items"></div>`;
  list.prepend(card);

  const itemsBox = card.querySelector(".job-items");
  const itemRows = new Map();
  const percents = new Map();   // élément -> pourcentage (0 à 100)
  const bar = card.querySelector(".job-bar");
  const barFill = card.querySelector(".job-bar-fill");
  const pctLabel = card.querySelector(".job-pct");
  const stage = card.querySelector(".job-stage");
  const finished = new Set();   // éléments terminés (OK, erreur, partiel, ignoré)
  let total = 0;

  const entry = { card, ws: null, timer: null, retries: 0, skip: 0, handled: 0, finished: alreadyDone, closing: false };
  trackedJobs.set(jobId, entry);
  recountRunningJobs();

  // « en cours… · N à traiter » : le compte se met à jour à chaque élément terminé
  function updateStatus() {
    const left = Math.max(0, total - finished.size);
    card.querySelector(".job-status").textContent = left ? `en cours… · ${left} à traiter` : "en cours…";
  }
  function markFinished(name) {
    finished.add(name);
    updateStatus();
  }

  function paintOverall(value) {
    const p = Math.max(0, Math.min(100, Math.round(value)));
    barFill.style.width = `${p}%`;
    bar.setAttribute("aria-valuenow", String(p));
    pctLabel.textContent = `${p} %`;
  }
  function updateOverall() {
    const n = Math.max(total, percents.size, 1);
    let sum = 0;
    for (const v of percents.values()) sum += v;
    paintOverall(sum / n);
  }
  function setPercent(name, value) {
    percents.set(name, Math.max(0, Math.min(100, value)));
    updateOverall();
  }
  function rowFor(name) {
    if (itemRows.has(name)) return itemRows.get(name);
    const row = document.createElement("div");
    row.className = "job-item-line";
    row.innerHTML = `<span class="status-icon">·</span><span class="job-item-name">${esc(name)}</span><span class="progress-track"><span class="progress-fill" style="width:0%"></span></span>`;
    itemsBox.appendChild(row);
    itemRows.set(name, row);
    return row;
  }
  function setIcon(row, text, cls, title) {
    const icon = row.querySelector(".status-icon");
    icon.textContent = text;
    icon.className = `status-icon ${cls}`;
    if (title) row.title = title;
  }

  // Tâche terminée : plus de connexion, plus de bouton de priorité, compte à jour.
  function finishJob() {
    entry.finished = true;
    stopJob(entry);
    card.querySelector(".job-prio-wrap")?.remove();
    recountRunningJobs();
  }

  const prioSelect = card.querySelector(".job-priority");
  if (prioSelect) {
    prioSelect.addEventListener("change", () => changeJobPriority(jobId, prioSelect.value, prioSelect));
  }

  // Applique un événement à la carte. `notify` : toasts (événement reçu en direct). `refresh` : rafraîchit
  // les listes à la fin de la tâche, sans vider la sélection.
  function apply(event, { notify = false, refresh = false } = {}) {
    switch (event.type) {
      case "job_start":
        total = event.total_items || 0;
        // Liste d'attente : tous les éléments apparaissent dès le départ, en attente.
        (event.items || []).forEach((name) => {
          if (itemRows.has(name)) return;
          const row = rowFor(name);
          setIcon(row, "·", "", "En attente");
          row.style.opacity = "0.6";
        });
        updateOverall();
        updateStatus();
        break;
      case "item_start": {
        const row = rowFor(event.item);
        setIcon(row, "▸", "", "En cours");
        row.style.opacity = "";
        if (!percents.has(event.item)) setPercent(event.item, 0);
        stage.textContent = `Préparation : ${event.item}`;
        break;
      }
      case "item_log":
        if (event.line) stage.textContent = shortText(`${event.item} — ${event.line}`);
        break;
      case "item_progress":
        rowFor(event.item).querySelector(".progress-fill").style.width = `${event.percent}%`;
        setPercent(event.item, event.percent);
        break;
      case "item_done": {
        const row = rowFor(event.item);
        setIcon(row, "✓", "ok");
        row.querySelector(".progress-fill").style.width = "100%";
        setPercent(event.item, 100);
        markFinished(event.item);
        break;
      }
      case "item_error": {
        const row = rowFor(event.item);
        setIcon(row, "✕", "error", event.error);
        setPercent(event.item, 100);
        markFinished(event.item);
        if (notify) toast(`${event.item} : ${event.error}`, "error");
        break;
      }
      case "item_partial": {
        const row = rowFor(event.item);
        setIcon(row, "⚠", "warn", event.error);
        row.querySelector(".progress-fill").style.width = "100%";
        setPercent(event.item, 100);
        markFinished(event.item);
        if (notify) toast(`${event.item} : ${event.error}`, "error");
        break;
      }
      case "item_skipped": {
        const row = rowFor(event.item);
        setIcon(row, "–", "warn", event.message || "Ignoré");
        row.style.opacity = "0.8";
        setPercent(event.item, 100);
        markFinished(event.item);
        if (notify && event.reason !== "already_copied" && event.reason !== "missing") {
          toast(`${event.item} : ${event.message || "ignoré"}`, "info");
        }
        break;
      }
      case "job_queued":
        // En attente d'un emplacement (un seul envoi 1fichier, une seule copie disque à la fois).
        if (prioSelect) prioSelect.value = event.priority;
        stage.textContent = `En file (${event.label || RESOURCE_NAME[event.resource] || "transfert"}) : position ${event.position} `
          + `sur ${event.waiting} · priorité ${PRIORITY_NAME[event.priority] || event.priority}`;
        stage.title = stage.textContent;
        break;
      case "job_priority":
        if (prioSelect) prioSelect.value = event.priority;
        break;
      case "job_wait":
        // Hors plage de transfert : rien ne démarre avant la prochaine plage activée.
        stage.textContent = event.message || "En attente d'une plage de transfert.";
        stage.title = stage.textContent;
        break;
      case "job_resume":
        stage.textContent = "";
        break;
      case "job_error":
        stage.textContent = shortText(event.error);
        if (notify) toast(`Opération interrompue : ${event.error}`, "error");
        break;
      case "job_done": {
        const sum = event.summary || {};
        const parts = [];
        if (sum.ok) parts.push(`${sum.ok} OK`);
        if (sum.error) parts.push(`${sum.error} erreur(s)`);
        if (sum.partial) parts.push(`${sum.partial} partiel(s)`);
        if (sum.skipped) parts.push(`${sum.skipped} ignoré(s)`);
        card.querySelector(".job-status").textContent = parts.length ? `terminé — ${parts.join(", ")}` : "terminé";
        paintOverall(100);
        stage.textContent = "";
        if (notify) {
          if (sum.error || sum.partial) {
            toast(`Terminé avec ${sum.error || 0} erreur(s) et ${sum.partial || 0} partiel(s). Détails au survol des lignes.`, "error");
          } else if (sum.ok) {
            toast("Opération terminée.", "success");
          }
        }
        finishJob();
        if (refresh) {
          loadDisks();
          if (state.surface === "of") loadOneFichierView({ silent: true });
          if (state.surface === "disk") loadDiskLibrary({ force: true, silent: true });
          if (state.searched) runSearch($("search-term").value.trim(), { silent: true });
        }
        break;
      }
      default:
        break;
    }
  }

  // Lit l'état réel une fois et n'applique que les événements pas encore vus (sans toasts).
  async function syncFromServer({ refresh = false } = {}) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    const events = job.events || [];
    for (const event of events.slice(entry.handled)) apply(event, { refresh });
    entry.handled = events.length;
    return job.status;
  }

  // Flux en direct. Le serveur rejoue d'abord tous les événements de la tâche : ceux que la carte a déjà
  // (entry.skip) sont ignorés, pour ne rien compter deux fois.
  function connect() {
    if (entry.closing) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/jobs/${jobId}`);
    entry.ws = ws;
    entry.skip = entry.handled;
    ws.onopen = () => { entry.retries = 0; };
    ws.onmessage = (ev) => {
      if (entry.ws !== ws || entry.closing) return;
      const event = JSON.parse(ev.data);
      if (entry.skip > 0) {
        entry.skip -= 1;
        return;
      }
      entry.handled += 1;
      apply(event, { notify: live, refresh: true });
    };
    ws.onclose = (ev) => {
      if (entry.ws !== ws) return;
      entry.ws = null;
      if (entry.closing || entry.finished) return;
      // 4404 : tâche masquée (session adulte fermée) ; sinon coupure, on reprend.
      if (ev.code === 4404) {
        forgetJob(jobId);
        return;
      }
      scheduleResume();
    };
  }

  // Coupure : nouvelle tentative après 1 s, 2 s, 4 s, 8 s, puis 10 s au maximum.
  function scheduleResume() {
    if (entry.closing || entry.finished) return;
    const delay = Math.min(10000, 1000 * 2 ** entry.retries);
    entry.retries += 1;
    card.querySelector(".job-status").textContent = "connexion interrompue · nouvelle tentative…";
    clearTimeout(entry.timer);
    entry.timer = setTimeout(resume, delay);
  }

  // Relit l'état réel ; si la tâche est finie, on s'arrête là, sinon on rouvre le flux.
  async function resume() {
    if (entry.closing || entry.finished) return;
    try {
      const status = await syncFromServer({ refresh: true });
      if (entry.closing) return;
      if (status === "done") {
        finishJob();
        return;
      }
      connect();
    } catch (err) {
      if (err.message === "Job introuvable.") {
        forgetJob(jobId);
        return;
      }
      scheduleResume();
    }
  }

  if (alreadyDone) {
    // Tâche déjà terminée : son état final se lit une fois, sans connexion ni notification.
    syncFromServer()
      .then(() => finishJob())
      .catch((err) => {
        if (err.message === "Job introuvable.") forgetJob(jobId);
        else card.querySelector(".job-status").textContent = "état indisponible";
      });
  } else {
    connect();
  }
}

async function loadRecentJobs() {
  try {
    const data = await api("/api/jobs");
    const recent = [...data.jobs].reverse().slice(-8);
    for (const j of recent) {
      trackJob(j.id, j.operation, { alreadyDone: j.status === "done", priority: j.priority || "normal" });
    }
  } catch (_) { /* non bloquant */ }
}
