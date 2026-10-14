// Scénarios navigateur de bout en bout (version 50). Lancés par lancer.sh, qui fournit :
//   E2E_URL        adresse du serveur de test
//   E2E_LOG        journal du faux Radarr
//   CHROME_PATH    exécutable Chromium
//   PLAYWRIGHT_MODULE (facultatif) fichier d'entrée du paquet playwright (ex. .../node_modules/playwright/index.mjs)
import { pathToFileURL } from "node:url";
const pw = await import(process.env.PLAYWRIGHT_MODULE ? pathToFileURL(process.env.PLAYWRIGHT_MODULE).href : "playwright");
const { chromium } = pw.default ?? pw;
import { readFileSync, existsSync } from "node:fs";

const URL = process.env.E2E_URL;
const LOG = process.env.E2E_LOG;
const echecs = [];
const ok = (nom, condition, detail = "") => {
  console.log((condition ? "OK    " : "ECHEC ") + nom + (detail ? " — " + detail : ""));
  if (!condition) echecs.push(nom);
};
const journal = () => (existsSync(LOG) ? readFileSync(LOG, "utf8") : "");

const navigateur = await chromium.launch({ executablePath: process.env.CHROME_PATH, args: ["--no-sandbox"] });
const page = await navigateur.newPage({ viewport: { width: 1360, height: 900 } });
const erreursJs = [];
page.on("pageerror", (e) => erreursJs.push(e.message));

// --- Disques : suppression avec retrait Radarr, puis décochée -----------------------------------------
{
  const corps = [];
  page.on("request", (r) => { if (r.url().endsWith("/api/disk-library/delete")) corps.push(r.postData()); });
  await page.goto(URL, { waitUntil: "networkidle" });
  await page.click('button[data-media="disques"]');
  await page.waitForSelector(".disk-card", { timeout: 15000 });
  await page.waitForTimeout(800);

  await page.check('input[aria-label^="Sélectionner Delta"]');
  await page.click("#disk-delete");
  await page.waitForSelector("#confirm-modal:not(.hidden)");
  ok("disque : ligne Radarr visible", await page.isVisible("#confirm-arr-row"));
  ok("disque : case cochée par défaut", await page.isChecked("#confirm-arr"));
  ok("disque : texte cite Radarr", /Retirer aussi la fiche de Radarr/.test(await page.innerText("#confirm-arr-text")));
  await page.fill("#confirm-delete-input", "SUPPRIMER");
  await page.click("#confirm-ok");
  await page.waitForSelector("#confirm-modal", { state: "hidden" });
  await page.waitForTimeout(2500);
  ok("disque : requête retirer_arr=true", corps.length > 0 && JSON.parse(corps[0]).retirer_arr === true);
  ok("disque : Radarr reçoit DELETE sans fichiers",
    /DELETE[^\n]*deleteFiles=false&addImportListExclusion=true/.test(journal()));

  await page.waitForTimeout(1500);
  await page.check('input[aria-label^="Sélectionner Charlie"]');
  await page.click("#disk-delete");
  await page.waitForSelector("#confirm-modal:not(.hidden)");
  await page.uncheck("#confirm-arr");
  await page.fill("#confirm-delete-input", "SUPPRIMER");
  await page.click("#confirm-ok");
  await page.waitForSelector("#confirm-modal", { state: "hidden" });
  await page.waitForTimeout(2500);
  ok("disque : requête retirer_arr=false quand décochée", corps.length >= 2 && JSON.parse(corps[1]).retirer_arr === false);
  ok("disque : aucun DELETE pour Charlie (movie/22)", !/movie\/22/.test(journal()));
  page.removeAllListeners("request");
}

// --- Catalogue : même option, avec la fiche d'Alpha ---------------------------------------------------
{
  const corps = [];
  page.on("request", (r) => { if (r.url().endsWith("/api/jobs") && r.method() === "POST") corps.push(r.postData()); });
  await page.goto(URL, { waitUntil: "networkidle" });
  await page.waitForSelector('input[aria-label^="Sélectionner Alpha"]', { timeout: 15000 });
  await page.check('input[aria-label^="Sélectionner Alpha"]');
  await page.click("#action-delete");
  await page.waitForSelector("#confirm-modal:not(.hidden)");
  ok("catalogue : ligne Radarr visible", await page.isVisible("#confirm-arr-row"));
  await page.fill("#confirm-delete-input", "SUPPRIMER");
  await page.click("#confirm-ok");
  await page.waitForSelector("#confirm-modal", { state: "hidden" });
  await page.waitForTimeout(3000);
  ok("catalogue : requête /api/jobs retirer_arr=true", corps.length > 0 && JSON.parse(corps[0]).retirer_arr === true);
  ok("catalogue : Radarr reçoit DELETE movie/23 sans fichiers",
    /DELETE[^\n]*movie\/23\?deleteFiles=false&addImportListExclusion=true/.test(journal()));
  page.removeAllListeners("request");
}

// --- 1fichier : marques, filtre, compteur, renvoi (annulé) --------------------------------------------
{
  const renvois = [];
  page.on("request", (r) => { if (r.url().includes("/resend") && r.method() === "POST") renvois.push(r.url()); });
  await page.goto(URL, { waitUntil: "networkidle" });
  await page.click('button[data-media="1fichier"]');
  await page.waitForSelector("#of-verify", { state: "visible", timeout: 10000 });
  await page.waitForSelector("#of-sub", { state: "attached" });
  await page.waitForTimeout(1200);

  ok("1fichier : bouton « Vérifier les fichiers » visible", await page.isVisible("#of-verify"));
  const texte = await page.locator("body").innerText();
  ok("1fichier : marque « Absent de 1fichier »", /Absent de 1fichier/.test(texte));
  ok("1fichier : marque « Corrompu »", /Corrompu/.test(texte));
  const sub = await page.innerText("#of-sub");
  ok("1fichier : compteur des absents et corrompus", /1 absent\(s\), 1 corrompu\(s\)/.test(sub), sub.slice(0, 160));

  await page.selectOption("#of-status", "problemes");
  await page.waitForTimeout(800);
  const cartes = await page.locator(".of-card .of-title").allInnerTexts();
  ok("1fichier : filtre « Absents ou corrompus » ne garde que 102 et 103",
    JSON.stringify(cartes) === JSON.stringify(["102", "103"]), JSON.stringify(cartes));

  await page.click('.of-card[data-of-open="102"]');
  await page.waitForSelector("[data-of-resend]", { timeout: 5000 });
  ok("1fichier : renvoi proposé pour l'archive absente", await page.isVisible("[data-of-resend]"));
  await page.click("[data-of-resend]");
  await page.waitForSelector("#confirm-modal:not(.hidden)");
  await page.click("#confirm-cancel");
  await page.waitForSelector("#confirm-modal", { state: "hidden" });
  await page.waitForTimeout(500);
  ok("1fichier : annuler le renvoi n'envoie rien", renvois.length === 0);
  page.removeAllListeners("request");
}

ok("aucune erreur JavaScript", erreursJs.length === 0, erreursJs.join(" | "));
await navigateur.close();
if (echecs.length) {
  console.log(`${echecs.length} scénario(s) en échec : ${echecs.join(", ")}`);
  process.exit(1);
}
console.log("Tous les scénarios sont passés.");
