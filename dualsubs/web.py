"""`dualsubs web`: a small status page served from the Plex machine.

Standard library only. It listens on localhost and needs a secret key, because
on shared hosts (seedboxes) other users can reach localhost ports too. Open it
through an SSH tunnel:  ssh -L 8765:127.0.0.1:8765 plex-server '~/.local/bin/dualsubs web'
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, progress
from .languages import NAMES
from .pipeline import SETTLED, State, compatibility, pair_label, parse_pair, process_item
from .plex import Plex, PlexError

MAX_TITLE_ITEMS = 500  # a long-running show is fine; "all" is not a title

COOKIE = "dualsubs_key"


def ensure_key(cfg: dict, save) -> str:
    if not cfg.get("web_key"):
        cfg["web_key"] = secrets.token_urlsafe(18)
        save(cfg)
    return cfg["web_key"]


def overview(state: dict, pairs: list, libraries: list[dict]) -> dict:
    """Coverage per library and pair, from the state file plus Plex's library list."""
    keys = ["+".join(p) for p in pairs]
    libs: dict[str, dict] = {}
    for lib in libraries:
        libs[lib["id"]] = {"id": lib["id"], "title": lib["title"], "type": lib["type"], "total": lib.get("total"),
                           "checked": 0, "pairs": {}, "missing_langs": {}}

    def blank_pairs():
        return {k: {"done": 0, "missing": 0, "error": 0, "low_match": 0} for k in keys}

    for rk, entry in state.items():
        if rk.startswith("_"):
            continue
        info = entry.get("_info", {})
        lib_id = info.get("lib") or "?"
        lib = libs.get(lib_id)
        if lib is None:
            lib = libs[lib_id] = {"id": lib_id, "title": info.get("library") or "Not yet identified",
                                  "type": info.get("type", ""), "total": None, "checked": 0, "pairs": {},
                                  "missing_langs": {}}
        if not lib["pairs"]:
            lib["pairs"] = blank_pairs()
        lib["checked"] += 1
        for k in keys:
            rec = entry.get(k)
            if not rec:
                continue
            slot = lib["pairs"][k]
            if rec.get("stream_ids") or rec.get("sidecar"):
                slot["done"] += 1
            elif rec.get("status") == "missing":
                slot["missing"] += 1
                for lang in rec.get("missing", []):
                    lib["missing_langs"][lang] = lib["missing_langs"].get(lang, 0) + 1
            elif rec.get("status") == "low-match":
                slot["low_match"] += 1
            elif rec.get("status") in SETTLED:
                slot["error"] += 1
    for lib in libs.values():
        if not lib["pairs"]:
            lib["pairs"] = blank_pairs()
    order = [lib["id"] for lib in libraries]
    out = sorted(libs.values(), key=lambda lb: (order.index(lb["id"]) if lb["id"] in order else 99, lb["title"]))
    # Hide empty placeholder rows (libraries we know nothing about and Plex doesn't list).
    return {"libraries": [lb for lb in out if lb["checked"] or lb["total"]]}


