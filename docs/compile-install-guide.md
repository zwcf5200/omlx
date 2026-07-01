# oMLX compile and install guide

Date: 2026-06-27

This guide covers building and installing oMLX from this source tree on Apple
Silicon macOS. It focuses on developer installs, optional native kernel builds,
and the local Swift macOS app build.

## Current Environment Check

Checked on this machine:

| Item | Result | Notes |
| --- | --- | --- |
| Repository | `/Users/zhouwei/code/zw/omlx` | Source tree used for the local editable install. |
| macOS | `26.5.1 (25F80)` | Meets the project requirement of macOS 15.0+. |
| CPU/platform | Apple Silicon arm64 | Required by MLX. |
| Default `python3` | `3.14.3` | Avoid using this for the project until dependencies officially support it. |
| Project `.venv` Python | `3.12.9` | Recommended local interpreter for source development. |
| `uv` | `0.9.7` | Installed through Homebrew. |
| Homebrew | `6.0.2` | Available. |
| Xcode selection | `/Library/Developer/CommandLineTools` | Command Line Tools are active, not full Xcode. |
| `xcodebuild` | Not available with current selection | Full Swift app builds need full Xcode selected. |
| `clang` | Apple clang `21.0.0` | Available from Command Line Tools. |
| `cmake` | `4.2.1` | Available. |
| `metal` compiler | Not found | Required for optional Metal custom kernels. Install/select full Xcode. |
| Existing oMLX CLI | `.venv/bin/omlx --version` -> `0.4.4` | Current editable environment works. |
| MLX runtime | `mlx 0.31.2`, default device `Device(gpu, 0)` | Metal device is visible outside the sandbox. |
| `uv.lock` | Not tracked in this checkout | `pyproject.toml` is the dependency source of truth for source installs here. |

The current environment is good enough for normal source installation and
server development. It is not sufficient for a full Swift app build or optional
custom Metal kernel build until full Xcode is installed and selected.

## Requirements

Minimum project requirements:

- Apple Silicon Mac.
- macOS 15.0 or newer.
- Python 3.11 to 3.13 for practical development. Python 3.12 is recommended.
- `uv` or `pip`.
- Command Line Tools for normal Python/source work.
- Full Xcode for Swift app builds, asset compilation, signing, and optional
  Metal custom kernels.

Recommended tools:

```bash
brew install uv cmake
xcode-select --install
```

For full app or Metal kernel builds, install Xcode from Apple and select it:

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
xcodebuild -version
xcrun --find metal
```

Both commands should succeed before attempting the app bundle or custom kernel
paths.

## Dependency Sources and Mirrors

`pyproject.toml` pins several packages directly from GitHub:

- `mlx-lm`
- `mlx-embeddings`
- `mlx-vlm`
- `dflash-mlx`
- optional `mlx-audio`

A domestic PyPI mirror speeds up wheel downloads, but it does not replace
`git+https://github.com/...` dependencies.

Recommended PyPI mirror command:

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync
```

The install validated in this checkout used Aliyun:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple uv pip install --python .venv/bin/python -e .
```

If GitHub access is slow, install only the packages needed for a targeted task,
or configure GitHub access separately. The PyPI mirror alone cannot mirror the
Git dependencies declared in `pyproject.toml`.

## Local Dependency Override Used Here

For this local machine, `pyproject.toml` was changed to avoid repeated GitHub
dependency fetches during source installs. The GitHub-pinned dependencies now
point at local source directories:

| Package | Local source |
| --- | --- |
| `mlx-lm` | `/Users/zhouwei/code/zw/mlx-lm` |
| `mlx-embeddings` | `/Users/zhouwei/code/zw/mlx-embeddings-32981fa4e8064ed664b52071789dd18271fe4206` |
| `mlx-vlm` | `/Users/zhouwei/code/zw/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c` |
| `dflash-mlx` | `/Users/zhouwei/code/zw/dflash-mlx-9ca002898b48e14c9727dec17299f497e8467870` |
| `mlx-audio` | `/Users/zhouwei/code/zw/mlx-audio-51753266e0a4f766fd5e6fbc46652224efc23981` |

The `mlx-lm` local checkout was moved to the original pinned commit:

```bash
git -C /Users/zhouwei/code/zw/mlx-lm \
  -c http.version=HTTP/1.1 \
  fetch --depth=1 origin 2c008fd0252b2c569227d12568356ab88ab0560a
git -C /Users/zhouwei/code/zw/mlx-lm \
  checkout --detach 2c008fd0252b2c569227d12568356ab88ab0560a
```

Because the local `mlx-vlm` source was prepared from a codeload tarball rather
than a git clone, its directory name carries the commit hash. Keep that path
stable as long as `pyproject.toml` points to it.

