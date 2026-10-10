#!/usr/bin/env python3
"""List, save and load prompt caches (slot state) for a llama.cpp server.

API (verified against llama.cpp master, tools/server/server-context.cpp):
  GET  /v1/models                           -> models with status loaded/unloaded
  GET  /slots?model=<model>                 -> list slots
  POST /slots/<id>?action=save              -> body {"model": ..., "filename": ...}
  POST /slots/<id>?action=restore           -> body {"model": ..., "filename": ...}
  POST /slots/<id>?action=erase             -> body {"model": ...}

Unless --model is given explicitly, the currently loaded model is detected
via /v1/models (status.value == "loaded"), so the wrong model can never be
requested (which would unload the active one and lose its cache).

save/restore/erase require the server to be started with --slot-save-path;
saved files are written to that directory on the server's filesystem.
Cache filenames are prefixed with the model name, e.g.
  Qwen3.8-27B-UD-Q4_K_XL.gguf_slot0_20261010-131457.bin
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

DEFAULT_URL = "http://localhost:8081"
DEFAULT_TOKEN = ""


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class NoModelError(Exception):
    pass


def request(url, token, method="GET", payload=None, query=None):
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            err = json.loads(raw)
            msg = err.get("message") or err.get("error", {}).get("message", raw)
        except (json.JSONDecodeError, AttributeError):
            msg = raw
        raise ApiError(e.code, msg) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def get_models(url, token):
    data = request(f"{url}/v1/models", token)
    return data.get("data", []) if isinstance(data, dict) else []


def loaded_models(url, token):
    return [
        m["id"] for m in get_models(url, token)
        if isinstance(m.get("status"), dict) and m["status"].get("value") == "loaded"
    ]


def server_slot_save_path(url, token):
    """Recover the server's --slot-save-path from its launch args in /v1/models."""
    for m in get_models(url, token):
        args = m.get("status", {}).get("args", [])
        for i, a in enumerate(args):
            if a == "--slot-save-path" and i + 1 < len(args):
                return args[i + 1]
            if a.startswith("--slot-save-path="):
                return a.split("=", 1)[1]
    return None


def resolve_model(args):
    """Return the model to use: explicit --model, else the loaded one."""
    if args.model:
        return args.model
    loaded = loaded_models(args.url, args.token)
    if not loaded:
        raise NoModelError("no model is currently loaded on the server")
    if len(loaded) > 1:
        raise NoModelError(
            "multiple models are loaded (" + ", ".join(loaded) + "); "
            "specify one with --model"
        )
    return loaded[0]


