const uiTemplates = JSON.parse(document.getElementById("ui-templates").textContent);
function renderHTML(name, values = {}) {
  return uiTemplates[name].replace(/\$\{(\w+)\}/g, (_, key) => {
    if (!(key in values)) throw Error(`Missing ${key} in ${name}`);
    return String(values[key]);
  }).replace(/ open="(true|false)"/g, (_, value) => value === "true" ? " open" : "");
}
const sourceIcon = (source) => renderHTML(
  source === "youtube" ? "source-youtube" : source === "soulseek" ? "source-soulseek" : "source-empty",
);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const bytes = (n) => {
  let u = ["B", "KB", "MB", "GB", "TB"],
    i = 0;
  n = Number(n) || 0;
  while (n >= 1024 && i < 4) {
    n /= 1024;
    i++;
  }
  return (i ? n.toFixed(1) : Math.round(n)) + " " + u[i];
};
const labels = {
  "youtube waiting": "Wachten op zoeken via YouTube",
  "youtube searching": "Zoeken op YouTube…",
  "youtube checking": "YouTube-resultaat controleren…",
  "youtube downloading": "Downloaden via YouTube",
  "youtube validating": "Audio controleren…",
  unavailable: "Niet beschikbaar",
  pending: "Wachten op zoeken",
  searching: "Zoeken op Soulseek…",
  matching: "In wachtrij zetten op Soulseek…",
  "already/duplicate": "Staat al elders in de wachtrij",
  completed: "Klaar",
  downloading: "Bezig met downloaden",
  "queued locally": "In de wachtrij",
  "queued remotely": "Wachten op de bron",
  failed: "Download mislukt",
  unknown: "Wachten op status",
  "search error": "Zoeken mislukt",
  "search timeout": "Zoektijd verstreken",
  no_match: "Geen resultaat",
  "no match": "Geen resultaat",
  review_required: "Controle nodig",
  "needs review": "Controle nodig",
  "trying another source": "Andere bron proberen",
};
const label = (s) => labels[s] || "Aandacht nodig";
const badge = (s) => renderHTML("badge", {
  kind: s === "completed" ? "success" : s === "downloading" || s.startsWith("youtube ") ? "active" : /fail|error|timeout|no.?match|review|unavailable/.test(s) ? "failed" : "",
  label: esc(label(s)),
});
const progress = (value, name, complete = false) => renderHTML("progress", {
  kind: complete ? "complete" : "", name: esc(name),
  value: Math.round(value), width: Math.max(0, Math.min(100, value)),
});
const trackLimits = new Map();
let receivedSnapshot = false;
let snapshot = JSON.parse(document.getElementById("initial-runs").textContent),
  transferLimit = 100;
const transferList = document.getElementById("transfer-list"),
  runList = document.getElementById("run-list");