### `mlx-vlm` Download Card

The main install blocker was `mlx-vlm`. Smaller repositories could be cloned or
downloaded, but `mlx-vlm` repeatedly failed over the configured HTTP proxy:

- `git fetch` / `git clone` could sit without progress or fail with early EOF.
- codeload over HTTP/2 failed around `10MB` with:
  `HTTP/2 stream 1 was not closed cleanly: CANCEL`.
- A partially extracted in-repo `mlx-vlm.../` directory was only about `4.9MB`
  and had no `pyproject.toml`; it was not usable as a Python dependency.

The reliable path was to force HTTP/1.1 for codeload:

```bash
curl --http1.1 -L --fail \
  --retry 8 --retry-all-errors \
  --connect-timeout 30 \
  --speed-limit 1000 --speed-time 120 \
  -o /tmp/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c.tar.gz \
  https://codeload.github.com/Blaizzy/mlx-vlm/tar.gz/e3906673d54df42cf573b837a76e65d58a2b865c

gzip -t /tmp/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c.tar.gz
mkdir -p /tmp/mlx-vlm-extract
tar -xzf /tmp/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c.tar.gz \
  -C /tmp/mlx-vlm-extract

rsync -a \
  /tmp/mlx-vlm-extract/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c/ \
  /Users/zhouwei/code/zw/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c/
```

After syncing, verify the local dependency has package metadata:

```bash
test -f /Users/zhouwei/code/zw/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c/pyproject.toml
test -d /Users/zhouwei/code/zw/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c/mlx_vlm
```

Clean temporary artifacts after the verified copy:

```bash
rm -rf \
  /tmp/mlx-vlm-e3906673d54df42cf573b837a76e65d58a2b865c.tar.gz \
  /tmp/mlx-vlm-extract
```

## Source Development Install

Use a supported Python version explicitly. Do not rely on this machine's default
`python3` if it is Python 3.14.

```bash
cd /Users/zhouwei/code/zw/omlx

# Example with Homebrew Python 3.12 if available.
uv venv --python 3.12 .venv
source .venv/bin/activate

UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple \
  uv pip install --python .venv/bin/python -e .
```

With MCP support:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple \
  uv pip install --python .venv/bin/python -e ".[mcp]"
```

With development tools:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple \
  uv pip install --python .venv/bin/python -e ".[dev]"
```

If you prefer resolver-managed sync:

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --dev
```

Use `uv sync --dev` only when GitHub dependency downloads are acceptable in the
current network.

## Verify Source Install

Check the CLI:

```bash
.venv/bin/omlx --version
.venv/bin/omlx --help
```

Check MLX and Metal visibility:

```bash
.venv/bin/python -c 'import importlib.metadata as m; import mlx.core as mx; print("mlx", m.version("mlx")); print(mx.default_device())'
```

Expected device on a working Apple Silicon setup:

```text
Device(gpu, 0)
```

Check dependency consistency:

```bash
uv pip check --python .venv/bin/python
```

Expected result:

```text
All installed packages are compatible
```

Confirm important local dependency origins:

```bash
.venv/bin/python -c 'import importlib.metadata as m, pathlib; names=["mlx-vlm","mlx-lm","mlx-embeddings","dflash-mlx"]; [(print("==", n, m.distribution(n).version), print((pathlib.Path(m.distribution(n)._path)/"direct_url.json").read_text())) for n in names]'
```

Each `direct_url.json` should point to a local `file:///Users/zhouwei/code/zw/...`
path.

Run a foreground server:

```bash
.venv/bin/omlx serve \
  --host 127.0.0.1 \
  --port 8000 \
  --model-dir ~/.omlx/models \
  --hf-endpoint https://hf-mirror.com \
  --log-level info
```

Health check:

```bash
curl -sS http://127.0.0.1:8000/api/status
```

If you pass `--api-key`, include:

```bash
curl -sS \
  -H 'Authorization: Bearer <your-api-key>' \
  http://127.0.0.1:8000/api/status
```

## Test Commands

Targeted tests are usually faster and more reliable than a full test run during
installation work.

```bash
.venv/bin/python -m pytest tests/test_cli.py \
  tests/test_vlm_model_adapter.py::TestVLMModelAdapter::test_release_resources_drops_model_references
.venv/bin/python -m pytest tests/test_cli.py
.venv/bin/python -m pytest tests/test_per_engine_threads.py tests/test_engine_core.py
```

The validated source install passed:

```text
54 passed in 2.07s
```

Run MLX/Metal tests outside restricted sandboxes. A restricted environment can
fail with errors such as:

```text
RuntimeError: [metal::load_device] No Metal device available
```

Run diff hygiene before committing:

