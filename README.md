# rcm — Remote Command MCP Server

A small MCP server that exposes a **fixed allow-list** of shell commands as
named MCP tools, served over **HTTP Streaming** (Streamable HTTP transport).

- Each command in the YAML config becomes one named MCP tool.
- Calling a tool runs the command with `shell=False` (no shell expansion),
  records stdout/stderr to disk, and returns just `run_id` + exit code +
  byte counts + absolute download URLs.
- Stdout/stderr are downloaded over plain HTTP from
  `/runs/<run_id>/{stdout,stderr,meta}`. These endpoints are intentionally
  **public**; access is gated by the unguessable 256-bit `run_id`
  (capability URLs).
- The MCP endpoint itself requires `Authorization: Bearer <RCM_API_KEY>`.

## Quickstart

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

cp commands.example.yaml commands.yaml
# Edit commands.yaml and set server.public_base_url, or:
export RCM_PUBLIC_BASE_URL=https://rcm.example.com

export RCM_API_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
echo "API key: $RCM_API_KEY"

python -m rcm
# MCP endpoint:    $RCM_PUBLIC_BASE_URL/mcp/
# Output download: $RCM_PUBLIC_BASE_URL/runs/<run_id>/{stdout,stderr,meta}
```

## Configuring commands

```yaml
server:
  host: 0.0.0.0
  port: 8000
  public_base_url: https://rcm.example.com

defaults:
  timeout: 30
  cwd: /var/log

commands:
  - name: tail_log
    description: Tail the last N lines of a log file under /var/log.
    command: ["tail", "-n", "{lines}", "/var/log/{file}"]
    params:
      lines: { type: integer, default: 100 }
      file:
        type: string
        pattern: '^[A-Za-z0-9._-]+$'
```

Rules:

- `command` must be a **list** (argv form). String form is rejected.
- `{name}` placeholders may only appear inside argv elements and must be
  declared in `params`.
- `params[*].type` is one of `string`, `integer`, `number`, `boolean`.
- Optional per-param: `description`, `default`, `pattern` (regex), `enum`.
- `name` must match `^[a-zA-Z_][a-zA-Z0-9_]*$` and be globally unique.

## Calling from an agent

Configure the MCP client like:

```json
{
  "url": "https://rcm.example.com/mcp/",
  "headers": { "Authorization": "Bearer <RCM_API_KEY>" }
}
```

A `tools/call` for `tail_log` with `{lines: 100, file: "nginx.log"}` returns:

```json
{
  "run_id": "k7Q...",
  "returncode": 0,
  "timed_out": false,
  "duration_ms": 42,
  "stdout_bytes": 7321,
  "stderr_bytes": 0,
  "stdout_url": "https://rcm.example.com/runs/k7Q.../stdout",
  "stderr_url": "https://rcm.example.com/runs/k7Q.../stderr"
}
```

Then download the output (no auth header needed; the `run_id` is the secret):

```bash
curl https://rcm.example.com/runs/k7Q.../stdout
curl https://rcm.example.com/runs/k7Q.../stderr
curl 'https://rcm.example.com/runs/k7Q.../stdout?tail=4096'
```

## Environment variables

| Variable | Purpose |
|---|---|
| `RCM_API_KEY` | Bearer token required for MCP requests. Required. |
| `RCM_PUBLIC_BASE_URL` | Public URL used to build absolute download links. Required (here or in YAML). |
| `RCM_CONFIG` | Path to YAML config (default: `./commands.yaml`). |
| `RCM_HOST` / `RCM_PORT` | Bind address/port (defaults: `0.0.0.0` / `8000`). |
| `RCM_RUNS_DIR` | Where stdout/stderr/meta are written (default: `./runs`). |
| `RCM_RUNS_RETENTION` | Keep at most N runs on disk (pruned at startup). `0` = keep all. |

## Building a standalone binary (Nuitka)

You can compile rcm into a self-contained binary with Nuitka. The result is a
`build/run_rcm.dist/` directory that can be copied to any same-OS/arch machine
without needing Python installed.

```bash
# Install build dependency (Nuitka + importlib_metadata shim)
pip install 'nuitka>=2.4' importlib_metadata

# Build (takes ~5-10 minutes on first run; ccache speeds up rebuilds)
./build.sh                  # standalone (default)
./build.sh onefile          # single-file binary

# Run the compiled server
RCM_API_KEY=... RCM_PUBLIC_BASE_URL=https://rcm.example.com \
  RCM_CONFIG=commands.yaml \
  ./build/run_rcm.dist/rcm
```

`build.sh` automatically probes which packages are installed and includes
them. Key caveats:

- `importlib_metadata` (third-party) must be installed because Nuitka's
  anti-bloat rewrites `opentelemetry` imports to use it.
- Python 3.14 support in Nuitka is experimental; 3.12-3.13 are safer.
- For onefile mode, add `--onefile-tempdir-spec={CACHE_DIR}/rcm` to avoid
  re-extracting on every launch (already set in `build.sh`).

## Notes

- Put a TLS-terminating reverse proxy (nginx/caddy) in front in production.
- v1 has a single global API key; per-tool authorization is out of scope.
