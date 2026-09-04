# rcm — Remote Command MCP Server

A small MCP server that exposes a **fixed allow-list** of shell commands as
named MCP tools, served over Streamable HTTP or stdio.

- Each command in the YAML config becomes one named MCP tool.
- Calling a tool runs the command with `shell=False` (no shell expansion),
  records stdout/stderr to disk, and returns just `run_id` + exit code +
  byte counts + absolute download URLs.
- Stdout/stderr are downloaded over HTTP or HTTPS from
  `/runs/<run_id>/{stdout,stderr,meta}`. These endpoints are intentionally
  **public**; access is gated by the unguessable 256-bit `run_id`
  (capability URLs).
- The HTTP MCP endpoint requires `Authorization: Bearer <RCM_API_KEY>`;
  stdio transport does not use API-key authentication.
- A `proxy` configuration can aggregate local or remote MCP servers and synchronize a local
  workspace before each proxied tool call.

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

# For a local MCP client, use stdio. API key and public_base_url are not needed.
uv run python -m rcm --stdio
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
  transport: http   # http (default) or stdio
  public_base_url: https://rcm.example.com
  # Optional native HTTPS; omit this block for HTTP.
  tls:
    enabled: true
    cert_file: /etc/rcm/server-cert.pem
    key_file: /etc/rcm/server-key.pem

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

## Proxy and combined configurations

Add a `proxy` block to aggregate multiple MCP targets. Each direct child of
`proxy` is a target; there is no additional `targets` configuration layer.
`commands` and `proxy` can be configured together: local commands keep their
original names, while proxied tools are exposed as `<target>__<tool>`.

```yaml
proxy:
  compile:
    transport: ssh
    ssh:
      host: compile-machine
      command: [rcm, --stdio]
    sync:
      source: /home/me/project
      destination: /srv/project
      excludes:
        - .git/**
        - build/**
        - '**/*.pyc'
      delete: false

  # Read the remote config over SSH. The remote server.transport decides how
  # this target is connected; no transport or remote command is repeated here.
  remote_compile:
    ssh:
      host: compile-machine
    config: /etc/rcm/commands.yaml
    sync:
      mappings:
        # Relative destinations are resolved below the remote defaults.cwd.
        - source: /home/me/project/backend
          destination: backend
          excludes: ['**/*.pyc']
          delete: false
        - source: /home/me/project/shared
          destination: shared

  local_tools:
    transport: stdio
    command: [uv, run, my-local-mcp]
    cwd: /home/me/tools

  reports:
    transport: http
    endpoint: https://reports.example.com/mcp
    headers:
      Authorization:
        env: REPORTS_MCP_AUTH
      X-Project:
        value: compile
```

Supported transports are `stdio` (local command), `ssh` (remote command),
`http` (Streamable HTTP), and `sse`. HTTP/SSE headers use exactly one of
`env` or `value`; an environment variable that is missing or empty is an
error, while `value` permits a directly configured header value.

The server's own transport is selected explicitly with `server.transport`.
It defaults to `http` for compatibility with existing configurations. A
proxy target can instead use `ssh` plus an absolute remote `config` path. rcm
reads that file over SSH and inspects its `server.transport`: for `http`, it
connects directly to the remote `server.public_base_url` and does not start a
remote rcm process; for `stdio`, it starts `rcm --stdio`, falling back to
`uvx rcm --stdio` when `rcm` is not on the remote PATH. HTTP connection errors
are returned as errors and do not fall back to stdio.

Every proxied tool is exposed as `<target>__<tool>`, for example
`compile__build`. Current rcm command tools have no additional name prefix.

A configuration must contain at least one of `commands` or `proxy`. A proxy-only
configuration may omit `commands`; a commands-only configuration may omit
`proxy`.

For an explicitly configured target, adding `sync` runs one-way `rsync`
immediately before every `tools/call`. A remote-config target keeps the legacy
behavior of synchronizing the local working directory even when `sync` is
omitted; rcm logs a warning for this implicit full-directory sync. Use
`sync: {enabled: false}` to disable it.