```bash
git diff --check
```

## Optional Native Custom Kernel Build

The optional GLM custom kernels live under:

```text
omlx/custom_kernels/glm_moe_dsa
```

Build path:

```bash
cd /Users/zhouwei/code/zw/omlx
source .venv/bin/activate

OMLX_WITH_CUSTOM_KERNEL=1 \
OMLX_CUSTOM_KERNEL_DEPLOYMENT_TARGET=15.0 \
python setup.py build_ext --inplace --force --with-custom-kernel
```

Prerequisites:

- Full Xcode selected.
- `xcrun --find metal` succeeds.
- `cmake` is available.
- `mlx==0.31.2` is installed in the active environment.

Current machine status: this path is blocked until full Xcode is selected,
because `xcrun --find metal` currently fails.

## Swift macOS App Build

The Python/PyObjC app pipeline has been retired. The Swift app under
`apps/omlx-mac` is the current local app bundle path.

Build Python venvstacks layers only:

```bash
cd /Users/zhouwei/code/zw/omlx
python packaging/build.py --venvstacks-only
```

Print the layer fingerprint:

```bash
python packaging/build.py --print-fingerprint
```

Build the full local app bundle:

```bash
apps/omlx-mac/Scripts/build.sh release
```

Reuse an existing `packaging/_export` layer tree:

```bash
apps/omlx-mac/Scripts/build.sh release --no-rebuild-donor
```

Build only the Swift app shell while reusing existing Python layers:

```bash
apps/omlx-mac/Scripts/build.sh swift
```

Build with optional custom kernels:

```bash
apps/omlx-mac/Scripts/build.sh release --with-custom-kernel
```

Output:

```text
apps/omlx-mac/build/Stage/oMLX.app
```

Install or run:

```bash
open apps/omlx-mac/build/Stage/oMLX.app
```

or copy the app to `/Applications`.

Current machine status: full app build is blocked until full Xcode is selected,
because `xcodebuild` currently reports that the active developer directory is
only Command Line Tools.

## Common Failure Modes

### `uv sync` is too slow

Use a domestic PyPI mirror:

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv sync --dev
```

or:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple uv sync --dev
```

If it still stalls, the likely bottleneck is one of the GitHub dependencies.
For local debugging, install only the required packages into `.venv` with
`uv pip install`.

### `mlx-vlm` download fails around 10MB

If codeload fails with `HTTP/2 stream 1 was not closed cleanly: CANCEL`, force
HTTP/1.1:

```bash
curl --http1.1 -L --fail --retry 8 --retry-all-errors \
  -o /tmp/mlx-vlm.tar.gz \
  https://codeload.github.com/Blaizzy/mlx-vlm/tar.gz/e3906673d54df42cf573b837a76e65d58a2b865c
```

Always run `gzip -t` before trusting the tarball. A truncated extraction may
look like a source tree but miss `pyproject.toml`, which makes the `file://`
dependency invalid.

### `uv pip install --reinstall mlx-lm` upgrades transitive packages

Installing a single local dependency directly can temporarily pull versions
outside oMLX's root constraints. In one run, direct reinstall of `mlx-lm`
upgraded `numpy` to `2.5.0`; reinstalling the root project restored the project
constraint:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple \
  uv pip install --python .venv/bin/python -e .

uv pip check --python .venv/bin/python
```

### Default Python is too new

This machine's default `python3` is `3.14.3`. The project metadata says
`requires-python >=3.11`, but several ML/packaging dependencies may lag behind
new Python releases. Use Python 3.12 or 3.13 explicitly.

### `xcodebuild` fails with Command Line Tools

Symptom:

```text
xcode-select: error: tool 'xcodebuild' requires Xcode
```

Fix:

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
```

### `xcrun --find metal` fails

This blocks optional Metal custom kernels. Install/select full Xcode, then
rerun:

```bash
xcrun --find metal
```

### MLX cannot see a Metal device

Verify outside restricted sandboxes:

```bash
.venv/bin/python -c 'import mlx.core as mx; print(mx.default_device())'
```

Expected:

```text
Device(gpu, 0)
```

## Recommended Local Workflow

For normal development on this machine:

```bash
cd /Users/zhouwei/code/zw/omlx
source .venv/bin/activate

.venv/bin/omlx --version
.venv/bin/python -m pytest tests/test_cli.py
git diff --check
```

For server validation:

```bash
.venv/bin/omlx serve \
  --host 127.0.0.1 \
  --port 11445 \
  --model-dir ~/.omlx/models \
  --hf-endpoint https://hf-mirror.com \
  --base-path /tmp/omlx-dev \
  --log-level info
```

Use a non-default port such as `11445` when the app-managed server or other
local services may already own the standard port.
