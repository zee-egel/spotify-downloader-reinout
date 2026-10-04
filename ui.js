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
  pending: "Waiting to search",
  searching: "Searching…",
  matching: "Queueing…",
  "already/duplicate": "Already queued",
  completed: "Complete",
  downloading: "Downloading",
  "queued locally": "Queued",
  "queued remotely": "Waiting for source",
  failed: "Download failed",
  unknown: "Awaiting status",
  "search error": "Search failed",
  "search timeout": "Search timed out",
  no_match: "No match",
  "no match": "No match",
  review_required: "Needs review",
  "needs review": "Needs review",
  "trying another source": "Trying another source",
};
const label = (s) => labels[s] || "Needs attention";
const badge = (s) =>
  '<span class="badge ' +
  (s === "downloading"
    ? "active"
    : /fail|error|timeout|no.?match|review/.test(s)
      ? "failed"
      : "") +
  '">' +
  esc(label(s)) +
  "</span>";
const progress = (value, name, complete = false) =>
  '<div class="progress ' +
  (complete ? "complete" : "") +
  '" role="progressbar" aria-label="' +
  esc(name) +
  '" aria-valuenow="' +
  Math.round(value) +
  '" aria-valuemin="0" aria-valuemax="100"><span style="width:' +
  Math.max(0, Math.min(100, value)) +
  '%"></span></div>';
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
  node.scrollTop = scroll;
}
function transferMarkup(rows) {
  const byBatch = new Map();
  for (const run of snapshot.runs)
    for (const t of run.tracks) if (t.batchId) byBatch.set(t.batchId, run);
  const groups = new Map();
  for (const t of rows) {
    const run = byBatch.get(t.batchId),
      key = run ? run.submissionId || run.submitted || run.id : "other";
    if (!groups.has(key))
      groups.set(key, { name: run?.name || "Other downloads", rows: [] });
    groups.get(key).rows.push(t);
  }
  return [...groups]
    .map(
      ([key, g]) =>
        '<details class="transfer-group" data-key="' +
        esc(key) +
        '" open><summary><span>' +
        esc(g.name) +
        '</span><span class="count">' +
        g.rows.length +
        " files</span></summary>" +
        g.rows
          .map((t) => {
            const detailKey =
              key + ":" + (t.id || t.batchId + ":" + t.username + ":" + t.name);
            return (
              '<article class="row transfer ' +
              (t.kind === "completed" ? "completed" : "") +
              '"><div class="transfer-top"><div><strong title="' +
              esc(t.name) +
              '">' +
              esc(t.name) +
              '</strong><div class="muted">' +
              esc(t.username || "Source unavailable") +
              "</div></div>" +
              badge(t.kind) +
              "</div>" +
              (t.kind === "downloading"
                ? progress(t.percent, t.name) +
                  '<div class="transfer-bottom"><span>' +
                  bytes(t.done) +
                  (t.size ? " / " + bytes(t.size) : "") +
                  (t.speed ? " · " + bytes(t.speed) + "/s" : "") +
                  "</span><span>" +
                  Math.round(t.percent) +
                  "%</span></div>"
                : "") +
              '<details class="track-details" data-key="' +
              esc(detailKey) +
              '"><summary>File details</summary><dl><dt>Source</dt><dd>' +
              esc(t.username || "Unavailable") +
              "</dd><dt>Folder</dt><dd>" +
              esc(t.folder) +
              "</dd>" +
              (t.size
                ? "<dt>File size</dt><dd>" + bytes(t.size) + "</dd>"
                : "") +
              "<dt>Status</dt><dd>" +
              esc(t.state) +
              "</dd>" +
              (t.error ? "<dt>Problem</dt><dd>" + esc(t.error) + "</dd>" : "") +
              "</dl></details>" +
              (t.kind === "failed"
                ? '<div class="error">The download stopped. Check the source in file details.</div>'
                : "") +
              "</article>"
            );
          })
          .join("") +
        "</details>",
    )
    .join("");
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
  rows.sort((a, b) => {
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
              downloading: "downloading",
              "queued locally": "queued",
              "queued remotely": "waiting for source",
              failed: "failed",
              completed: "complete",
            }[k]
        : "";
    })
    .filter(Boolean);
  document.getElementById("queue-summary").textContent =
    counts.join(" · ") ||
    (receivedSnapshot ? "No downloads yet." : "Connecting…");
  document.getElementById("queue-notice").hidden = !snapshot.error;
  document.getElementById("queue-error").textContent = snapshot.error || "";
  replaceList(
    transferList,
    rows.length
      ? transferMarkup(rows.slice(0, transferLimit))
      : '<div class="empty">' +
          (query || status
            ? "No downloads match your filters."
            : snapshot.error
              ? "Downloads will appear when the connection is restored."
              : !receivedSnapshot
                ? "Loading downloads…"
                : 'No downloads yet. <a class="link" href="#start">Import a playlist</a>') +
          "</div>",
  );
  const more = document.getElementById("more-transfers");
  more.hidden = rows.length <= transferLimit;
  more.textContent =
    "Show more (" + Math.max(0, rows.length - transferLimit) + " remaining)";
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
              key = r.submissionId || r.submitted || String(i);
            const counts = [
              "downloading",
              "queued locally",
              "queued remotely",
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
                    "queued locally": "queued",
                    "queued remotely": "waiting for source",
                  }[k] || k),
              );
            const status =
              total && complete >= total
                ? "Complete"
                : r.phase === "final"
                  ? "Finished"
                  : r.phase === "unconfirmed"
                    ? "Awaiting confirmation"
                    : ["submitted", "submitting", "resolving"].includes(r.phase)
                      ? "Resolving…"
                      : r.phase === "queued"
                        ? "Queued"
                        : "In progress";
            const date = new Date(r.submitted),
              when = Number.isNaN(date.getTime())
                ? ""
                : date.toLocaleString(undefined, {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  });
            const limit = trackLimits.get(key) || 100;
            const trackRows = r.tracks
              .slice(0, limit)
              .map(
                (t, j) =>
                  '<div class="track-row"><span class="track-number">' +
                  (j + 1) +
                  '</span><div class="track-info"><span class="track-title" title="' +
                  esc(t.title) +
                  '">' +
                  esc(t.title || "Untitled track") +
                  '</span><div class="muted">' +
                  esc(t.artist) +
                  "</div>" +
                  (t.source
                    ? '<details class="track-details" data-key="' +
                      esc(key + ":track:" + j) +
                      '"><summary>Source details</summary><div>' +
                      esc(t.source) +
                      (t.fallback ? " · Alternative source" : "") +
                      "</div></details>"
                    : "") +
                  "</div>" +
                  badge(t.status) +
                  (t.status === "downloading"
                    ? '<span class="count">' +
                      Math.round(t.percent) +
                      "%</span>"
                    : "") +
                  "</div>",
              )
              .join("");
            return (
              '<article class="run"><div class="run-header"><span class="artwork" aria-hidden="true">♫</span><div class="run-info"><h3 class="run-title" title="' +
              esc(r.name) +
              '">' +
              esc(r.name || "Spotify playlist") +
              '</h3><div class="muted">' +
              esc(when) +
              (total != null ? " · " + total + " tracks" : "") +
              '</div></div><span class="badge ' +
              (status === "In progress" || status === "Resolving…"
                ? "active"
                : "") +
              '">' +
              status +
              "</span></div>" +
              (total
                ? progress(
                    (complete / total) * 100,
                    "Completed tracks",
                    complete >= total,
                  )
                : "") +
              '<div class="run-details"><div class="run-counts">' +
              (total != null
                ? complete + " of " + total + " complete"
                : "Waiting for track information") +
              (counts.length ? " · " + esc(counts.join(" · ")) : "") +
              '</div><details data-key="' +
              esc(key) +
              '"><summary>View tracks</summary><div class="track-list">' +
              (trackRows ||
                '<p class="muted">Track details have not arrived yet.</p>') +
              "</div>" +
              (r.tracks.length > limit
                ? '<button class="more-tracks" data-run="' +
                  esc(key) +
                  '" type="button">Show more tracks</button>'
                : "") +
              '<a class="link" href="https://open.spotify.com/playlist/' +
              encodeURIComponent(r.id) +
              '" target="_blank" rel="noopener noreferrer">Open in Spotify ↗</a></details></div></article>'
            );
          })
          .join("")
      : '<div class="empty">No playlists imported yet.</div>',
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
    { files: "Library", transfers: "Downloads", start: "Import" }[view] +
    " · Playlist desk";
}
for (const link of document.querySelectorAll(".nav a"))
  link.addEventListener("click", (e) => {
    if (!link.hash) return;
    e.preventDefault();
    location.hash = link.hash;
  });
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
window.addEventListener("hashchange", route);
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
  controls.reverse.textContent = reverse ? "Reversed" : "Reverse order";
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
    "Show more (" + Math.max(0, rows.length - libraryLimit) + " remaining)";
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
function validPlaylist(value) {
  if (/^spotify:playlist:[a-zA-Z0-9]{22}$/.test(value)) return true;
  try {
    const u = new URL(value);
    return (
      u.protocol === "https:" &&
      ["open.spotify.com", "play.spotify.com"].includes(u.hostname) &&
      /^\/playlist\/[a-zA-Z0-9]{22}\/?$/.test(u.pathname)
    );
  } catch {
    return false;
  }
}
input.addEventListener("input", () => {
  input.setCustomValidity("");
  feedback.textContent = "";
  input.removeAttribute("aria-invalid");
});
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (submit.disabled) return;
  if (!validPlaylist(input.value.trim())) {
    input.setCustomValidity(
      "Paste a Spotify playlist link. Album and track links are not supported.",
    );
    input.reportValidity();
    input.setAttribute("aria-invalid", "true");
    return;
  }
  submit.disabled = true;
  submit.textContent = "Importing…";
  form.setAttribute("aria-busy", "true");
  feedback.className = "hint";
  feedback.textContent = "Sending playlist…";
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
          "The playlist could not be imported. Try again.",
      );
    }
    feedback.textContent = "Playlist submitted.";
    input.value = "";
  } catch (error) {
    feedback.className = "error";
    feedback.textContent =
      error instanceof TypeError
        ? "Connection lost. Check recent imports before submitting again."
        : error.message;
  } finally {
    submit.disabled = false;
    submit.textContent = "Import";
    form.removeAttribute("aria-busy");
  }
});
let stream;
function connect() {
  document.getElementById("live-status").textContent = "Connecting…";
  stream?.close();
  stream = new EventSource("/events" + (location.search || ""));
  stream.onmessage = (e) => {
    try {
      snapshot = JSON.parse(e.data);
      receivedSnapshot = true;
      document.getElementById("live-status").innerHTML =
        '<span class="live-dot ' +
        (snapshot.error ? "offline" : "") +
        '"></span>' +
        (snapshot.error ? "Source offline" : "Connected");
      renderTransfers();
      renderRuns();
      renderLibrary();
    } catch {
      document.getElementById("live-status").textContent =
        "Could not read update";
    }
  };
  stream.onerror = () => {
    document.getElementById("live-status").innerHTML =
      '<span class="live-dot offline"></span>Reconnecting…';
  };
}
document.getElementById("reconnect").addEventListener("click", connect);
connect();
