# claude-chatter-mcp bridge

`claude-chatter-mcp` bridges MCP-compatible agents to Claude's `SendMessage`/`ListAgents` implementation, allowing bidirectional communication - built for **Google Antigravity** and **Claude Code**, non-blocking and authenticated.

## 🚀 Key Features

* **Session Discovery & Automatic Purging**: Scans local workspace configurations (`~/.claude/sessions/*.json`), verifies process lifecycles via PID tracking (`psutil`), and automatically purges orphaned session descriptors and stale `.key` credential files.
* **Authenticated Unix Socket IPC**: Authenticates outbound message frames directly into active target session sockets (`/run/user/<uid>/cc-socks/*.sock`) using exfiltrated `peerToken` credentials.
* **Fire-and-Forget Message Dispatch**: `send_message`/`--send` dispatches and returns immediately; a real reply, if the target sends one, arrives later as its own inbound message rather than being synchronously extracted from a transcript file.
* **Dedicated Inbound Unix Socket Listener**: Listens on `/tmp/agy_mcp_bridge.sock` (with secure single-user `0600` permissions) for real-time peer confirmations and inbound message buffering.
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
# 1. Put bridge-mcp on PATH (the plugin's MCP server and hook both run it by name)
uv tool install git+https://github.com/non7top/claude-chatter-mcp.git   # or: uv tool install --editable .

# 2. Install the plugin
agy plugin install https://github.com/non7top/claude-chatter-mcp          # or: agy plugin install .
```

If you registered the server by hand earlier (`agy mcp add ...`), remove it so the plugin's copy is the only one.

---

## 🔔 Waking an idle Antigravity session (push)

`agy` cannot be woken by an MCP server, so by default it only sees inbound messages when it calls `read_messages`. Push closes that gap using `agy agentapi send-message`, which needs `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN`. `agy` gives those only to its agent's shell tool, never to MCP servers, so each conversation has to be **primed** once:

1. The agent calls the `prime_push` tool, which returns a shell command (`bridge-mcp --prime`).
2. The agent runs that command in its shell tool. It reads its own `ANTIGRAVITY_*` environment and hands it to its bridge over a `0600` control socket (`<bridge socket>.ctl`). The token is kept in memory only and passed to `agy` through the child's environment, never argv, logs or disk.
3. From then on every inbound message makes the bridge run `agy agentapi send-message` with a fixed notice ("N new message(s) in your bridge inbox, call `read_messages`"). No sender-written text is injected.

With the plugin installed, the `PreInvocation` hook (`bridge-mcp --prime-hook`) asks `agy` to run the priming command for any conversation that isn't primed yet, so the agent doesn't have to decide to. Whether `agy` honours that injected step is not yet verified; `rules/claude-chatter.md` and the `read_messages` `push_hint` are the fallbacks.

---

## 🧰 MCP Tool Reference

The MCP server exposes these tools:

### 1. `list_sessions`
Scans Claude Code workspace configurations, cleans up orphaned/dead session files, and returns all active Claude sessions.
* **Arguments**: None

### 2. `send_message`
Dispatches an authenticated user message frame to a target Claude Code session. Fire-and-forget: returns as soon as the message is delivered. Any real reply the target sends back arrives later as its own inbound message, observable via `get_responses`.
* **Arguments**:
  * `session` (string, required): Target session name (e.g. `rgle`), PID, or Session ID.
  * `message` (string, required): Text message content to deliver.

### 3. `get_responses`
Queries historical inbound and outbound message response records cached in memory.
* **Arguments**:
  * `msg_id` (string, optional): Specific message ID filter.

### 4. `read_messages`
Returns `{"messages": {...}}` with unread inbound messages (oldest first) and marks them read. Adds a `push_hint` when running under `agy` without push primed.
* **Arguments**:
  * `mark_read` (boolean, optional, default true): set false to peek without consuming.

### 5. `prime_push`
Returns the shell command that primes push for this conversation (see above) and whether push is already primed.
* **Arguments**: None

### 6. `purge_sessions`
Manually triggers proactive scanning and deletion of dead socket descriptors and key files.
* **Arguments**: None

### 7. `rename_session`
Renames this bridge's announced session descriptor in `~/.claude/sessions/` so surrounding Claude Code processes discover it under the new name via `ListAgents`.
* **Arguments**:
  * `new_name` (string, required): The new session name to announce (e.g. `bridge-dev`). Without a rename, the name defaults to `bridge-<cwd basename>`; override it with `--name` or `CHATTER_SESSION_NAME`.

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
* **Send Message to Session** (fire-and-forget; any reply arrives later as its own inbound message):
  ```bash
  bridge-mcp --send rgle "Hello from CLI"
  ```

---

## ⚙️ Running Tests

Run the included unit and integration test suites:

```bash
python3 test_suite.py
```

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
