# rcm — Remote Command MCP Server

A small MCP server that exposes a **fixed allow-list** of shell commands as
named MCP tools, served over **HTTP Streaming** (Streamable HTTP transport).

- Each command in the YAML config becomes one named MCP tool.
- Calling a tool runs the command with `shell=False` (no shell expansion),
  records stdout/stderr to disk, and returns just `run_id` + exit code +
  byte counts + absolute download URLs.
- Stdout/stderr are downloaded over HTTP or HTTPS from
  `/runs/<run_id>/{stdout,stderr,meta}`. These endpoints are intentionally
  **public**; access is gated by the unguessable 256-bit `run_id`
  (capability URLs).
- The MCP endpoint itself requires `Authorization: Bearer <RCM_API_KEY>`.
- An optional WebDAV share can be mounted on the same HTTP server.

## Quickstart

```bash
# Install uv: https://docs.astral.sh/uv/getting-started/installation/
uv sync

cp commands.example.yaml commands.yaml
# Edit commands.yaml and set server.public_base_url, or:
export RCM_PUBLIC_BASE_URL=https://rcm.example.com

export RCM_API_KEY=$(uv run python -c 'import secrets;print(secrets.token_urlsafe(32))')
echo "API key: $RCM_API_KEY"

uv run python -m rcm
# MCP endpoint:    $RCM_PUBLIC_BASE_URL/mcp/
# Output download: $RCM_PUBLIC_BASE_URL/runs/<run_id>/{stdout,stderr,meta}
```

After installing or publishing the package, the CLI can also be invoked from
any directory with uvx:

```bash
RCM_CONFIG=/etc/rcm/commands.yaml \
RCM_RUNS_DIR=/var/lib/rcm/runs \
uvx rcm
```

To run directly from a local checkout or Git repository:

```bash
uvx --from /opt/rcm rcm
uvx --from git+https://github.com/example/rcm rcm
```

The `rcm` command is declared in `pyproject.toml` as the entry point for
`rcm.server:main`.

Run the test suite with:

```bash
uv run pytest
```

## Configuring commands

```yaml
server:
  host: 0.0.0.0
  port: 8000
  public_base_url: https://rcm.example.com
  # Optional native HTTPS; omit this block for HTTP.
  tls:
    enabled: true
    cert_file: /etc/rcm/server-cert.pem
    key_file: /etc/rcm/server-key.pem

webdav:
  root: /srv/rcm-files
  path: /webdav/
  read_only: true
  auth:
    type: bearer

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

## HTTPS and self-signed certificates

Native HTTPS is enabled with `server.tls.enabled: true`. The server requires
`server.public_base_url` to use the `https://` scheme in this mode. For a
manually managed certificate, provide both `cert_file` and `key_file`; paths
relative to the YAML file are resolved relative to that file.

For a self-signed certificate, let rcm generate and reuse one beside the
configuration file:

```yaml
server:
  public_base_url: https://rcm.example.com
  tls:
    enabled: true
    auto_generate: true
    # Optional additional DNS names or IP addresses.
    hostnames: [rcm.internal.example.com, 192.168.1.20]
```

The generated files are `.rcm/rcm-cert.pem` and `.rcm/rcm-key.pem`. Import
`rcm-cert.pem` into the client host's trust store (or configure it as the
client's CA file), for example:

```bash
curl --cacert .rcm/rcm-cert.pem https://rcm.example.com/healthz
```

When `public_base_url` is present, its hostname is included in the generated
certificate SANs. If it is unavailable, configure at least one value in
`server.tls.hostnames`. `localhost` and `127.0.0.1` are included automatically.
Do not disable certificate verification globally in clients; trust the
generated certificate explicitly instead.

When rcm runs behind a TLS-terminating reverse proxy, leave `server.tls`
disabled. rcm then listens on HTTP while `public_base_url` can remain an
`https://` URL for links returned to clients.

## WebDAV

WebDAV is enabled when the `webdav` section is present. The root must already
exist and be a directory. The default path is `/webdav/`, the default mode is
read-only, and the default authentication type is `bearer`, which reuses
`RCM_API_KEY`.

Authentication types:

- `bearer`: use `Authorization: Bearer <RCM_API_KEY>`.
- `basic`: provide `username` and `password` under `webdav.auth`. The password
  can be overridden with `RCM_WEBDAV_PASSWORD`.
- `none`: allow anonymous access.

The WebDAV path cannot overlap `/mcp`, `/runs`, or `/healthz`. Enable
`read_only: false` only for a directory that is safe for remote modification.
Use TLS directly or place a TLS-terminating reverse proxy in front of the
service, especially when using Basic authentication.

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
| `RCM_TLS_ENABLED` | Overrides `server.tls.enabled`. |
| `RCM_TLS_CERT_FILE` / `RCM_TLS_KEY_FILE` | Overrides the configured certificate/key paths. |
| `RCM_TLS_AUTO_GENERATE` | Overrides `server.tls.auto_generate`. |
| `RCM_TLS_HOSTNAMES` | Comma-separated SAN hostnames/IP addresses. |
| `RCM_RUNS_DIR` | Where stdout/stderr/meta are written (default: `./runs`). |
| `RCM_RUNS_RETENTION` | Keep at most N runs on disk (pruned at startup). `0` = keep all. |
| `RCM_WEBDAV_PASSWORD` | Overrides the configured WebDAV Basic-auth password. |

## Building a standalone binary (Nuitka)

You can compile rcm into a self-contained binary with Nuitka. The result is a
`build/run_rcm.dist/` directory that can be copied to any same-OS/arch machine
without needing Python installed.

```bash
# Install the locked runtime and build dependencies
uv sync

# Build (takes ~5-10 minutes on first run; ccache speeds up rebuilds)
./build.sh                  # standalone (default)
./build.sh onefile          # single-file binary

# Run the compiled server
RCM_API_KEY=... RCM_PUBLIC_BASE_URL=https://rcm.example.com \
  RCM_CONFIG=commands.yaml \
  ./build/run_rcm.dist/rcm
```

`build.sh` uses the uv-managed environment and automatically probes which
packages are installed and includes them. Key caveats:

- `importlib_metadata` is included in the uv development dependency group
  because Nuitka's anti-bloat rewrites `opentelemetry` imports to use it.
- Python 3.14 support in Nuitka is experimental; 3.12-3.13 are safer.
- For onefile mode, add `--onefile-tempdir-spec={CACHE_DIR}/rcm` to avoid
  re-extracting on every launch (already set in `build.sh`).

## Notes

- Use a TLS-terminating reverse proxy (nginx/caddy) when preferred; configure
  rcm for HTTP in that deployment.
- v1 has a single global API key; per-tool authorization is out of scope.
