# rcm — Remote Command MCP Server

A small MCP server that exposes a **fixed allow-list** of shell commands as
named MCP tools, served over Streamable HTTP or stdio.

- Each command in the YAML config becomes one named MCP tool.
- Calling a tool runs the command with `shell=False` (no shell expansion),
  records stdout/stderr to disk, optionally collects configured output files,
  and returns an `rcm.run-result/v2` object with a URI, byte count, and SHA-256
  digest for each artifact.
- Artifacts are downloaded over HTTP or HTTPS from
  `/runs/<run_id>/{stdout,stderr,collect,meta}`. These endpoints are intentionally
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
# Output download: $RCM_PUBLIC_BASE_URL/runs/<run_id>/{stdout,stderr,collect,meta}

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

  - name: build_report
    description: Build a report and collect its output.
    command: [python, scripts/build_report.py]
    cwd: /srv/project
    collect:
      on_exit: success
      mode: changed
      paths:
        - path: dist/**/*.pdf
          required: true
        - path: test-results
          on_exit: always
          mode: always
          required: false
```

Rules:

- `command` must be a **list** (argv form). String form is rejected.
- `{name}` placeholders may only appear inside argv elements and must be
  declared in `params`.
- `params[*].type` is one of `string`, `integer`, `number`, `boolean`.
- Optional per-param: `description`, `default`, `pattern` (regex), `enum`.
- `name` must match `^[a-zA-Z_][a-zA-Z0-9_]*$` and be globally unique.

### Collecting command files

`commands[*].collect.paths` is an allow-list of files and directories to pack.
Each path is relative to the command's effective `cwd`. POSIX `*`, `?`, `[]`,
and complete `**` path segments are supported; absolute paths, `..`, backslashes,
and malformed `**` segments are rejected. Glob matches include both files and
directories. A matched directory is collected recursively as one unit while
preserving its relative layout.

`collect.on_exit` controls which process outcomes are eligible: `success`
(default) requires exit code zero without a timeout, while `always` also handles
non-zero exits and timeouts after the process has started. `collect.mode` is
`always` by default; `changed` publishes a path only when its type, metadata,
link target, contents, or recursive directory tree differs from the pre-command
snapshot. Metadata is checked first; when it is unchanged, SHA-256 content
fingerprints provide the final comparison. Both settings can be overridden on
an individual path.

`required` defaults to `false`. A missing or unreadable optional path is skipped
and recorded in the result's `warnings`. If a required path is unavailable, no
collect archive is published, but the command result and stdout/stderr remain
available. An unchanged required path still satisfies `required`; if no rule
selects content, an empty archive is not published. Deleted changed paths are
reported as warnings.

The collect artifact is a gzip-compressed tar served from
`/runs/<run_id>/collect` with media type `application/gzip`. Compression uses
gzip level 6. To avoid leaking host identities, every tar member has UID/GID
normalized to `0/0`, empty user/group names, and no ownership-related PAX
headers. Symbolic links are archived without following them. The active RCM
configuration and `RCM_RUNS_DIR` are always excluded.

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
    # Optional for an RCM v2 target. Default: localize.
    artifacts: passthrough
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

HTTP/SSE targets use the native MCP client transports. SSH credentials are
taken from the local OpenSSH configuration, agent, and keys. The local machine
must provide `rsync` for synchronized targets and `ssh` for SSH targets.

RCM servers advertise the v2 artifact capability and supported artifact kinds
during MCP initialization.
For another RCM, `artifacts` defaults to `localize`: a local stdio proxy reads
the returned `file://` URI directly, an SSH proxy streams the file over the
same SSH host, and an HTTP proxy downloads the returned URL. The proxy verifies
the declared size and SHA-256 of every returned artifact, creates a new local
run, and returns URIs appropriate for its own server transport (`file://` for
stdio, HTTP URLs for HTTP). Stdout, stderr, and collect use this same byte-for-byte
copy and validation path; collect is never decompressed or recompressed by a
proxy. No command output is embedded in the MCP tool response.

An HTTP RCM target can instead set `artifacts: passthrough`. Its run ID and
HTTP artifact URLs are then returned without a local copy, even when the outer
RCM uses stdio. `passthrough` is rejected for stdio, SSH, and SSE targets. An
explicit `artifacts` setting also requires the target to identify itself as an
RCM v2 server. Results from ordinary MCP services are always passed through
unchanged. Targets discovered through `ssh` plus `config` are RCM-specific and
therefore also require the remote server to support v2.

`collect` is an additive v2 field. Proxies from before collect support continue
to handle stdout/stderr, but may omit collect while rebuilding a localized
result. Upgrade every RCM in a proxy chain when collected files must reach the
outermost caller.

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

A successful call to a command with `collect` configured returns:

```json
{
  "schema": "rcm.run-result/v2",
  "run_id": "k7Q...",
  "returncode": 0,
  "timed_out": false,
  "duration_ms": 42,
  "stdout": {
    "uri": "https://rcm.example.com/runs/k7Q.../stdout",
    "bytes": 7321,
    "sha256": "..."
  },
  "stderr": {
    "uri": "https://rcm.example.com/runs/k7Q.../stderr",
    "bytes": 0,
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  },
  "collect": {
    "uri": "https://rcm.example.com/runs/k7Q.../collect",
    "bytes": 10240,
    "sha256": "..."
  }
}
```

Then download the output (no auth header needed; the `run_id` is the secret):

```bash
curl https://rcm.example.com/runs/k7Q.../stdout
curl https://rcm.example.com/runs/k7Q.../stderr
curl -o collect.tar.gz https://rcm.example.com/runs/k7Q.../collect
tar -tzf collect.tar.gz
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
| `RCM_RUNS_DIR` | Where stdout/stderr/collect/meta are written (default: `./runs`). |
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
