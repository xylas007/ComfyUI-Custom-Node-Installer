"""
Core logic for ComfyUI-Custom-Node-Installer.

- parse any git link (https / ssh / user/repo / github tree urls / raw .py files)
- detect what's already installed (incl. ComfyUI-Manager's disabled nodes)
- git clone / update + requirements.txt + install.py (same steps Manager does)
- resolve "missing node types" of a workflow to repos using workflow metadata,
  the Comfy Registry and ComfyUI-Manager's public node database
"""
import base64
import copy
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

import requests

try:
    import folder_paths  # provided by ComfyUI
except Exception:  # allows testing outside ComfyUI
    folder_paths = None

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
DB_URLS = [
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json",
    "https://raw.githubusercontent.com/Comfy-Org/ComfyUI-Manager/main/extension-node-map.json",
]
DB_TTL = 12 * 3600
REGISTRY_API = "https://api.comfy.org/nodes/"
PROTECTED_PKGS = {"torch", "torchvision", "torchaudio", "xformers", "triton"}
UA = "ComfyUI-Custom-Node-Installer"


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------
# paths / url helpers
# --------------------------------------------------------------------------
def cn_dir():
    try:
        return os.path.abspath(folder_paths.get_folder_paths("custom_nodes")[0])
    except Exception:
        return os.path.abspath(folder_paths.folder_names_and_paths["custom_nodes"][0][0])


def _norm_name(s):
    return re.sub(r"[\s_]+", "-", (s or "").strip().lower())


def norm_url(u):
    u = (u or "").strip()
    m = re.match(r"^[\w.-]+@([^:]+):(.+)$", u)
    if m:
        host, path = m.groups()
    else:
        pu = urlparse(u)
        host, path = (pu.hostname or "").lower(), pu.path
        if not host:
            return u.lower().rstrip("/")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}".lower()


def parse_repo(s):
    """-> {kind: git|file, clone, name, norm, ref, host}"""
    s = (s or "").strip()
    if not s:
        raise ValueError("Empty link")
    ref = None

    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", s)
    if m:
        host, path = m.groups()
        return {"kind": "git", "clone": f"git@{host}:{path}.git", "name": path.split("/")[-1],
                "norm": norm_url(s), "ref": None, "host": host.lower()}

    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        if re.match(r"^[\w.-]+/[\w.-]+$", s):
            s = "https://github.com/" + s  # "user/repo" shorthand
        else:
            raise ValueError(f"'{s}' doesn't look like a git link")

    u = urlparse(s)
    if u.scheme.lower() not in ("http", "https", "git", "file"):
        raise ValueError(f"Unsupported link type '{u.scheme}'")
    host = (u.hostname or "").lower()
    path = u.path.strip("/")

    # single-file nodes (e.g. gists / raw .py)
    if path.lower().endswith(".py") and u.scheme != "file":
        raw = s
        if host == "github.com" and "/blob/" in path:
            raw = "https://raw.githubusercontent.com/" + path.replace("/blob/", "/", 1)
        return {"kind": "file", "clone": raw, "name": os.path.basename(path), "norm": norm_url(raw),
                "ref": None, "host": host}

    parts = path.split("/")
    if host.endswith("github.com"):
        if len(parts) < 2:
            raise ValueError("GitHub link needs user/repo")
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            ref = parts[3]
        parts = parts[:2]
    repo_path = "/".join(parts)
    if repo_path.endswith(".git"):
        repo_path = repo_path[:-4]
    if not repo_path:
        raise ValueError("Link has no repository path")
    netloc = u.netloc.split("@")[-1]  # never keep credentials typed into the url
    if u.scheme == "file":
        clone = f"file:///{repo_path}"
        norm = clone.lower()
    else:
        clone = f"{u.scheme}://{netloc}/{repo_path}.git"
        norm = f"{host}/{repo_path}".lower()
    return {"kind": "git", "clone": clone, "name": repo_path.split("/")[-1], "norm": norm, "ref": ref, "host": host}


