# claude-chatter-mcp bridge

`claude-chatter-mcp` lets MCP-compatible agents (built for **Google Antigravity**) exchange messages with Claude Code sessions. It speaks Claude Code's peer-messaging protocol (`SendMessage`/`ListAgents`): the bridge announces itself as a session and talks to other sessions over their Unix sockets. Non-blocking and authenticated.

The protocol, storage and CLI core lives in the [`claude-chatter`](https://pypi.org/project/claude-chatter/) library (a dependency of this package); this repo adds the MCP server, the `agy` push priming and the plugin files on top.

It ships three ways: as a plain MCP server (`bridge-mcp`), as a standalone daemon/CLI, and as an **Antigravity CLI (`agy`) plugin** that bundles the MCP server, a `PreInvocation` hook that primes push, a rules file and the `chatter` skill (see [Option 4](#option-4-antigravity-plugin-mcp-server--push-priming)).

## 🚀 Key Features

* **Session Discovery & Automatic Purging**: Scans local workspace configurations (`~/.claude/sessions/*.json`), verifies process lifecycles via PID tracking (`psutil`), and automatically purges orphaned session descriptors and stale `.key` credential files.
* **Authenticated Unix Socket IPC**: Authenticates outbound message frames directly into active target session sockets (`/run/user/<uid>/cc-socks/*.sock`) using the `peerToken` read from each session's `.key` file.
* **Fire-and-Forget Message Dispatch**: `send_message`/`--send` dispatches and returns immediately; a real reply, if the target sends one, arrives later as its own inbound message rather than being synchronously extracted from a transcript file.
* **Dedicated Inbound Unix Socket Listener**: Listens on a per-session socket (default `/run/user/<uid>/cc-socks/bridge-<name>.sock`, falling back to `/tmp/cc-socks/`; override with `--socket` or `CHATTER_BRIDGE_SOCKET`; single-user `0600` permissions) for real-time peer confirmations and inbound message buffering.
* **Push to Antigravity**: `enable_push` / `bridge-mcp --prime` lets an inbound message wake an idle `agy` conversation through `agy agentapi send-message`, so the agent does not have to poll `read_messages` (see [Waking an idle Antigravity session](#-waking-an-idle-antigravity-session-push)).
* **Background Job Listing**: `list_jobs` / `--jobs` reads `~/.claude/jobs` to report every background job, including finished ones that have no live process, with state, result text, PR states, worktree, running status and transcript path.
* **Graceful Lifecycle & Signal Handling**: Catches `SIGTERM` and `SIGINT` signals to cleanly unbind sockets and exit with status code `0`, avoiding process termination errors during MCP server reloads.

---

## 🛠️ Architecture

```
┌──────────────────────┐             ┌──────────────────────┐
│  Google Antigravity  │             │     Claude Code      │
│     (Prose / agy)    │             │   (Execution / CLI)  │
└──────────┬───────────┘             └──────────▲───────────┘
           │                                    │
    (MCP Tool Calls)                 (Pushed NDJSON Stream)
           │                                    │
┌──────────▼────────────────────────────────────┴───────────┐
│                 SOCKET BRIDGE MCP SERVER                  │
│  ┌───────────────────────┐    ┌─────────────────────────┐ │
│  │ Response History Cache│    │  Active Session Monitor │ │
│  │     (In-Memory)       │    │  (Dead Socket Purging)  │ │
│  └───────────────────────┘    └─────────────────────────┘ │
│  ┌───────────────────────┐    ┌─────────────────────────┐ │
│  │   Inbound IPC Server  │    │ Outbound Socket Manager │ │
│  │   Listener Daemon     │    │   (Key-Authenticated)   │ │
│  └───────────────────────┘    └─────────────────────────┘ │
└───────────────────────────────────────────────────────────┘
```

---

## 📦 Installation & Setup

### Option 1: Via `uvx` or `uv tool run` (Zero-Installation / Ephemeral)

Register directly with **Antigravity CLI (`agy`)** from GitHub without cloning:

**Using `uvx`:**
```bash
agy mcp add claudemessaging uvx --from git+https://github.com/non7top/claude-chatter-mcp.git bridge-mcp
```

**Using `uv tool run`:**
```bash
agy mcp add claudemessaging uv tool run --from git+https://github.com/non7top/claude-chatter-mcp.git bridge-mcp
```

Or run directly in terminal:
```bash
uvx --from git+https://github.com/non7top/claude-chatter-mcp.git bridge-mcp
```

---

### Option 2: Permanent Tool Installation (`uv tool install`)

Install the `bridge-mcp` binary permanently into your system path using `uv`:

```bash
# 1. Install tool permanently into ~/.local/bin (or uv tool path)
uv tool install git+https://github.com/non7top/claude-chatter-mcp.git

# 2. Register with Antigravity CLI
agy mcp add claudemessaging bridge-mcp
```

---

### Option 3: Local Repository Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/non7top/claude-chatter-mcp.git
   cd claude-chatter-mcp
   ```

2. **Register with Antigravity CLI (`agy`) using `uv` / `uvx`**:
   ```bash
   agy mcp add claudemessaging uvx --from . bridge-mcp
   ```

3. **Or register with standard `python3`**:
   ```bash
   agy mcp add claudemessaging python3 $(pwd)/bridge_mcp.py
   ```

4. **Verify registration**:
   ```bash
   agy mcp list
   ```

---

### Option 4: Antigravity plugin (MCP server + push priming)

This repo is also an Antigravity CLI plugin: `plugin.json`, `mcp_config.json` (the MCP server), `hooks.json` (a `PreInvocation` hook that primes push, see below) and `rules/claude-chatter.md`.

```bash
agy plugin install https://github.com/non7top/claude-chatter-mcp
```

Only `uv` needs to be installed: the plugin's MCP server and hook run `uvx --from git+https://github.com/non7top/claude-chatter-mcp bridge-mcp`, which fetches and caches the bridge on first use.

If you registered the server by hand earlier (`agy mcp add ...`), remove it so the plugin's copy is the only one.

---

## 🔔 Waking an idle Antigravity session (push)

`agy` cannot be woken by an MCP server, so by default it only sees inbound messages when it calls `read_messages`. Push closes that gap using `agy agentapi send-message`, which needs `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN`. `agy` gives those only to its agent's shell tool, never to MCP servers, so each conversation has to be **primed** once:

1. The agent calls the `enable_push` tool, which returns a shell command (`bridge-mcp --prime`).
2. The agent runs that command in its shell tool. It reads its own `ANTIGRAVITY_*` environment and hands it to its bridge over a `0600` control socket (`<bridge socket>.ctl`). The token is kept in memory only and passed to `agy` through the child's environment, never argv, logs or disk.
3. From then on every inbound message makes the bridge run `agy agentapi send-message` with a fixed notice ("N new message(s) in your bridge inbox, call `read_messages`"). No sender-written text is injected.

With the plugin installed, the `PreInvocation` hook (`bridge-mcp --prime-hook`) asks `agy` to run the priming command for any conversation that isn't primed yet, so the agent doesn't have to decide to. Whether `agy` honours that injected step is not yet verified; `rules/claude-chatter.md` and the `read_messages` `push_hint` are the fallbacks.

---

## 🧰 MCP Tool Reference

The MCP server exposes these tools:

### 1. `list_sessions`
Scans Claude Code workspace configurations, cleans up orphaned/dead session files, and returns all active Claude sessions (name, pid, session ID, socket, cwd, kind, status). Auth tokens are not included in the output.
* **Arguments**: None

### 2. `list_jobs`
Reads `~/.claude/jobs/*/state.json` and returns one entry per background job, finished ones included: `state`, `detail`, `output` (the final result text the fleet list shows), `needs`, PR `children`, `cwd`, worktree path/branch, last timeline entry, transcript path and mtime (the path only; the file is never read), `running` (the daemon roster has a live worker for the job), `live` (pid/kind/status when a session process is attached via `jobId`), and `pr_state` per PR link from Claude Code's `gh-pr-status-cache.json`. Read-only; also `bridge-mcp --jobs`.
* **Arguments**: None

### 3. `send_message`
Dispatches an authenticated user message frame to a target Claude Code session. Fire-and-forget: returns as soon as the message is delivered. Any real reply the target sends back arrives later as its own inbound message, observable via `get_responses`.
* **Arguments**:
  * `session` (string, required): Target session name (e.g. `rgle`), PID, or Session ID.
  * `message` (string, required): Text message content to deliver.

### 4. `read_messages`
Returns unread inbound messages (oldest first), keyed by message ID, and marks them read. Inbound and outbound messages are persisted in a local SQLite database (`~/.claude/chatter-messages.db`), so unread messages and message history survive bridge and host restarts. Push can wake a primed Antigravity session when one arrives, but it is best effort, so also call this periodically or after a `tools/list_changed` notification.
* **Arguments**:
  * `mark_read` (boolean, optional, default true): set false to peek without consuming.

### 5. `get_responses`
Queries inbound and outbound message history persisted across bridge restarts (newest first).
* **Arguments**:
  * `msg_id` (string, optional): Specific message ID filter.
  * `limit` (integer, optional): Maximum number of messages to return (newest first).
  * `offset` (integer, optional, default 0): Number of messages to skip when paginating.

### 6. `enable_push`
Returns the shell command that primes push for this conversation (see above) and whether push is already primed.
* **Arguments**: None

### 7. `purge_sessions`
Manually triggers proactive scanning and deletion of dead socket descriptors and key files.
* **Arguments**: None

### 8. `rename_session`
Renames this bridge's announced session descriptor in `~/.claude/sessions/` so surrounding Claude Code processes discover it under the new name via `ListAgents`. Tracks stable session identity and alias history in SQLite across renames, safely merging message histories without collisions. When renaming onto a session name with existing message history in storage, duplicate message IDs retain the renamed session's copy. Renaming is rejected if the target name is currently held by an active session.
* **Arguments**:
  * `new_name` (string, required): The new session name to announce (e.g. `bridge-dev`). Without a rename, the name defaults to `<project basename>-<2 random alphanumerics>`, generated once and stored per project path in `~/.claude/chatter-session-names/`; a rename overwrites the stored name. Under `agy` the project path is the agy process's working directory, because `agy` does not expose its conversation title to MCP servers, the shell environment or the status line payload (requested upstream: [google-antigravity/antigravity-cli#1415](https://github.com/google-antigravity/antigravity-cli/issues/1415)). Override with `--name` or `CHATTER_SESSION_NAME`.

---

## 🖥️ Standalone Daemon & CLI Mode

In addition to standard stdio MCP mode, `bridge_mcp.py` can be launched in **standalone mode** (as a persistent background daemon or command-line execution tool):

### Standalone Daemon Server
Run the bridge as a background service without stdio MCP binding:
```bash
bridge-mcp --standalone --name custom-bridge-name
```
Or with `python3`:
```bash
python3 bridge_mcp.py --standalone --name custom-bridge-name
```

### Command-Line Execution Tools
Execute single-shot operations directly from terminal:

* **List Active Sessions**:
  ```bash
  bridge-mcp --list
  ```
* **Proactively Purge Dead Sessions**:
  ```bash
  bridge-mcp --purge
  ```
* **List Background Jobs** (finished ones included; read-only apart from the session scan that precedes it):
  ```bash
  bridge-mcp --jobs
  ```
* **Check Inbox** (displays unread inbound messages without marking them read):
  ```bash
  bridge-mcp --inbox
  ```
* **View Message History**:
  ```bash
  bridge-mcp --history
  ```
* **Send Message to Session** (fire-and-forget; any reply arrives later as its own inbound message):
  ```bash
  bridge-mcp --send rgle "Hello from CLI"
  ```

---

## 🔧 Operations, Security and Limits

**Files the bridge reads or writes** (all under `~/.claude/`):
* `sessions/` - Claude Code's session descriptors and `.key` files; the bridge reads them and writes its own descriptor and key.
* `chatter-messages.db` - SQLite database storing inbound and outbound messages, tracking read/unread status across bridge restarts. Mode 0600. Read and outbound messages are kept for 30 days (capped at 1,000 per session); unread inbound messages are preserved up to 90 days. In SQLite WAL mode, read-only inspect commands (`--inbox`, `--history`) access the database with `mode=ro` and do not alter stored data, though SQLite may create empty `.db-wal` and `.db-shm` files for WAL index coordination.
* `logs/claude-chatter.log` - shared by all bridge processes (also written to stderr); rotates at 5 MiB, 3 backups.
* `chatter-session-names/` - the generated or renamed session name and stable session ID per project path.
* `chatter-primed/` - which Antigravity conversations are primed for push, plus a retry marker (a failed priming is retried after 10 minutes).
* `chatter-bin/bridge-mcp` - a stable symlink to this install, so the prime command can be allow-listed.

**Settings:** `--name` or `CHATTER_SESSION_NAME` (alias `AGY_SESSION_NAME`) sets the session name; `--socket` or `CHATTER_BRIDGE_SOCKET` (alias `AGY_MCP_BRIDGE_SOCKET`) sets the bridge socket. `AGY_WAKEUP_PIN_AGY=1` makes push stop, and drop the stored credential, once the agy process that was primed has ended or its pid has been reused (off by default).

**Packages:** the Antigravity push logic (prime command, `PreInvocation` hook, shell-side primer, control socket, `agentapi send-message` with delivery confirmation) lives in the self-contained `agy_wakeup` package in this repo, which does not import `claude_chatter_mcp`; the `agy-wakeup` command exposes its `--prime` and `--prime-hook` modes, and `claude_chatter_mcp/agy_glue.py` configures it for this bridge.

**Security model:**
* Peer tokens are read from each session's `.key` file and only used to authenticate outbound frames. `list_sessions` and `--list` do not print them.
* The bridge's own key file and its sockets are created with `0600` permissions.
* The Antigravity token used for push is held in memory only, passed to `agy` through its environment (never argv, logs or disk), and only accepted together with a loopback address.
* Push injects a fixed notice, never text written by the sender.

**Limits:**
* The bridge works on the real `~/.claude`, which is why it runs host-native and only the tests run in a container with an isolated home.
* Developed on Linux: the default socket directory is `/run/user/<uid>/cc-socks` (fallback `/tmp/cc-socks`), and process start time and PID namespace come from `/proc` and `/etc/machine-id`, with placeholder values when those are unreadable.
* `list_jobs` reads Claude Code's own job, roster and PR-cache files. Their layout is not a documented interface and may change; unparseable files are skipped.
* Replies are asynchronous, and an idle Claude Code session may not answer until someone prompts it.
* Push is best effort; keep calling `read_messages`. See [docs/agy-notes.md](docs/agy-notes.md) for the Antigravity details.

---

## ⚙️ Running Tests

Run the included unit and integration test suites in a disposable non-root container:

```bash
make test
```

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