const controls = {
  search: document.getElementById("transfer-search"),
  status: document.getElementById("transfer-status"),
  sort: document.getElementById("transfer-sort"),
  reverse: document.getElementById("transfer-reverse"),
};
// bounded rows keep large queues cheap; add virtualization if browsing beyond 100 at a time becomes common.
function replaceList(node, markup) {
  if (node._markup === markup) return;
  const states = new Map(
    [...node.querySelectorAll("details[data-key]")].map((el) => [
      el.dataset.key,
      el.open,
    ]),
  );
  const focused = node.contains(document.activeElement)
    ? document.activeElement.closest("details[data-key]")?.dataset.key
    : null;
  const active = node.contains(document.activeElement)
    ? document.activeElement
    : null;
  const file = active?.closest("[data-path]")?.dataset.path;
  const href = active?.getAttribute("href");
  const run = active?.dataset.run;
  const focusKey = active?.closest("[data-key]")?.dataset.key;
  const activeTag = active?.tagName;
  const scroll = node.scrollTop;
  node.innerHTML = markup;
  node._markup = markup;
  for (const el of node.querySelectorAll("details[data-key]"))
    if (states.has(el.dataset.key)) el.open = states.get(el.dataset.key);
  if (focused)
    [...node.querySelectorAll("details[data-key]")]
      .find((el) => el.dataset.key === focused)
      ?.querySelector("summary")
      ?.focus({ preventScroll: true });
  if (file) {
    const row = [...node.querySelectorAll("[data-path]")].find(
      (el) => el.dataset.path === file,
    );
    [...(row?.querySelectorAll("a, button") || [])]
      .find(
        (el) =>
          el.tagName === active.tagName && el.getAttribute("href") === href,
      )
      ?.focus({ preventScroll: true });
  }
  if (active && !file) {
    const scope =
      [...node.querySelectorAll("[data-key]")].find(
        (el) => el.dataset.key === focusKey,
      ) || node;
    [...scope.querySelectorAll("a, button, summary")]
      .find(
        (el) =>
          el.tagName === activeTag &&
          el.getAttribute("href") === href &&
          el.dataset.run === run,
      )
      ?.focus({ preventScroll: true });
  }
  node.scrollTop = scroll;
}
function transferMarkup(rows, collapseOther = false) {
  const byBatch = new Map();
  for (const run of snapshot.runs)
    for (const t of run.tracks) if (t.batchId) byBatch.set(t.batchId, run);
  const groups = new Map();
  for (const t of rows) {
    const run = byBatch.get(t.batchId),
      key = run ? run.submissionId || run.submitted || run.id : "other";
    if (!groups.has(key))
      groups.set(key, { name: run?.name || "Overige downloads", rows: [] });
    groups.get(key).rows.push(t);
  }
  return [...groups].sort(([a], [b]) => (a === "other") - (b === "other")).map(([key, g]) => renderHTML("transfer-group", {
    key: esc(key), name: esc(g.name), count: g.rows.length,
    open: !collapseOther || key !== "other" || groups.size === 1,
    unit: g.rows.length === 1 ? "bestand" : "bestanden",
    rows: g.rows.map(t => renderHTML("transfer", {
      kind: t.kind === "completed" ? "completed" : "",
      name: esc(t.name), source: esc(t.username || "Bron niet beschikbaar"),
      badge: badge(t.kind),
      progress: t.kind === "downloading" ? renderHTML("transfer-progress", {
        bar: t.size ? progress(t.percent, t.name) : "",
        bytes: bytes(t.done) + (t.size ? " / " + bytes(t.size) : "") + (t.speed ? " · " + bytes(t.speed) + "/s" : ""),
        percent: t.size ? Math.round(t.percent) + "%" : "Bestandsgrootte niet opgegeven",
      }) : "",
      key: esc(key + ":" + (t.id || t.batchId + ":" + t.username + ":" + t.name)),
      sourceDetail: esc(t.username || "Niet beschikbaar"), folder: esc(t.folder),
      size: t.size ? renderHTML("definition", { label: "Bestandsgrootte", value: bytes(t.size) }) : "",
      state: esc(t.state),
      error: t.error ? renderHTML("definition", { label: "Probleem", value: esc(t.error) }) : "",
      failure: t.kind === "failed" ? renderHTML("transfer-failure") : "",
    })).join(""),
  })).join("");
}