# --------------------------------------------------------------------------
# what's installed
# --------------------------------------------------------------------------
def _read_git_remote(d):
    try:
        txt = open(os.path.join(d, ".git", "config"), encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', txt, re.S)
    return m.group(1) if m else None


def _read_branch(d):
    try:
        head = open(os.path.join(d, ".git", "HEAD"), encoding="utf-8").read().strip()
    except OSError:
        return ""
    m = re.match(r"ref:\s*refs/heads/(.+)", head)
    return m.group(1) if m else ""


def find_installed():
    base = cn_dir()
    out = []

    def scan(dirpath, disabled):
        try:
            names = sorted(os.listdir(dirpath), key=str.lower)
        except OSError:
            return
        for n in names:
            p = os.path.join(dirpath, n)
            if n == "__pycache__":
                continue
            if os.path.isdir(p):
                if n == ".disabled":
                    scan(p, True)
                    continue
                if n.startswith("."):
                    continue
                dis = disabled or n.endswith(".disabled")
                clean = n[:-9] if n.endswith(".disabled") else n
                remote = _read_git_remote(p)
                out.append({"dir": n, "path": p, "isfile": False, "disabled": dis, "remote": remote,
                            "norm": norm_url(remote) if remote else None,
                            "branch": _read_branch(p), "key": _norm_name(clean)})
            elif n.endswith(".py") and n != "__init__.py" and not disabled:
                out.append({"dir": n, "path": p, "isfile": True, "disabled": False, "remote": None,
                            "norm": None, "branch": "", "key": _norm_name(n)})

    scan(base, False)
    return out


def match_installed(parsed, installed):
    ordered = sorted(installed, key=lambda it: it["disabled"])  # enabled copies win
    for it in ordered:
        if parsed["norm"] and it["norm"] and it["norm"] == parsed["norm"]:
            return it
    for it in ordered:  # same folder name -> a clone would collide anyway
        if it["key"] == _norm_name(parsed["name"]):
            return it
    return None


def list_installed_git(exclude_dir=None):
    res = []
    for it in find_installed():
        if it["isfile"] or it["disabled"] or not it["remote"]:
            continue
        if exclude_dir and os.path.abspath(it["path"]) == os.path.abspath(exclude_dir):
            continue
        url = re.sub(r"//[^/@]+@", "//", it["remote"])  # strip embedded credentials
        res.append({"dir": it["dir"], "url": url, "branch": it["branch"]})
    return res


# --------------------------------------------------------------------------
# subprocess helpers
# --------------------------------------------------------------------------
def _env(tokens, host):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    tok = (tokens or {}).get("github", "")
    if tok and host.endswith("github.com"):
        b64 = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {b64}"
    return env


def run_cmd(cmd, cwd, env, st, cancel, timeout=1800, label=None):
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, errors="replace")
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    out = []
    try:
        for line in p.stdout:
            line = line.strip()
            if line:
                out.append(line)
                st["msg"] = ((label + ": ") if label else "") + line[-140:]
            if cancel.is_set():
                p.kill()
                raise Cancelled()
    finally:
        timer.cancel()
    rc = p.wait()
    return rc, "\n".join(out[-25:])


def _git_error(tail, host):
    low = tail.lower()
    if any(k in low for k in ("authentication failed", "could not read username", "terminal prompts disabled", "403", "401")):
        return "Needs authentication - private repo? Add a GitHub token at the top."
    if "not found" in low or "does not exist" in low or "404" in low or "does not appear to be a git repository" in low:
        return "Repository not found, or private (check the link / add a GitHub token)"
    if "could not resolve host" in low or "unable to access" in low:
        return "Network problem reaching the git host"
    fatal = [ln for ln in tail.splitlines() if ln.lower().startswith(("fatal:", "error:"))]
    last = fatal[-1] if fatal else (tail.strip().splitlines()[-1] if tail.strip() else "git failed")
    return last[:200]


def _rmtree(path):
    def onerr(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=onerr)


def _pip_prefix():
    try:
        if subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, timeout=60).returncode == 0:
            return [sys.executable, "-m", "pip", "install", "--no-input"]
    except Exception:
        pass
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    raise ValueError("pip isn't available in this Python environment")