class Portal:
    def __init__(self, config_dir: Path, load_config, save_config, plex_factory):
        self.config_dir = config_dir
        self.load_config, self.save_config = load_config, save_config
        self.plex_factory = plex_factory  # () -> (Plex, Settings) or raises
        self._libs: tuple[float, list[dict]] = (0.0, [])
        self._server: dict = {}
        self.job: dict | None = None
        self._job_lock = threading.Lock()

    # ------------------------------------------------------------------ data
    def libraries(self) -> list[dict]:
        if time.time() - self._libs[0] < 300 and self._libs[1]:
            return self._libs[1]
        try:
            plex, _ = self.plex_factory()
            info = plex.identity()
            self._server = {"name": info.get("name"), "version": info.get("version"), "ok": True}
            libs = plex.libraries()
            for lib in libs:
                try:
                    lib["total"] = plex.library_size(lib["id"], lib["type"])
                except PlexError:
                    lib["total"] = None
        except (PlexError, OSError, SystemExit) as e:
            self._server = {"ok": False, "error": str(e)}
            libs = self._libs[1]
        self._libs = (time.time(), libs)
        return libs

    def status(self) -> dict:
        cfg = self.load_config()
        try:
            state = json.loads((self.config_dir / "state.json").read_text())
        except (OSError, ValueError):
            state = {}
        pairs = []
        for p in _cfg_pairs(cfg):
            try:
                pairs.append(parse_pair(p))
            except ValueError:
                pass
        budget = state.get("_budget", {})
        data = overview(state, pairs, self.libraries())
        data.update({
            "version": __version__,
            "server": self._server,
            "pairs": [{"key": "+".join(p), "label": pair_label(p)} for p in pairs],
            "languages": sorted(({"code": c, "name": n} for c, n in NAMES.items()), key=lambda x: x["name"]),
            "progress": progress.read(self.config_dir / "progress.json"),
            "fetch": {"enabled": bool(cfg.get("fetch_missing")), "limit": cfg.get("fetch_limit", 50),
                      "today": budget.get("count", 0) if budget.get("date") == time.strftime("%Y-%m-%d") else 0},
            "now": time.time(),
        })
        return data

    def set_pairs(self, raw: list) -> list[str]:
        pairs = []
        for p in raw:
            pair = parse_pair(str(p))
            if pair not in pairs:
                pairs.append(pair)
        if not pairs:
            raise ValueError("Keep at least one language pair")
        for a, b in pairs:
            if (b, a) in pairs:
                raise ValueError(f"{pair_label((a, b))} and {pair_label((b, a))} show the same two languages, only "
                                 "swapped; keep one of them")
        cfg = self.load_config()
        cfg["pairs"] = ["+".join(p) for p in pairs]
        cfg.pop("primary", None)
        cfg.pop("secondary", None)
        self.save_config(cfg)
        return cfg["pairs"]

    # ------------------------------------------------------------------ search / check / repair
    def search(self, q: str) -> list[dict]:
        plex, _ = self.plex_factory()
        return [h for h in plex.search(q) if h["type"] in ("movie", "show")][:12] if q.strip() else []

    def _state(self) -> State:
        return State(self.config_dir / "state.json")

    def title(self, rating_key: str) -> dict:
        plex, settings = self.plex_factory()
        keys = plex.expand(rating_key)
        state = self._state()
        rows = []
        for k in keys[:MAX_TITLE_ITEMS]:
            try:
                rows.append(compatibility(plex.item(k), settings, state))
            except PlexError as e:
                rows.append({"ratingKey": k, "title": k, "error": str(e), "pairs": {}, "languages": [], "image_only": []})
        summary, downloads = {}, 0
        for r in rows:
            for key, x in r["pairs"].items():
                s = summary.setdefault(key, {})
                s[x["status"]] = s.get(x["status"], 0) + 1
                if x["status"] == "missing" and len(x["missing"]) == 1 and x["searched"].get(x["missing"][0]) != "none":
                    downloads += 1
        cfg = self.load_config()
        return {"ratingKey": rating_key, "items": rows, "truncated": len(keys) > MAX_TITLE_ITEMS,
                "pairs": [{"key": "+".join(p), "label": pair_label(p)} for p in settings.pairs],
                "summary": summary, "downloads_needed": downloads,
                "downloads_left": max(0, settings.fetch_limit - state.fetches_today()),
                "fetch_limit": cfg.get("fetch_limit", settings.fetch_limit)}

    def start_repair(self, rating_key: str, title: str, fetch: bool) -> dict:
        with self._job_lock:
            if self.job and self.job["state"] in ("running", "paused"):
                raise ValueError(f"Already working on {self.job['title']}; wait for it to finish")
            plex, settings = self.plex_factory()
            settings.fetch_missing = bool(fetch)
            keys = plex.expand(rating_key)[:MAX_TITLE_ITEMS]
            self.job = {"ratingKey": rating_key, "title": title or rating_key, "fetch": bool(fetch),
                        "state": "running", "done": 0, "total": len(keys), "counts": {}, "recent": [],
                        "started": time.time(), "paused_streams": 0}
            threading.Thread(target=self._run_job, args=(self.job, plex, settings, keys), daemon=True).start()
            return self.job

    def _run_job(self, job: dict, plex: Plex, settings, keys: list[str]) -> None:
        state = self._state()
        yield_streams = self.load_config().get("yield_to_streams", True)
        try:
            for k in keys:
                while yield_streams:  # streaming comes first, as in a normal run
                    try:
                        n = plex.playing_sessions()
                    except PlexError:
                        break
                    if not n:
                        break
                    job.update(state="paused", paused_streams=n)
                    time.sleep(60)
                job.update(state="running", paused_streams=0)
                for res in process_item(plex, k, settings, state):
                    job["counts"][res.status] = job["counts"].get(res.status, 0) + 1
                    if res.status not in ("up-to-date", "unchanged"):
                        job["recent"] = ([{"at": time.time(), "line": res.line()}] + job["recent"])[:30]
                job["done"] += 1
            job["state"] = "finished"
        except Exception as e:  # noqa: BLE001 - report, don't kill the server
            job.update(state="failed", error=str(e))
        finally:
            state.flush()

    def set_fetch(self, enabled: bool) -> None:
        cfg = self.load_config()
        cfg["fetch_missing"] = bool(enabled)
        self.save_config(cfg)