function renderTransfers() {
  const query = controls.search.value.trim().toLowerCase(),
    status = controls.status.value,
    sort = controls.sort.value,
    reverse = controls.reverse.dataset.reverse === "true";
  let rows = snapshot.transfers.filter(
    (t) =>
      (!status ||
        (status === "queued"
          ? t.kind.includes("queued")
          : t.kind === status)) &&
      (!query ||
        (t.name + " " + t.username + " " + t.folder)
          .toLowerCase()
          .includes(query)),
  );
  const order = {
    downloading: 0,
    "queued locally": 1,
    "queued remotely": 2,
    failed: 3,
    unknown: 4,
    completed: 5,
  };
  const imported = new Set(snapshot.runs.flatMap(r => r.tracks.map(t => t.batchId).filter(Boolean)));
  rows.sort((a, b) => {
    const group = Number(imported.has(b.batchId)) - Number(imported.has(a.batchId));
    if (group) return group;
    let x =
      sort === "status"
        ? (order[a.kind] ?? 9) - (order[b.kind] ?? 9)
        : sort === "progress"
          ? a.percent - b.percent
          : sort === "speed"
            ? a.speed - b.speed
            : sort === "newest"
              ? String(b.date).localeCompare(String(a.date)) ||
                a.order - b.order
              : a.name.localeCompare(b.name);
    return (reverse ? -1 : 1) * (x || a.name.localeCompare(b.name));
  });
  const counts = [
    "downloading",
    "queued locally",
    "queued remotely",
    "failed",
    "completed",
  ]
    .map((k) => {
      const n = snapshot.transfers.filter((t) => t.kind === k).length;
      return n
        ? n +
            " " +
            {
              downloading: "bezig met downloaden",
              "queued locally": "in de wachtrij",
              "queued remotely": "wachten op de bron",
              failed: "mislukt",
              completed: "klaar",
            }[k]
        : "";
    })
    .filter(Boolean);
  document.getElementById("queue-summary").textContent =
    counts.join(" · ") ||
    (receivedSnapshot ? "Nog geen downloads." : "Verbinden…");
  document.getElementById("queue-notice").hidden = !snapshot.error;
  document.getElementById("queue-error").textContent = snapshot.error || "";
  replaceList(
    transferList,
    rows.length
      ? transferMarkup(rows.slice(0, transferLimit), true)
      : renderHTML("empty", { content: query || status
          ? "Geen downloads gevonden met deze filters."
          : snapshot.error ? "Downloads verschijnen zodra de verbinding is hersteld."
          : !receivedSnapshot ? "Downloads laden…" : renderHTML("empty-transfers") }),
  );
  const more = document.getElementById("more-transfers");
  more.hidden = rows.length <= transferLimit;
  more.textContent =
    "Meer tonen (nog " + Math.max(0, rows.length - transferLimit) + ")";
}
function renderRuns() {
  replaceList(
    runList,
    snapshot.runs.length
      ? snapshot.runs
          .map((r, i) => {
            const c = r.counts,
              total = r.total,
              complete = c.completed || 0,
              handled = r.handled ?? complete,
              finished = r.finished || r.phase === "final",
              key = r.submissionId || r.submitted || String(i);
            const counts = [
              "downloading",
              "queued locally",
              "queued remotely",
              "unavailable",
              "already/duplicate",
              "failed",
              "search timeout",
              "search error",
              "no match",
              "needs review",
            ]
              .filter((k) => c[k])
              .map(
                (k) =>
                  c[k] +
                  " " +
                  ({
                    "already/duplicate": "al in de wachtrij",
                    "queued locally": "in de wachtrij",
                    "queued remotely": "wachten op de bron",
                  }[k] || label(k)),
              );
            const status = finished
              ? total && complete >= total
                ? "Alle nummers gedownload"
                : complete
                  ? "Afgerond · deels gelukt"
                  : "Afgerond · geen nummers gedownload"
              : r.phase === "final"
                ? "Afgerond"
                : r.phase === "unconfirmed"
                  ? "Wachten op bevestiging"
                  : ["submitted", "submitting", "resolving"].includes(r.phase)
                    ? "Playlist uitlezen…"
                    : r.phase === "queued"
                      ? "In de wachtrij"
                      : "Bezig";
            const date = new Date(r.submitted),
              when = Number.isNaN(date.getTime())
                ? ""
                : date.toLocaleString("nl-NL", {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  });
            const limit = trackLimits.get(key) || 100;
            const trackRows = r.tracks.slice(0, limit).map((t, j) => renderHTML("track", {
              key: esc(key + ":row:" + (t.spotifyId || j)), number: j + 1,
              sourceIcon: sourceIcon(t.downloadSource), title: esc(t.title),
              displayTitle: esc(t.title || "Nummer zonder titel"), artist: esc(t.artist),
              reason: t.reason ? renderHTML("track-reason", { reason: esc(t.reason) }) : "",
              source: t.source ? renderHTML("track-source", {
                key: esc(key + ":track:" + j), source: esc(t.source),
                fallback: t.fallback ? " · Alternatieve bron" : "",
              }) : "",
              badge: badge(t.status),
              percent: (t.status === "downloading" || t.status === "youtube downloading") && t.percent != null
                ? renderHTML("track-percent", { percent: Math.round(t.percent) }) : "",
            })).join("");
            const active =
              r.tracks.find((t) => t.status.startsWith("youtube ")) ||
              r.tracks.find((t) =>
                ["downloading", "searching", "matching"].includes(t.status),
              );
            const unavailable = c.unavailable || 0;
            const activity = finished
              ? complete
                ? "Je muziek staat klaar"
                : "Deze import is afgerond"
              : active
                ? label(active.status)
                : r.phase === "unconfirmed"
                  ? "Import kon niet worden bevestigd"
                  : ["submitted", "submitting", "resolving"].includes(r.phase)
                    ? "Je playlist uitlezen"
                    : r.tracks.some((t) => t.status.includes("queued"))
                      ? "Wachten op een bron"
                      : "Wachten op de volgende workflow-update";
            const description = finished
              ? complete +
                (complete === 1 ? " nummer is" : " nummers zijn") +
                " gedownload." +
                (unavailable
                  ? " " + unavailable + " niet beschikbaar na opnieuw zoeken."
                  : "") +
                (c["already/duplicate"]
                  ? " Sommige nummers stonden al elders in de wachtrij."
                  : "")
              : active
                ? [active.artist, active.title].filter(Boolean).join(" — ") +
                  (active.percent != null && /downloading/.test(active.status)
                    ? " · " + Math.round(active.percent) + "%"
                    : "")
                : r.phase === "unconfirmed"
                  ? "Controleer deze import op updates voordat je hem opnieuw indient."
                  : ["submitted", "submitting", "resolving"].includes(r.phase)
                    ? "Nummergegevens verschijnen zodra ze binnen zijn. Nummers worden één voor één gezocht."
                    : r.tracks.some((t) => t.status.includes("queued"))
                      ? "De bron verstuurt deze bestanden nog niet. Beschikbare nummers staan al in je bibliotheek."
                      : "De voortgang verschijnt zodra de downloaddienst een wijziging doorgeeft.";
            return renderHTML("run", {
              key: esc(key), name: esc(r.name || "Spotify-playlist"), when: esc(when),
              totalLabel: total != null ? " · " + total + (total === 1 ? " nummer" : " nummers") : "",
              kind: finished ? (complete ? "success" : "failed") : "active",
              status, handled, complete, unavailable,
              progress: total ? renderHTML("run-progress", {
                handled, total, bar: progress((handled / total) * 100, "Nummers afgehandeld", finished),
              }) : "",
              activity: esc(activity), description: esc(description),
              counts: counts.length && !finished ? renderHTML("run-counts", { counts: esc(counts.join(" · ")) }) : "",
              libraryLabel: complete ? "Gedownloade muziek ophalen" : "Bibliotheek openen",
              open: !finished && i === 0,
              trackCount: total != null ? total : r.tracks.length,
              tracks: trackRows || renderHTML("empty-tracks"),
              more: r.tracks.length > limit ? renderHTML("more-tracks", { key: esc(key) }) : "",
              sourceUrl: esc(r.url || `https://open.spotify.com/${r.kind || "playlist"}/${encodeURIComponent(r.id)}`),
            });
          })
          .join("")
      : renderHTML("empty-imports"),
  );
  document.getElementById("clear-history").hidden = !snapshot.runs.length;
}
function route() {
  const hash = location.hash.slice(1),
    view =
      hash === "files" ? "files" : hash === "transfers" ? "transfers" : "start";
  for (const panel of document.querySelectorAll("[data-view]"))
    panel.hidden = panel.dataset.view !== view;
  for (const link of document.querySelectorAll(".nav a")) {
    if (link.hash === "#" + view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title =
    { files: "Bibliotheek", transfers: "Downloads", start: "Importeren" }[view] +
    " · The Disc Situation";
}
runList.addEventListener("click", (e) => {
  const button = e.target.closest(".more-tracks");
  if (!button) return;
  const key = button.dataset.run;
  trackLimits.set(key, (trackLimits.get(key) || 100) + 100);
  renderRuns();
  const details = [...runList.querySelectorAll("details[data-key]")].find(
    (el) => el.dataset.key === key,
  );
  details?.querySelector("summary")?.focus({ preventScroll: true });
});
window.addEventListener("hashchange", () => {
  route();
  const heading = document.querySelector("[data-view]:not([hidden]) h1");
  if (heading) {
    heading.setAttribute("tabindex", "-1");
    heading.focus({ preventScroll: true });
  }
  if (["#start", "#files", "#transfers"].includes(location.hash))
    window.scrollTo(0, 0);
});
route();
renderRuns();
renderTransfers();
for (const control of [controls.search, controls.status, controls.sort])
  control.addEventListener(
    control === controls.search ? "input" : "change",
    () => {
      transferLimit = 100;
      renderTransfers();
    },
  );
controls.reverse.addEventListener("click", () => {
  const reverse = controls.reverse.dataset.reverse !== "true";
  controls.reverse.dataset.reverse = String(reverse);
  controls.reverse.textContent = reverse ? "Volgorde herstellen" : "Volgorde omkeren";
  controls.reverse.setAttribute("aria-pressed", String(reverse));
  renderTransfers();
});
document.getElementById("more-transfers").addEventListener("click", () => {
  transferLimit += 100;
  renderTransfers();
});
const librarySearch = document.getElementById("library-search");
let libraryLimit = 100;
function renderLibrary() {
  const library = snapshot.library;
  if (library) {
    replaceList(document.getElementById("library-list"), library.html || "");
    document.getElementById("library-error").textContent = library.error || "";
  }
  let count = 0;
  for (const row of document.querySelectorAll(".library-row")) {
    row.hidden = !row
      .querySelector("strong")
      .textContent.toLowerCase()
      .includes(librarySearch.value.trim().toLowerCase());
    if (!row.hidden) count++;
  }
  document.getElementById("library-no-match").hidden =
    !!count || !librarySearch.value;
  const query = librarySearch.value.trim().toLowerCase();
  const rows = snapshot.transfers.filter(
    (t) => !query || (t.name + " " + t.username).toLowerCase().includes(query),
  );
  rows.sort(
    (a, b) =>
      (a.kind === "completed") - (b.kind === "completed") ||
      String(b.date).localeCompare(String(a.date)),
  );
  replaceList(
    document.getElementById("library-transfers"),
    transferMarkup(rows.slice(0, libraryLimit)),
  );
  document.getElementById("library-activity").hidden = !rows.length;
  const more = document.getElementById("more-library-transfers");
  more.hidden = rows.length <= libraryLimit;
  more.textContent =
    "Meer tonen (nog " + Math.max(0, rows.length - libraryLimit) + ")";
}
librarySearch.addEventListener("input", () => {
  libraryLimit = 100;
  renderLibrary();
});
document
  .getElementById("more-library-transfers")
  .addEventListener("click", () => {
    libraryLimit += 100;
    renderLibrary();
  });
renderLibrary();
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    document
      .querySelector("[data-view]:not([hidden]) input:not([type=hidden])")
      ?.focus();
  }
  if (e.key === "Escape" && e.target.matches("input[type=search]")) {
    e.target.value = "";
    e.target.dispatchEvent(new Event("input"));
  } else if (e.key === "Escape") {
    const detail = e.target.closest("details");
    if (detail) {
      detail.open = false;
      detail.querySelector("summary").focus();
    }
  }
});
const form = document.getElementById("import-form"),
  input = form.elements.url,
  feedback = document.getElementById("import-feedback"),
  submit = form.querySelector("button");