def safe_name(name):
    """Make a model id safe for use inside a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def cache_filename(args, model, name):
    """Prepend the model name to a cache filename (unless already present
    or --no-model-prefix)."""
    if getattr(args, "no_model_prefix", False):
        return name
    prefix = safe_name(model) + "_"
    if name.startswith(prefix):
        return name
    return prefix + name


def cmd_list(args):
    model = resolve_model(args)
    slots = request(f"{args.url}/slots", args.token, query={"model": model})
    if not isinstance(slots, list):
        print(slots, file=sys.stderr)
        return 1
    print(f"model: {model}")
    if not slots:
        print("no slots")
        return 0
    print(f"{'ID':>3}  {'busy':<5} {'ctx':>8} {'prompt':>8} {'cached':>8}  info")
    for s in slots:
        prompt = s.get("n_prompt_tokens", 0)
        cached = s.get("n_prompt_tokens_cache", 0)
        info = []
        if s.get("is_processing"):
            info.append("processing")
        if s.get("id_task"):
            info.append(f"task={s['id_task']}")
        print(
            f"{s['id']:>3}  {str(s.get('is_processing', False)):<5} "
            f"{s.get('n_ctx', 0):>8} {prompt:>8} {cached:>8}  {' '.join(info)}"
        )
    return 0


def cmd_save(args):
    model = resolve_model(args)
    filename = cache_filename(args, model, args.filename)
    res = request(
        f"{args.url}/slots/{args.slot}",
        args.token,
        method="POST",
        payload={"model": model, "filename": filename},
        query={"action": "save"},
    )
    print(json.dumps(res, indent=2) if not isinstance(res, str) else res)
    print(f"saved slot {args.slot} cache as '{filename}' "
          f"(stored in the server's --slot-save-path directory)")
    return 0


def cmd_save_all(args):
    model = resolve_model(args)
    slots = request(f"{args.url}/slots", args.token, query={"model": model})
    if not isinstance(slots, list):
        print(slots, file=sys.stderr)
        return 1
    print(f"model: {model}")
    if not slots:
        print("no slots")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = safe_name(model) + "_" if not args.no_model_prefix else ""
    rc = 0
    for s in slots:
        sid = s["id"]
        prompt = s.get("n_prompt_tokens", 0)
        if prompt == 0 and not args.include_empty:
            print(f"slot {sid}: empty, skipped")
            continue
        filename = f"{args.prefix}{base}slot{sid}_{stamp}.bin"
        try:
            request(
                f"{args.url}/slots/{sid}",
                args.token,
                method="POST",
                payload={"model": model, "filename": filename},
                query={"action": "save"},
            )
            print(f"slot {sid}: saved '{filename}' ({prompt} prompt tokens)")
        except ApiError as e:
            print(f"slot {sid}: error: {e}", file=sys.stderr)
            rc = 1
    return rc


def cmd_load(args):
    model = resolve_model(args)
    filename = cache_filename(args, model, args.filename)
    res = request(
        f"{args.url}/slots/{args.slot}",
        args.token,
        method="POST",
        payload={"model": model, "filename": filename},
        query={"action": "restore"},
    )
    print(json.dumps(res, indent=2) if not isinstance(res, str) else res)
    print(f"loaded '{filename}' into slot {args.slot}")
    return 0


def cmd_erase(args):
    model = resolve_model(args)
    res = request(
        f"{args.url}/slots/{args.slot}",
        args.token,
        method="POST",
        payload={"model": model},
        query={"action": "erase"},
    )
    print(json.dumps(res, indent=2) if not isinstance(res, str) else res)
    print(f"erased slot {args.slot} cache")
    return 0


def cmd_files(args):
    path = args.slot_save_path
    if not path:
        path = server_slot_save_path(args.url, args.token)
        if path:
            print(f"slot-save-path (from server): {path}")
    if not path:
        print("error: could not determine --slot-save-path; "
              "pass it explicitly to 'files'", file=sys.stderr)
        return 1
    if not os.path.isdir(path):
        print(f"error: directory not found: {path}", file=sys.stderr)
        return 1
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            entries.append((os.path.getsize(full), name))
    if not entries:
        print("no saved caches")
        return 0
    for size, name in entries:
        print(f"{size:>14,}  {name}")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="List, save and load prompt caches for a llama.cpp server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--url", default=DEFAULT_URL, help="server URL")
    p.add_argument("--model", default=None,
                   help="model name (default: auto-detect the currently loaded model)")
    p.add_argument("--token", default=DEFAULT_TOKEN, help="bearer token")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list slots and their prompt cache state").set_defaults(func=cmd_list)

    sp = sub.add_parser("save", help="save a slot's prompt cache to a file")
    sp.add_argument("slot", type=int, help="slot id (see 'list')")
    sp.add_argument("filename", help="cache filename (model name is prepended)")
    sp.add_argument("--no-model-prefix", action="store_true",
                    help="do not prepend the model name to the filename")
    sp.set_defaults(func=cmd_save)

    sp = sub.add_parser("save-all", help="save every slot's prompt cache with auto-generated names")
    sp.add_argument("--prefix", default="", help="prefix for the generated filenames")
    sp.add_argument("--include-empty", action="store_true",
                    help="also save slots that have no prompt tokens")
    sp.add_argument("--no-model-prefix", action="store_true",
                    help="do not prepend the model name to the filenames")
    sp.set_defaults(func=cmd_save_all)

    sp = sub.add_parser("load", help="load a saved prompt cache into a slot")
    sp.add_argument("slot", type=int, help="slot id (see 'list')")
    sp.add_argument("filename", help="cache filename (model name is prepended)")
    sp.add_argument("--no-model-prefix", action="store_true",
                    help="do not prepend the model name to the filename")
    sp.set_defaults(func=cmd_load)

    sp = sub.add_parser("erase", help="erase a slot's prompt cache")
    sp.add_argument("slot", type=int, help="slot id (see 'list')")
    sp.set_defaults(func=cmd_erase)

    sp = sub.add_parser("files", help="list saved cache files on disk")
    sp.add_argument("--slot-save-path", default=None,
                    help="server's --slot-save-path directory (local path)")
    sp.set_defaults(func=cmd_files)

    args = p.parse_args()
    try:
        return args.func(args)
    except NoModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        if e.status == 501:
            print("hint: start the server with --slot-save-path <dir> to enable "
                  "save/load/erase", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"error: cannot reach {args.url}: {e.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