def _cfg_pairs(cfg: dict) -> list:
    if cfg.get("pairs"):
        return cfg["pairs"]
    return [f"{cfg.get('primary', 'nl')}+{cfg.get('secondary', 'en')}"]


def make_handler(portal: Portal, key: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "dualsubs"

        def log_message(self, *a):  # quiet
            pass

        def _authed(self) -> bool:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            given = cookie[COOKIE].value if COOKIE in cookie else ""
            return hmac.compare_digest(given, key)

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            try:
                self._get()
            except Exception as e:  # noqa: BLE001 - answer instead of dropping the connection
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _get(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if "key" in q and url.path == "/":  # first visit from the printed link: swap the key for a cookie
                if hmac.compare_digest(q["key"][0], key):
                    self._send(303, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"})
                else:
                    self._send(403, b"Wrong key", "text/plain")
                return
            if not self._authed():
                self._send(403, b"Open the link that `dualsubs web` printed (it contains the access key).",
                           "text/plain; charset=utf-8")
                return
            if url.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                self._json(200, portal.status())
            elif url.path == "/api/search":
                try:
                    self._json(200, {"results": portal.search(q.get("q", [""])[0])})
                except (PlexError, OSError) as e:
                    self._json(502, {"error": str(e)})
            elif url.path == "/api/title":
                try:
                    self._json(200, portal.title(q.get("rk", [""])[0]))
                except (PlexError, OSError) as e:
                    self._json(502, {"error": str(e)})
            elif url.path == "/api/job":
                self._json(200, {"job": portal.job})
            else:
                self._send(404, b"Not found", "text/plain")

        def do_POST(self):
            if not self._authed():
                self._json(403, {"error": "not signed in"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/pairs":
                    self._json(200, {"pairs": portal.set_pairs(body.get("pairs", []))})
                elif self.path == "/api/repair":
                    job = portal.start_repair(str(body.get("key", "")), str(body.get("title", "")),
                                              bool(body.get("fetch")))
                    self._json(200, {"job": job})
                elif self.path == "/api/fetch":
                    portal.set_fetch(body.get("enabled"))
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "not found"})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except (PlexError, OSError) as e:
                self._json(502, {"error": str(e)})

    return Handler


def serve(portal: Portal, key: str, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(portal, key))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>dualsubs</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e3e3e0;--accent:#c98a00;--ok:#2e7d32;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1e;--ink:#ececec;--mute:#9a9a9a;--line:#2e2e2e;--accent:#ffd54f;--ok:#81c784;--bad:#ef9a9a}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:980px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:22px;margin:0}h1 span{color:var(--accent)}h2{font-size:15px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
header{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:18px;gap:12px;flex-wrap:wrap}
.mute{color:var(--mute)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:8px 0}.bar>i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--mute);font-size:13px}td.n{text-align:right}th.n{text-align:right}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid var(--line);margin:0 6px 6px 0}
.pill button{border:0;background:none;color:var(--mute);cursor:pointer;font-size:15px;padding:0 0 0 6px}
select,button.act{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
button.act{cursor:pointer;background:var(--ink);color:var(--card);border-color:var(--ink)}
ul.recent{list-style:none;padding:0;margin:0;font-size:13px}ul.recent li{padding:4px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}
.ok{color:var(--ok)}.bad{color:var(--bad)}.small{font-size:13px}.warn{color:var(--accent)}
input[type=search]{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);width:min(420px,70%)}
.hits button{font:inherit;margin:6px 6px 0 0;padding:4px 10px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
.scroll{max-height:420px;overflow:auto;margin-top:10px}.chip{font-size:12px;border:1px solid var(--line);border-radius:6px;padding:0 5px;margin-right:3px;white-space:nowrap}
button.ghost{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
</style></head><body><main>
<header><h1>dual<span>subs</span></h1><div class="mute small" id="server"></div></header>
<div class="card" id="progress"><h2>Current run</h2><div class="mute">Loading...</div></div>
<div class="card"><h2>Check a show or movie</h2>
<form id="sf"><input type="search" id="q" placeholder="Title, e.g. Foundation"> <button class="act">Search</button></form>
<div class="hits" id="hits"></div><div id="title"></div></div>
<div class="card"><h2>Library coverage</h2><div id="coverage" class="mute">Loading...</div>
<p class="mute small">A bilingual track needs subtitles in <b>both</b> languages. "Missing" items lack one;
see which language below. "Not checked" items haven't been processed yet.</p></div>
<div class="card"><h2>Language pairs</h2><div id="pairs"></div>
<p><select id="p1"></select> on top, <select id="p2"></select> underneath <button class="act" id="add">Add pair</button></p>
<p class="mute small">Each pair becomes its own subtitle track. Changes apply from the next run: a run that's
already going keeps its pairs. Removing a pair here doesn't delete existing tracks from Plex
(<code>dualsubs remove all --pair xx+yy</code> does).</p>
<p><label><input type="checkbox" id="fetch"> Fetch missing subtitles from Plex's subtitle search</label>
<span class="mute small" id="fetchinfo"></span></p><p class="bad small" id="err"></p></div>
<div class="card"><h2>Recent results</h2><ul class="recent" id="recent"></ul></div>
<p class="mute small" id="foot"></p>
</main><script>
const $=id=>document.getElementById(id), el=(t,txt,cls)=>{const e=document.createElement(t);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e};
const fmt=n=>n==null?"?":n.toLocaleString(), ago=t=>{const s=Math.max(0,Date.now()/1000-t);return s<90?Math.round(s)+"s ago":s<5400?Math.round(s/60)+" min ago":Math.round(s/3600)+" h ago"};
let S=null;
async function post(url,body){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
 const j=await r.json();if(!r.ok){$("err").textContent=j.error||"Failed";return null}$("err").textContent="";return j}
function render(){
 const s=S;$("server").textContent=(s.server.ok?`${s.server.name} · Plex ${s.server.version}`:`Plex not reachable: ${s.server.error||""}`)+` · dualsubs ${s.version}`;
 // progress
 const P=$("progress");P.replaceChildren(el("h2","Current run"));const p=s.progress;
 if(!p){P.append(el("div","Nothing has run yet.","mute"))}else{
  const st={running:"Running",paused:"Paused for Plex playback",finished:"Finished",waiting:"Waiting for new items",stopped:"Stopped"}[p.state]||p.state;
  const head=el("div");head.append(el("b",st));head.append(el("span",` · ${p.command}${p.target?" "+p.target:""} · updated ${ago(p.updated)}`,"mute"));P.append(head);
  if(p.total){const pct=Math.round(100*p.done/p.total);const b=el("div",null,"bar"),i=el("i");i.style.width=pct+"%";b.append(i);P.append(b);
   P.append(el("div",`${fmt(p.done)} of ${fmt(p.total)} items (${pct}%)`+(p.current?` · ${p.current}`:""),"small"))}
  if(p.state==="paused")P.append(el("div",`${p.paused_streams} stream(s) playing; continues when they stop.`,"small mute"));
  const nice={"low-match":"poor timing","up-to-date":"already done"};const c=Object.entries(p.counts||{}).map(([k,v])=>`${fmt(v)} ${nice[k]||k}`).join(", ");if(c)P.append(el("div",c,"small mute"))}
 // coverage
 const C=$("coverage");C.replaceChildren();C.className="";
 if(!s.libraries.length){C.append(el("div","No libraries yet. Run dualsubs add or scan.","mute"))}else{
  const t=el("table"),h=el("tr");["Library","Items","Not checked"].forEach((x,i)=>h.append(el("th",x,i?"n":"")));
  s.pairs.forEach(pr=>h.append(el("th",pr.label,"n")));h.append(el("th","Missing most often"));t.append(h);
  s.libraries.forEach(lb=>{const r=el("tr");r.append(el("td",lb.title));r.append(el("td",fmt(lb.total),"n"));
   r.append(el("td",lb.total==null?"?":fmt(Math.max(0,lb.total-lb.checked)),"n"));
   s.pairs.forEach(pr=>{const x=lb.pairs[pr.key]||{done:0,missing:0};const td=el("td",null,"n");
    td.append(el("div",`${fmt(x.done)} ✓`,"ok"));const sub=[];if(x.missing)sub.push(`${fmt(x.missing)} missing`);
    if(x.low_match)sub.push(`${fmt(x.low_match)} poor timing`);if(x.error)sub.push(`${fmt(x.error)} errors`);
    if(sub.length)td.append(el("div",sub.join(" · "),"small mute"));r.append(td)});
   const ml=Object.entries(lb.missing_langs).sort((a,b)=>b[1]-a[1]).slice(0,3).map(([l,n])=>`${(s.languages.find(x=>x.code===l)||{name:l}).name} ${fmt(n)}`).join(", ");
   r.append(el("td",ml||"–","small mute"));t.append(r)});C.append(t)}
 // pairs
 const Pp=$("pairs");Pp.replaceChildren();s.pairs.forEach(pr=>{const pill=el("span",pr.label,"pill");const x=el("button","×");x.title="Remove "+pr.label;
  x.onclick=async()=>{if(s.pairs.length<2){$("err").textContent="Keep at least one pair.";return}if(await post("/api/pairs",{pairs:s.pairs.filter(q=>q.key!==pr.key).map(q=>q.key)}))load()};
  pill.append(x);Pp.append(pill)});
 $("fetch").checked=s.fetch.enabled;$("fetchinfo").textContent=` · ${s.fetch.today}/${s.fetch.limit} downloads today`;
 // recent
 const R=$("recent");R.replaceChildren();((p&&p.recent)||[]).forEach(x=>{const li=el("li");li.append(el("span",ago(x.at)+"  ","mute"));li.append(el("span",x.line));R.append(li)});
 if(!R.children.length)R.append(el("li","Nothing yet.","mute"));
 $("foot").textContent="Refreshes every 10 seconds.";
}
function fillSelects(){if($("p1").options.length)return;S.languages.forEach(l=>{["p1","p2"].forEach(id=>{const o=el("option",l.name);o.value=l.code;$(id).append(o)})});$("p1").value="en";$("p2").value="es"}
async function load(){try{const r=await fetch("/api/status");if(r.status===403){document.body.textContent="Access key missing: open the link printed by `dualsubs web`.";return}
 S=await r.json();fillSelects();render()}catch(e){$("foot").textContent="Can't reach dualsubs web (stopped, or the SSH tunnel closed)."}}
$("add").onclick=async()=>{const k=$("p1").value+"+"+$("p2").value;if(await post("/api/pairs",{pairs:S.pairs.map(q=>q.key).concat([k])}))load()};
$("fetch").onchange=async e=>{if(await post("/api/fetch",{enabled:e.target.checked}))load()};
// ---- check & repair
const ST={done:["✓ track","ok"],ready:["ready","warn"],missing:["missing","mute"],"low-match":["poor timing","bad"],error:["error","bad"]};
const langName=c=>(S&&S.languages.find(x=>x.code===c)||{name:c}).name;
$("sf").onsubmit=async e=>{e.preventDefault();const H=$("hits");H.replaceChildren(el("span","Searching...","mute small"));
 const r=await fetch("/api/search?q="+encodeURIComponent($("q").value));const j=await r.json();H.replaceChildren();
 if(!r.ok){H.append(el("span",j.error,"bad small"));return}if(!j.results.length){H.append(el("span","Nothing found.","mute small"));return}
 j.results.forEach(h=>{const b=el("button",`${h.title}${h.year?" ("+h.year+")":""} · ${h.type==="show"?"TV":"movie"}`);b.onclick=()=>openTitle(h.ratingKey,h.title);H.append(b)})};
let TITLE=null;
async function openTitle(key,title){TITLE={key,title};const T=$("title");T.replaceChildren(el("p",`Checking ${title}...`,"mute small"));
 const r=await fetch("/api/title?rk="+encodeURIComponent(key));const d=await r.json();if(!r.ok){T.replaceChildren(el("p",d.error,"bad"));return}
 T.replaceChildren();T.append(el("h3",title));
 const sum=el("p",null,"small");d.pairs.forEach(p=>{const s=d.summary[p.key]||{};const parts=Object.entries(s).map(([k,v])=>`${v} ${(ST[k]||[k])[0]}`);
  sum.append(el("b",p.label+": "));sum.append(el("span",parts.join(", ")+"   "))});T.append(sum);
 const ready=Object.values(d.summary).reduce((a,s)=>a+(s.ready||0),0);
 const acts=el("p");const mk=el("button",`Make tracks (${ready} ready)`,"ghost");mk.disabled=!ready;mk.onclick=()=>repair(false);acts.append(mk);acts.append(document.createTextNode(" "));
 const rp=el("button",`Repair: fetch missing subtitles + make tracks`,"act");rp.onclick=()=>repair(true);acts.append(rp);T.append(acts);
 T.append(el("p",`Repair can fetch about ${d.downloads_needed} subtitle file(s) where one language is missing (${d.downloads_left} of ${d.fetch_limit} downloads left today). Plex's subtitle search is asked for subtitles matching this exact release first; if timing still doesn't line up, that item is skipped rather than given bad subtitles.`,"mute small"));
 if(d.truncated)T.append(el("p","Showing the first 500 episodes.","mute small"));
 const box=el("div",null,"scroll"),tb=el("table"),h=el("tr");h.append(el("th",d.items.length>1?"Episode":"Title"));d.pairs.forEach(p=>h.append(el("th",p.label)));h.append(el("th","Subtitles"));tb.append(h);
 d.items.forEach(it=>{const tr=el("tr");tr.append(el("td",it.show&&it.title.startsWith(it.show)?it.title.slice(it.show.length+1):it.title));
  d.pairs.forEach(p=>{const x=it.pairs[p.key]||{};const[t,c]=ST[x.status]||[x.status||"?","mute"];const td=el("td",null,"small");td.append(el("span",t,c));
   if(x.status==="missing")td.append(el("div",x.missing.map(langName).join(" + ")+(Object.values(x.searched||{}).includes("none")?" (none found)":""),"mute"));tr.append(td)});
  const sub=el("td",null,"small");it.languages.forEach(l=>sub.append(el("span",l,"chip")));(it.image_only||[]).forEach(l=>sub.append(el("span",l+" (image)","chip mute")));
  if(it.error)sub.append(el("span",it.error,"bad"));tr.append(sub);tb.append(tr)});box.append(tb);T.append(box);T.append(el("div",null,"small")).id="job";pollJob()}
async function repair(fetchMissing){if(!TITLE)return;const j=await post("/api/repair",{key:TITLE.key,title:TITLE.title,fetch:fetchMissing});if(j)pollJob(true)}
let jobTimer=null;
async function pollJob(start){const r=await fetch("/api/job");const {job}=await r.json();const J=$("job");if(!J||!job)return;J.replaceChildren();
 const st={running:"Working",paused:"Paused for Plex playback",finished:"Done",failed:"Failed"}[job.state]||job.state;
 const line=el("p");line.append(el("b",`${st}: ${job.title}`));line.append(el("span",` · ${job.done}/${job.total} · `+Object.entries(job.counts).map(([k,v])=>`${v} ${k}`).join(", "),"mute"));J.append(line);
 if(job.error)J.append(el("p",job.error,"bad"));const ul=el("ul",null,"recent");job.recent.slice(0,8).forEach(x=>ul.append(el("li",x.line)));J.append(ul);
 clearTimeout(jobTimer);if(job.state==="running"||job.state==="paused")jobTimer=setTimeout(pollJob,2000);else if(!start&&TITLE&&job.ratingKey===TITLE.key&&job.state==="finished"&&!J.dataset.refreshed){J.dataset.refreshed=1}}
// Deep link: #check=<ratingKey> opens that title directly.
load().then(()=>{const m=location.hash.match(/^#check=(\d+)/);if(m)openTitle(m[1],decodeURIComponent((location.hash.split("&t=")[1]||m[1])))});
setInterval(load,10000);
</script></body></html>"""
