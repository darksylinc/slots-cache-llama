# slots-cache-llama

CLI tool to **list, save and load prompt caches** (slot state) of a
[llama.cpp](https://github.com/ggml-org/llama.cpp) server.

A "prompt cache" here is the full state of a server slot — KV cache and
sampling state — saved to disk by the server's slot actions. Saving a slot
lets you restore a long conversation/context later without reprocessing the
prompt tokens.

## Requirements

- A `llama-server` build with slot actions, started with
  `--slot-save-path <dir>` (the directory saved caches are written to)
- An API/bearer token if the server is protected
- Python 3 (standard library only, no dependencies)

## How it works

- Unless `--model` is given, the tool queries `GET /v1/models` and uses the
  model whose `status` is `loaded`. This prevents accidentally requesting a
  different model, which would unload the active one and lose its cache.
- If no model (or several) is loaded, the tool errors out instead of guessing.
- Cache filenames are prefixed with the model name, e.g.
  `Qwen3.8-27B-UD-Q4_K_XL.gguf_slot0_20261010-132418.bin`, so caches from
  different models coexist in one directory. The prefix is idempotent:
  passing an already-prefixed name does not double it.
- `files` auto-discovers the server's `--slot-save-path` from the launch
  args embedded in the `/v1/models` response. But the script must be launched on
  the same system as the server.

Server endpoints used:

| Command    | Request                                            |
|------------|----------------------------------------------------|
| `list`     | `GET /slots?model=<model>`                         |
| `save`     | `POST /slots/<id>?action=save`   `{"model", "filename"}` |
| `load`     | `POST /slots/<id>?action=restore` `{"model", "filename"}` |
| `erase`    | `POST /slots/<id>?action=erase`  `{"model"}`       |
| `files`    | local directory listing (no server endpoint exists) |

## Usage

Defaults: `--url http://localhost:8081`, `--token ""`, model
auto-detected. Override with `--url`, `--token`, `--model` as needed.

```bash
# List slots and their prompt cache state (uses the currently loaded model)
./slot_cache.py list

# Save one slot's cache (model name is prepended to the filename)
./slot_cache.py save 0 mycache.bin
# -> Qwen3.8-27B-UD-Q4_K_XL.gguf_mycache.bin

# Save every non-empty slot with auto-generated names
./slot_cache.py save-all
# -> Qwen3.8-27B-UD-Q4_K_XL.gguf_slot0_20261010-132418.bin
./slot_cache.py save-all --prefix run1_ --include-empty

# Load a cache into a slot (short or full filename both work)
./slot_cache.py load 1 slot0_20261010-132418.bin
./slot_cache.py load 1 Qwen3.8-27B-UD-Q4_K_XL.gguf_slot0_20261010-132418.bin

# Erase a slot's cache
./slot_cache.py erase 1

# List saved cache files on disk (path auto-detected from the server)
./slot_cache.py files
./slot_cache.py files --slot-save-path /custom/dir   # override
```

## Notes

- `save`/`load`/`erase` fail with HTTP 501 if the server was not started
  with `--slot-save-path`.
- `--no-model-prefix` (on `save`, `load`, `save-all`) disables the model
  name prefix, e.g. to load files saved by older versions of this tool.
- Saving a slot that is currently processing a request may be slow or
  refused by the server; save idle slots when possible.
- Saved files are large (hundreds of MB to GBs, proportional to the cached
  context); prune the cache directory as needed.