def _filtered_requirements(src, dest_dir, protect):
    lines = []
    for raw in open(src, encoding="utf-8", errors="ignore").read().splitlines():
        s = raw.strip()
        if protect and s and not s.startswith(("#", "-")):
            name = re.split(r"[<>=!~\[;@ ]", s, 1)[0].lower().replace("_", "-")
            if name in PROTECTED_PKGS:
                continue
        lines.append(raw)
    tmp = os.path.join(dest_dir, ".mli_requirements.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return tmp


def _has_real_lines(path):
    for ln in open(path, encoding="utf-8", errors="ignore").read().splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            return True
    return False


def install_deps(dest, opts, st, cancel, env):
    """returns warning string or ''."""
    warn = ""
    req = os.path.join(dest, "requirements.txt")
    if opts.get("requirements", True) and os.path.isfile(req):
        tmp = _filtered_requirements(req, dest, opts.get("protect_torch", True))
        try:
            if _has_real_lines(tmp):
                st.update(state="installing", msg="pip install -r requirements.txt")
                cmd = _pip_prefix() + ["-r", tmp]
                rc, tail = run_cmd(cmd, dest, env, st, cancel, label="pip")
                if rc != 0 and ("externally-managed" in tail.lower() or "pep 668" in tail.lower()):
                    # system Python on many cloud images (Ubuntu 23.04+) - same fix Manager users apply
                    rc, tail = run_cmd(cmd + ["--break-system-packages"], dest, env, st, cancel, label="pip")
                if rc != 0:
                    warn = "requirements install failed: " + (tail.splitlines()[-1][:160] if tail else "see console")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    inst = os.path.join(dest, "install.py")
    if opts.get("install_py", True) and os.path.isfile(inst):
        st.update(state="installing", msg="running install.py")
        rc, tail = run_cmd([sys.executable, "install.py"], dest, env, st, cancel, label="install.py")
        if rc != 0:
            warn = (warn + "; " if warn else "") + "install.py failed: " + (tail.splitlines()[-1][:160] if tail else "")
    return warn


def _rev(dest):
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# one entry
# --------------------------------------------------------------------------
HEX_REF = re.compile(r"^[0-9a-f]{7,40}$", re.I)


def process_entry(entry, opts, tokens, mode, st, cancel):
    parsed = parse_repo(entry.get("url"))
    ref = (entry.get("ref") or "").strip() or parsed["ref"]
    base = cn_dir()
    installed = find_installed()
    hit = match_installed(parsed, installed)
    st.update(name=parsed["name"])

    # ---- single .py file
    if parsed["kind"] == "file":
        if mode == "update":
            st.update(state="skipped", msg="single-file nodes can't be updated here")
            return
        if hit:
            st.update(state="skipped", msg=f"already installed ({hit['dir']})")
            return
        st.update(state="cloning", msg="downloading file")
        r = requests.get(parsed["clone"], timeout=60, headers={"User-Agent": UA})
        r.raise_for_status()
        with open(os.path.join(base, parsed["name"]), "wb") as f:
            f.write(r.content)
        st.update(state="done", msg="restart needed")
        return

    if not shutil.which("git"):
        raise ValueError("git isn't installed on this machine")
    env = _env(tokens, parsed["host"])

    # ---- update
    if mode == "update":
        if not hit or hit["isfile"]:
            st.update(state="skipped", msg="not installed")
            return
        if hit["disabled"]:
            st.update(state="skipped", msg="disabled in ComfyUI-Manager")
            return
        if ref and HEX_REF.match(ref):
            st.update(state="skipped", msg="pinned to a commit")
            return
        dest = hit["path"]
        before = _rev(dest)
        st.update(state="cloning", msg="git pull")
        rc, tail = run_cmd(["git", "pull", "--ff-only"], dest, env, st, cancel, label="git pull")
        if rc != 0:
            raise ValueError(_git_error(tail, parsed["host"]))
        run_cmd(["git", "submodule", "update", "--init", "--recursive"], dest, env, st, cancel)
        if _rev(dest) == before:
            st.update(state="uptodate", msg="")
            return
        warn = install_deps(dest, opts, st, cancel, env)
        st.update(state="warning" if warn else "updated", msg=warn or "restart needed")
        return

    # ---- install
    if hit:
        extra = " - but disabled in ComfyUI-Manager" if hit["disabled"] else ""
        st.update(state="skipped", msg=f"already installed (folder '{hit['dir']}'){extra}")
        return
    dest = os.path.join(base, parsed["name"])
    if os.path.exists(dest):
        st.update(state="skipped", msg=f"folder '{parsed['name']}' already exists")
        return

    def clone(args):
        return run_cmd(["git", "clone", *args, parsed["clone"], dest], base, env, st, cancel, label="git")

    st.update(state="cloning", msg="starting clone")
    try:
        shallow = ["--depth", "1", "--recursive", "--shallow-submodules"]
        if ref and HEX_REF.match(ref):
            rc, tail = clone(["--recursive"])
            if rc == 0:
                rc, tail = run_cmd(["git", "checkout", ref], dest, env, st, cancel, label="checkout")
                if rc == 0:
                    run_cmd(["git", "submodule", "update", "--init", "--recursive"], dest, env, st, cancel)
        elif ref:
            rc, tail = clone(shallow + ["--branch", ref])
            if rc != 0:  # maybe a tag/commit name git can't shallow-clone -> full clone + checkout
                if os.path.exists(dest):
                    _rmtree(dest)
                rc, tail = clone(["--recursive"])
                if rc == 0:
                    rc, tail = run_cmd(["git", "checkout", ref], dest, env, st, cancel, label="checkout")
        else:
            rc, tail = clone(shallow)
        if rc != 0:
            raise ValueError(_git_error(tail, parsed["host"]))
        warn = install_deps(dest, opts, st, cancel, env)
    except BaseException:
        if os.path.exists(dest):
            _rmtree(dest)  # never leave a half-cloned folder behind
        raise
    st.update(state="warning" if warn else "done", msg=warn or "restart needed")


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
JOBS = {}
_LOCK = threading.Lock()


class Job:
    def __init__(self, entries, mode):
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode
        self.cancel = threading.Event()
        self.state = "running"
        self.needs_restart = False
        self.items = {e["id"]: {"state": "queued", "msg": "", "name": ""} for e in entries}

    def snapshot(self):
        return {"id": self.id, "state": self.state, "needs_restart": self.needs_restart,
                "items": copy.deepcopy(self.items)}


def _run(job, entries, opts, tokens):
    try:
        for e in entries:
            st = job.items[e["id"]]
            if job.cancel.is_set():
                st.update(state="cancelled", msg="")
                continue
            try:
                process_entry(e, opts, tokens, job.mode, st, job.cancel)
                if st["state"] in ("done", "updated", "warning"):
                    job.needs_restart = True
            except Cancelled:
                st.update(state="cancelled", msg="")
            except Exception as ex:  # noqa: BLE001 - report each failure on its row
                st.update(state="error", msg=str(ex)[:300])
    finally:
        job.state = "cancelled" if job.cancel.is_set() else "finished"


def start_job(entries, opts, tokens, mode="install"):
    entries = [dict(e) for e in entries]
    for e in entries:
        e.setdefault("id", uuid.uuid4().hex[:8])
    job = Job(entries, mode)
    with _LOCK:
        JOBS[job.id] = job
        for old in list(JOBS)[:-15]:
            JOBS.pop(old, None)
    threading.Thread(target=_run, args=(job, entries, opts or {}, tokens or {}), daemon=True).start()
    return job


def check_entries(entries):
    installed = find_installed()
    out = {}
    for e in entries:
        try:
            p = parse_repo(e.get("url"))
            hit = match_installed(p, installed)
            out[e["id"]] = {"installed": bool(hit), "disabled": bool(hit and hit["disabled"]),
                            "dir": hit["dir"] if hit else None}
        except Exception as ex:
            out[e["id"]] = {"invalid": str(ex)}
    return out


# --------------------------------------------------------------------------
# resolving missing node types -> repos
# --------------------------------------------------------------------------
_DB = {"t": 0, "index": {}, "patterns": [], "titles": {}, "by_name": {}}
_DB_LOCK = threading.Lock()
_REG_CACHE = {}


def _load_db(force=False):
    with _DB_LOCK:
        if not force and _DB["index"] and time.time() - _DB["t"] < DB_TTL:
            return
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache = os.path.join(CACHE_DIR, "extension-node-map.json")
        fresh = os.path.isfile(cache) and time.time() - os.path.getmtime(cache) < DB_TTL
        data = None
        if force or not fresh:
            for u in DB_URLS:
                try:
                    r = requests.get(u, timeout=60, headers={"User-Agent": UA})
                    r.raise_for_status()
                    data = r.json()
                    tmp = cache + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f)
                    os.replace(tmp, cache)
                    break
                except Exception:
                    continue
        if data is None and os.path.isfile(cache):
            data = json.load(open(cache, encoding="utf-8"))
        if data is None:
            local = os.path.join(cn_dir(), "ComfyUI-Manager", "extension-node-map.json")
            if os.path.isfile(local):
                data = json.load(open(local, encoding="utf-8"))
        if data is None:
            raise RuntimeError("Couldn't get ComfyUI-Manager's node database (no internet?)")

        index, patterns, titles, by_name = {}, [], {}, {}
        for repo, val in data.items():
            names = val[0] if val else []
            meta = val[1] if len(val) > 1 and isinstance(val[1], dict) else {}
            titles[repo] = meta.get("title_aux") or meta.get("title") or repo.rstrip("/").split("/")[-1]
            for n in names:
                index.setdefault(n, []).append(repo)
            pat = meta.get("nodename_pattern")
            if pat:
                try:
                    patterns.append((re.compile(pat), repo))
                except re.error:
                    pass
            by_name.setdefault(_norm_name(repo.rstrip("/").split("/")[-1].removesuffix(".git")), repo)
        _DB.update(t=time.time(), index=index, patterns=patterns, titles=titles, by_name=by_name)


