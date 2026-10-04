// Run with node test_ui.js. No browser or external services required.
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
function element() {
  return {
    value: "",
    textContent: "",
    innerHTML: "",
    dataset: {},
    hidden: false,
    scrollTop: 0,
    handlers: {},
    addEventListener(name, fn) {
      this.handlers[name] = fn;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return false;
    },
    setAttribute(name, value) {
      this[name] = value;
    },
    removeAttribute(name) {
      delete this[name];
    },
  };
}
const elements = new Map();
const get = (id) => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};
get("initial-runs").textContent = JSON.stringify({
  runs: [],
  transfers: [],
  error: null,
});
get("import-form").elements = { url: element() };
get("import-form").querySelector = () => get("submit");
get("transfer-sort").value = "status";
const panels = ["start", "transfers", "files"].map((view) => ({
  ...element(),
  dataset: { view },
}));
const links = panels.map((panel) => ({
  ...element(),
  hash: "#" + panel.dataset.view,
}));
const context = vm.createContext({
  URL,
  console,
  location: { hash: "#start" },
  window: { addEventListener() {} },
  document: {
    getElementById: get,
    querySelectorAll: (s) =>
      s === "[data-view]" ? panels : s === ".nav a" ? links : [],
    addEventListener() {},
  },
  EventSource: class {
    close() {}
  },
  Date,
  Map,
  Set,
});
vm.runInContext(fs.readFileSync("ui.js", "utf8"), context);
const run = (code) => vm.runInContext(code, context);
assert.equal(
  run(
    "validPlaylist('https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc')",
  ),
  true,
);
for (const url of [
  "https://evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M",
  "https://open.spotify.com/track/37i9dQZF1DXcBWIGoYBM5M",
  "not a URL",
])
  assert.equal(run(`validPlaylist(${JSON.stringify(url)})`), false);
assert.equal(
  run("validPlaylist('spotify:playlist:37i9dQZF1DXcBWIGoYBM5M')"),
  true,
);
assert.equal(panels[0].hidden, false);
assert.equal(panels[1].hidden, true);
run("location.hash='#files';route()");
assert.equal(panels[2].hidden, false);
assert.equal(links[2]["aria-current"], "page");
run(
  `snapshot={error:null,runs:[{id:'123',submissionId:'run',name:'<script>bad</script>',phase:'progress',total:2,counts:{completed:1,downloading:1},tracks:[{batchId:'batch',title:'Song <one>',artist:'Artist',status:'downloading',source:'Peer',percent:50}]}],transfers:[{id:'file',batchId:'batch',name:'Song <one>',username:'Peer',folder:'Music',kind:'downloading',state:'InProgress',percent:50,size:1000,done:500,speed:100,date:'',order:0}]};renderRuns();renderTransfers()`,
);
assert.ok(
  get("run-list").innerHTML.includes("&lt;script&gt;bad&lt;/script&gt;"),
);
assert.ok(get("run-list").innerHTML.includes("1 of 2 complete"));
assert.ok(get("transfer-list").innerHTML.includes('aria-valuenow="50"'));
assert.ok(get("transfer-list").innerHTML.includes("Song &lt;one&gt;"));
assert.ok(!get("transfer-list").innerHTML.includes(">InProgress</span>"));
get("transfer-search").value = "missing";
get("transfer-search").handlers.input();
assert.ok(get("transfer-list").innerHTML.includes("No downloads match"));
get("transfer-search").value = "";
run("snapshot.error='Source offline';renderTransfers()");
assert.equal(get("queue-notice").hidden, false);
run(
  "snapshot.error=null;snapshot.transfers=Array.from({length:150},(_,i)=>({...snapshot.transfers[0],id:String(i)}));renderTransfers()",
);
assert.equal(
  (get("transfer-list").innerHTML.match(/<article/g) || []).length,
  100,
);
assert.equal(get("more-transfers").hidden, false);
get("more-transfers").handlers.click();
assert.equal(
  (get("transfer-list").innerHTML.match(/<article/g) || []).length,
  150,
);
run("snapshot.library={html:'<div>Newly arrived file</div>',error:null};renderLibrary()");
assert.ok(get('library-list').innerHTML.includes('Newly arrived file'));
assert.ok(get('library-transfers').innerHTML.includes('aria-valuenow="50"'));
// Library is independent of Downloads filters and handles completion live.
get('transfer-search').value = 'does not match';
run("snapshot.transfers=snapshot.transfers.slice(0,1);snapshot.transfers[0].kind='completed';snapshot.transfers[0].percent=100;renderLibrary()");
assert.ok(get('library-transfers').innerHTML.includes('Complete'));
run("snapshot.library={html:null,error:'Folder removed'};renderLibrary()");
assert.equal(get('library-list').innerHTML, '');
assert.equal(get('library-error').textContent, 'Folder removed');
console.log("UI checks passed");

assert.ok(run("sourceIcon('youtube')").includes('aria-label="Downloaded from YouTube"'));
assert.ok(run("sourceIcon('soulseek')").includes('alt="Downloaded from Soulseek"'));
assert.ok(!run("sourceIcon('')").includes('<img'));
