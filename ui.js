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
  "youtube waiting": "Waiting for YouTube recovery",
  "youtube searching": "Recovering on YouTube…",
  "youtube checking": "Checking YouTube match…",
  "youtube downloading": "Downloading from YouTube",
  "youtube validating": "Checking audio…",
  unavailable: "Unavailable",
  pending: "Waiting to search",
  searching: "Searching Soulseek…",
  matching: "Queueing on Soulseek…",
  "already/duplicate": "Already queued elsewhere",
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
  return [...groups].map(([key, g]) => renderHTML("transfer-group", {
    key: esc(key), name: esc(g.name), count: g.rows.length,
    unit: g.rows.length === 1 ? "file" : "files",
    rows: g.rows.map(t => renderHTML("transfer", {
      kind: t.kind === "completed" ? "completed" : "",
      name: esc(t.name), source: esc(t.username || "Source unavailable"),
      badge: badge(t.kind),
      progress: t.kind === "downloading" ? renderHTML("transfer-progress", {
        bar: t.size ? progress(t.percent, t.name) : "",
        bytes: bytes(t.done) + (t.size ? " / " + bytes(t.size) : "") + (t.speed ? " · " + bytes(t.speed) + "/s" : ""),
        percent: t.size ? Math.round(t.percent) + "%" : "File size not reported",
      }) : "",
      key: esc(key + ":" + (t.id || t.batchId + ":" + t.username + ":" + t.name)),
      sourceDetail: esc(t.username || "Unavailable"), folder: esc(t.folder),
      size: t.size ? renderHTML("definition", { label: "File size", value: bytes(t.size) }) : "",
      state: esc(t.state),
      error: t.error ? renderHTML("definition", { label: "Problem", value: esc(t.error) }) : "",
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
      : renderHTML("empty", { content: query || status
          ? "No downloads match your filters. Even the deep cuts came up empty."
          : snapshot.error ? "Downloads will appear when the connection is restored."
          : !receivedSnapshot ? "Loading downloads…" : renderHTML("empty-transfers") }),
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
                    "already/duplicate": "already queued",
                    "queued locally": "queued",
                    "queued remotely": "waiting for source",
                  }[k] || k),
              );
            const status = finished
              ? total && complete >= total
                ? "All songs downloaded"
                : complete
                  ? "Finished · partial success"
                  : "Finished · no songs downloaded"
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
            const trackRows = r.tracks.slice(0, limit).map((t, j) => renderHTML("track", {
              key: esc(key + ":row:" + (t.spotifyId || j)), number: j + 1,
              sourceIcon: sourceIcon(t.downloadSource), title: esc(t.title),
              displayTitle: esc(t.title || "Untitled track"), artist: esc(t.artist),
              reason: t.reason ? renderHTML("track-reason", { reason: esc(t.reason) }) : "",
              source: t.source ? renderHTML("track-source", {
                key: esc(key + ":track:" + j), source: esc(t.source),
                fallback: t.fallback ? " · Alternative source" : "",
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
                ? "Your music is ready"
                : "This import has finished"
              : active
                ? label(active.status)
                : r.phase === "unconfirmed"
                  ? "Submission could not be confirmed"
                  : ["submitted", "submitting", "resolving"].includes(r.phase)
                    ? "Reading your playlist"
                    : r.tracks.some((t) => t.status.includes("queued"))
                      ? "Waiting for a source"
                      : "Waiting for the next workflow update";
            const description = finished
              ? complete +
                " song" +
                (complete === 1 ? " is" : "s are") +
                " downloaded." +
                (unavailable
                  ? " " + unavailable + " unavailable after recovery."
                  : "") +
                (c["already/duplicate"]
                  ? " Some tracks were already queued elsewhere."
                  : "")
              : active
                ? [active.artist, active.title].filter(Boolean).join(" — ") +
                  (active.percent != null && /downloading/.test(active.status)
                    ? " · " + Math.round(active.percent) + "%"
                    : "")
                : r.phase === "unconfirmed"
                  ? "Check this import for updates before submitting again."
                  : ["submitted", "submitting", "resolving"].includes(r.phase)
                    ? "Track details appear as they arrive. Songs are searched one at a time."
                    : r.tracks.some((t) => t.status.includes("queued"))
                      ? "The source has not started sending these files. Available songs are already in your library. Peer pressure has its limits."
                      : "Progress appears when the download service reports a change.";
            return renderHTML("run", {
              key: esc(key), name: esc(r.name || "Spotify playlist"), when: esc(when),
              totalLabel: total != null ? " · " + total + " tracks" : "",
              kind: finished ? (complete ? "success" : "failed") : "active",
              status, handled, complete, unavailable,
              progress: total ? renderHTML("run-progress", {
                handled, total, bar: progress((handled / total) * 100, "Tracks handled", finished),
              }) : "",
              activity: esc(activity), description: esc(description),
              counts: counts.length && !finished ? renderHTML("run-counts", { counts: esc(counts.join(" · ")) }) : "",
              libraryLabel: complete ? "Get downloaded music" : "Open library",
              open: !finished && i === 0,
              trackCount: total != null ? total + " " : "",
              tracks: trackRows || renderHTML("empty-tracks"),
              more: r.tracks.length > limit ? renderHTML("more-tracks", { key: esc(key) }) : "",
              playlistId: encodeURIComponent(r.id),
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
    { files: "Library", transfers: "Downloads", start: "Import" }[view] +
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
    feedback.className = "error";
    feedback.textContent =
      "Use a Spotify playlist link or URI. Album and track links are not supported.";
    input.focus();
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
    feedback.textContent =
      "Playlist submitted. The crate digging has begun. Follow the tracks in your imports below.";
    input.value = "";
  } catch (error) {
    feedback.className = "error";
    feedback.textContent =
      error instanceof TypeError
        ? "Connection lost. Check recent imports before submitting again."
        : error.message;
  } finally {
    submit.disabled = false;
    submit.textContent = "Import playlist";
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
      document.getElementById("live-status").innerHTML = renderHTML("connection", {
        kind: snapshot.error ? "offline" : "", label: snapshot.error ? "Source offline" : "Connected",
      });
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
      renderHTML("connection", { kind: "offline", label: "Reconnecting…" });
  };
}
document.getElementById("reconnect").addEventListener("click", connect);
connect();