input.addEventListener("input", () => {
  input.setCustomValidity("");
  feedback.textContent = "";
  input.removeAttribute("aria-invalid");
});
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (submit.disabled) return;
  submit.disabled = true;
  submit.textContent = "Importeren…";
  form.setAttribute("aria-busy", "true");
  feedback.className = "hint";
  feedback.textContent = "Muzieklink versturen…";
  try {
    const response = await fetch("/submit", {
      method: "POST",
      body: new URLSearchParams(new FormData(form)),
    });
    if (!response.ok) {
      const doc = new DOMParser().parseFromString(
        await response.text(),
        "text/html",
      );
      throw Error(
        doc.querySelector(".error")?.textContent ||
          "De muzieklink kon niet worden geïmporteerd. Probeer het opnieuw.",
      );
    }
    feedback.textContent =
      "Import ingediend. Volg de voortgang hieronder bij je imports.";
    input.value = "";
  } catch (error) {
    feedback.className = "error";
    feedback.textContent =
      error instanceof TypeError
        ? "Verbinding verbroken. Controleer je recente imports voordat je de playlist opnieuw indient."
        : error.message;
  } finally {
    submit.disabled = false;
    submit.textContent = "Importeren";
    form.removeAttribute("aria-busy");
  }
});
let stream;
function connect() {
  document.getElementById("live-status").textContent = "Verbinden…";
  stream?.close();
  stream = new EventSource("/events" + (location.search || ""));
  stream.onmessage = (e) => {
    try {
      snapshot = JSON.parse(e.data);
      receivedSnapshot = true;
      document.getElementById("live-status").innerHTML = renderHTML("connection", {
        kind: snapshot.error ? "offline" : "", label: snapshot.error ? "Bron offline" : "Verbonden",
      });
      renderTransfers();
      renderRuns();
      renderLibrary();
    } catch {
      document.getElementById("live-status").textContent =
        "Update kon niet worden gelezen";
    }
  };
  stream.onerror = () => {
    document.getElementById("live-status").innerHTML =
      renderHTML("connection", { kind: "offline", label: "Opnieuw verbinden…" });
  };
}
document.getElementById("reconnect").addEventListener("click", connect);
connect();
