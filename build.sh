#!/usr/bin/env bash
# Build a standalone binary of rcm with Nuitka.
#
# Usage:
#   ./build.sh                      # standalone (default)
#   ./build.sh onefile              # single-file binary
#   ./build.sh standalone --lto=yes # extra Nuitka args after the mode

set -euo pipefail

cd "$(dirname "$0")"

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if ! python -c "import nuitka" >/dev/null 2>&1; then
  echo ">>> Nuitka not found in the active environment; installing..."
  python -m pip install --quiet 'nuitka>=2.4'
fi

MODE="${1:-standalone}"
shift || true
case "$MODE" in
  standalone|onefile) ;;
  *)
    echo "error: mode must be 'standalone' or 'onefile' (got: $MODE)" >&2
    exit 2
    ;;
esac

OUT_DIR="build"
mkdir -p "$OUT_DIR"

echo ">>> Building rcm in $MODE mode -> $OUT_DIR/"

# Common flags. Use run_rcm.py as the entry point — a thin wrapper around
# rcm.server.main(). This avoids Nuitka bugs with --python-flag=-m on newer
# Python versions.
COMMON=(
  "--mode=$MODE"
  "--assume-yes-for-downloads"
  "--output-dir=$OUT_DIR"
  "--output-filename=rcm"
  "--remove-output"

  # Force-include our own package.
  "--include-package=rcm"

  # Drop dev-only or alternative-async-backend transitive imports.
  "--noinclude-pytest-mode=nofollow"
  "--noinclude-IPython-mode=nofollow"
  "--noinclude-setuptools-mode=nofollow"
  "--noinclude-unittest-mode=nofollow"
  "--nofollow-import-to=trio"
  "--nofollow-import-to=tkinter"
  "--nofollow-import-to=tests"
  "--nofollow-import-to=test"
)

# Force-include packages with dynamic / lazy / entry-point imports that
# Nuitka's static analysis cannot see. Probe importability so the build
# does not break when an optional dependency is not installed.
for pkg in \
    fastmcp fastmcp_slim mcp uvicorn starlette \
    pydantic pydantic_core pydantic_settings \
    anyio sniffio httpx httpcore httpx_sse h11 sse_starlette \
    yaml exceptiongroup typing_extensions typing_inspection \
    jsonschema jsonschema_specifications jsonschema_path \
    attrs referencing rpds rich markdown_it mdurl pygments \
    click cyclopts docstring_parser \
    authlib joserfc cryptography cffi pycparser jwt \
    idna certifi platformdirs dotenv \
    python_multipart multipart openapi_pydantic jsonref pathable \
    beartype aiofile caio cachetools keyring jaraco more_itertools \
    email_validator dns annotated_types \
    importlib_metadata \
    ; do
  if python -c "import $pkg" >/dev/null 2>&1; then
    COMMON+=("--include-package=$pkg")
  fi
done

# Bundle package data (templates, JSON schemas, etc.) for libraries we ship.
for pkg in fastmcp mcp starlette pydantic uvicorn jsonschema_specifications certifi; do
  if python -c "import $pkg" >/dev/null 2>&1; then
    COMMON+=("--include-package-data=$pkg")
  fi
done

# Optional uvicorn extras / yaml C accelerator. Include only what is installed.
for opt_mod in h11 httptools websockets wsproto uvloop _yaml exceptiongroup; do
  if python -c "import $opt_mod" >/dev/null 2>&1; then
    COMMON+=("--include-module=$opt_mod")
  fi
done

# Nuitka's anti-bloat config rewrites opentelemetry.util._importlib_metadata
# to the third-party `importlib_metadata`. Ensure it is installed and included
# (already in the --include-package loop above).

# Some packages check importlib.metadata for their distribution info / entry
# points at runtime; keep that metadata in the bundle.
for dist in fastmcp fastmcp-slim mcp uvicorn starlette pydantic pydantic-core \
            pydantic-settings opentelemetry-api anyio httpx httpcore httpx-sse \
            sse-starlette PyYAML jsonschema rich click cyclopts authlib certifi \
            python-multipart; do
  if python -c "from importlib.metadata import distribution; distribution('$dist')" >/dev/null 2>&1; then
    COMMON+=("--include-distribution-metadata=$dist")
  fi
done

if [[ "$MODE" == "onefile" ]]; then
  # Stable extract dir avoids re-extraction churn on every launch.
  COMMON+=("--onefile-tempdir-spec={CACHE_DIR}/rcm")
fi

set -x
python -m nuitka "${COMMON[@]}" "$@" run_rcm.py
set +x

if [[ "$MODE" == "standalone" ]]; then
  echo ""
  echo ">>> Built: $OUT_DIR/run_rcm.dist/rcm"
  echo "    Ship the entire $OUT_DIR/run_rcm.dist/ directory."
else
  echo ""
  echo ">>> Built: $OUT_DIR/rcm"
fi