def _db_candidates(node_type):
    c = list(_DB["index"].get(node_type, []))
    for rx, repo in _DB["patterns"]:
        if repo not in c and rx.search(node_type):
            c.append(repo)
    return c


def _registry_repo(cnr_id):
    if cnr_id in _REG_CACHE:
        return _REG_CACHE[cnr_id]
    url = None
    try:
        r = requests.get(REGISTRY_API + cnr_id, timeout=10, headers={"User-Agent": UA})
        if r.ok:
            url = (r.json() or {}).get("repository") or None
    except Exception:
        pass
    _REG_CACHE[cnr_id] = url
    return url


def resolve_missing(items, force_db=False):
    """items: [{type, cnr_id?, aux_id?}] -> {repos:[...], unresolved:[...]}"""
    _load_db(force_db)
    installed = find_installed()
    repos = {}  # norm -> dict

    def add(url, source, node_type):
        try:
            p = parse_repo(url)
        except ValueError:
            return False
        r = repos.get(p["norm"])
        if not r:
            hit = match_installed(p, installed)
            r = repos[p["norm"]] = {
                "url": url.rstrip("/"), "source": source, "covers": [],
                "title": _DB["titles"].get(url) or p["name"],
                "installed": bool(hit), "disabled": bool(hit and hit["disabled"]),
            }
        if node_type not in r["covers"]:
            r["covers"].append(node_type)
        return True

    pending, unresolved = [], []
    for it in items:
        t = it.get("type")
        if not t:
            continue
        cnr, aux = it.get("cnr_id"), it.get("aux_id")
        done = False
        if aux and re.match(r"^[\w.-]+/[\w.-]+$", aux):
            done = add(f"https://github.com/{aux}", "hint", t)
        elif cnr and cnr != "comfy-core":
            reg = _registry_repo(cnr)
            if reg:
                done = add(reg, "registry", t)
            if not done and _DB["by_name"].get(_norm_name(cnr)):
                done = add(_DB["by_name"][_norm_name(cnr)], "manager-db", t)
        if not done:
            if cnr == "comfy-core":
                unresolved.append({"type": t, "reason": "core ComfyUI node - update ComfyUI itself"})
            else:
                pending.append(t)

    # greedy set-cover over the Manager DB so one repo covering many types wins
    cands = {t: _db_candidates(t) for t in pending}
    chosen_norms = {n for n in repos}
    remaining = set(pending)
    for t in list(remaining):  # prefer repos we already picked
        for c in cands[t]:
            try:
                if parse_repo(c)["norm"] in chosen_norms:
                    add(c, "manager-db", t)
                    remaining.discard(t)
                    break
            except ValueError:
                continue
    while remaining:
        score = {}
        for t in remaining:
            for c in cands[t]:
                score.setdefault(c, set()).add(t)
        if not score:
            break
        best = max(score, key=lambda c: (len(score[c]), "github.com" in c))
        for t in score[best]:
            add(best, "manager-db", t)
        remaining -= score[best]
    for t in remaining:
        unresolved.append({"type": t, "reason": "not found in ComfyUI-Manager's database"})

    return {"repos": list(repos.values()), "unresolved": unresolved}