`sync.mappings` accepts one or more independent `source` and `destination`
pairs. They run in order under one target-level lock, and each mapping has its
own `excludes` and `delete` options. A failed mapping stops the remaining
mappings and blocks the remote call. The legacy single-mapping fields directly
under `sync` remain supported, but cannot be mixed with `mappings`.

For `ssh` plus `config`, a relative destination is resolved below the remote
`defaults.cwd`, falling back to the directory containing the remote config.
For an explicit SSH target it is relative to the SSH login directory, matching
native rsync behavior. Local destinations are relative to the proxy process's
working directory. Absolute paths and existing explicit `host:path`
destinations retain their current meaning. Relative destination paths must use
POSIX separators and cannot contain empty, `.`, or `..` components or `~`.

`excludes` are relative POSIX globs and support `*`, `?`, character classes,
and recursive `**`. In addition, rcm always protects the active local config,
the local `RCM_RUNS_DIR` (default `./runs`), a remote config located below the
destination, and the destination's top-level `runs/` directory. These paths
are protected from both transfer and `--delete` and cannot be overridden.
`delete` defaults to `false`. Destinations may overlap when all involved
mappings have deletion disabled; if either overlapping mapping enables
deletion, rcm rejects the target during startup.

The proxy uses the Python `mcp-proxy` bridge for HTTP/SSE targets when
available. SSH credentials are taken from the local OpenSSH configuration,
agent, and keys. The local machine must provide `rsync` for synchronized
targets and `ssh` for SSH targets.

When a proxy starts a stdio or SSH child, it marks the child with an internal
rcm artifact protocol. A child rcm server returns command output as base64;
the local proxy stores it locally and returns local `file://` URLs. Results
from non-rcm MCP services are passed through unchanged.

## HTTPS and self-signed certificates

Native HTTPS is enabled with `server.tls.enabled: true`. The server requires
`server.public_base_url` to use the `https://` scheme with HTTPS transport. For a
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

## Calling from an agent

Configure the MCP client like:

```json
{
  "url": "https://rcm.example.com/mcp/",
  "headers": { "Authorization": "Bearer <RCM_API_KEY>" }
}
```

When `proxy` is configured, use the same endpoint and call remote tools using
their prefixed names, such as `compile__build`. Local command tools remain
available under their original names.

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
| `RCM_API_KEY` | Bearer token for HTTP MCP requests; required for HTTP transport and ignored for stdio transport. |
| `RCM_PUBLIC_BASE_URL` | Public URL used to build HTTP download links. Required for HTTP transport (here or in YAML). |
| `RCM_CONFIG` | Path to YAML config (default: `./commands.yaml`). |
| `RCM_HOST` / `RCM_PORT` | Bind address/port (defaults: `0.0.0.0` / `8000`). |
| `RCM_TLS_ENABLED` | Overrides `server.tls.enabled`. |
| `RCM_TLS_CERT_FILE` / `RCM_TLS_KEY_FILE` | Overrides the configured certificate/key paths. |
| `RCM_TLS_AUTO_GENERATE` | Overrides `server.tls.auto_generate`. |
| `RCM_TLS_HOSTNAMES` | Comma-separated SAN hostnames/IP addresses. |
| `RCM_RUNS_DIR` | Where stdout/stderr/meta are written (default: `./runs`). |
| `RCM_RUNS_RETENTION` | Keep at most N runs on disk (pruned at startup). `0` = keep all. |
| Target header `env` values | Environment variables referenced by proxy target headers are resolved at startup. |

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
- Proxy synchronization still requires the host's `rsync` and, for SSH
  targets, the host's `ssh` client.

## Notes

- Use a TLS-terminating reverse proxy (nginx/caddy) when preferred; configure
  rcm for HTTP in that deployment.
- v1 has a single global API key; per-tool authorization is out of scope.
