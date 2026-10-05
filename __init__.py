import asyncio
import os
import sys
import threading
import time

from aiohttp import web
from server import PromptServer

from . import installer as ins

WEB_DIRECTORY = "./web"
routes = PromptServer.instance.routes
SELF_DIR = os.path.dirname(os.path.abspath(__file__))


def _err(msg, status=400):
    return web.json_response({"error": str(msg)}, status=status)


async def _in_thread(fn, *a):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *a)


@routes.get("/mli/installed")
async def mli_installed(request):
    try:
        return web.json_response({"nodes": await _in_thread(ins.list_installed_git, SELF_DIR)})
    except Exception as e:
        return _err(e, 500)


@routes.post("/mli/check")
async def mli_check(request):
    try:
        body = await request.json()
        return web.json_response({"result": await _in_thread(ins.check_entries, body.get("entries", []))})
    except Exception as e:
        return _err(e, 500)


@routes.post("/mli/resolve")
async def mli_resolve(request):
    try:
        body = await request.json()
        res = await _in_thread(ins.resolve_missing, body.get("items", []), bool(body.get("refresh_db")))
        return web.json_response(res)
    except Exception as e:
        return _err(e, 500)


@routes.post("/mli/start")
async def mli_start(request):
    try:
        body = await request.json()
        entries = [e for e in body.get("entries", []) if (e.get("url") or "").strip()]
        if not entries:
            return _err("Nothing to do")
        mode = "update" if body.get("mode") == "update" else "install"
        job = ins.start_job(entries, body.get("opts"), body.get("tokens"), mode)
        return web.json_response({"job": job.id})
    except Exception as e:
        return _err(e, 500)


@routes.get("/mli/status")
async def mli_status(request):
    job = ins.JOBS.get(request.query.get("job", ""))
    if not job:
        return _err("Unknown job", 404)
    return web.json_response(job.snapshot())


@routes.post("/mli/cancel")
async def mli_cancel(request):
    body = await request.json()
    job = ins.JOBS.get(body.get("job", ""))
    if job:
        job.cancel.set()
    return web.json_response({"ok": True})


@routes.post("/mli/restart")
async def mli_restart(request):
    def _do():
        time.sleep(1.0)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        args = [sys.executable] + (["-s"] if getattr(sys.flags, "no_user_site", 0) else []) + sys.argv
        os.execv(sys.executable, args)

    threading.Thread(target=_do, daemon=True).start()
    return web.json_response({"ok": True})


class CustomNodeInstaller:
    """All the UI lives in web/custom_node_installer.js. `state` is a hidden JSON string
    holding the git links (and the GitHub token only if the user ticked 'save in workflow')."""

    CATEGORY = "utils/model downloader"
    FUNCTION = "noop"
    RETURN_TYPES = ()

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"state": ("STRING", {"default": "{}"})}}

    def noop(self, state):
        return {}


NODE_CLASS_MAPPINGS = {"CustomNodeInstaller": CustomNodeInstaller}
NODE_DISPLAY_NAME_MAPPINGS = {"CustomNodeInstaller": "Custom Node Installer (git links + auto-scan)"}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
