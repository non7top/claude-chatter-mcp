#!/usr/bin/env python3
"""
Comprehensive Test Suite for Claude Message Bridge MCP Server (claude-mcp2)
"""
import os
import re
import sys
import json
import time
import glob
import uuid
import hashlib
import asyncio
import tempfile
import atexit
import unittest
from unittest import mock
import subprocess
import io

# Isolate host-native test runs from real ~/.claude/chatter-messages.db
_suite_tmp_db_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("CHATTER_DB_PATH", os.path.join(_suite_tmp_db_dir.name, "test_suite_default.db"))
atexit.register(_suite_tmp_db_dir.cleanup)

# Ensure repo directory is in path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from claude_chatter import ClaudeClient


class TestMcpToolsOnClient(unittest.IsolatedAsyncioTestCase):
    """MCP tool behavior on top of a real claude-chatter ClaudeClient."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_messages.db")
        self.sock_path = os.path.join(self.tmp_dir.name, "test_bridge.sock")
        self.session_name = "test-session-persist"

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def test_get_responses_pagination_via_mcp_server(self):
        from claude_chatter_mcp.mcp_server import build_mcp_server
        bridge = ClaudeClient(
            bridge_socket_path=self.sock_path,
            session_name=self.session_name,
            db_path=self.db_path
        )
        for i in range(1, 6):
            bridge.response_store[f"msg_{i}"] = {
                "direction": "inbound",
                "content": f"msg {i}",
                "timestamp": float(i)
            }
        server = build_mcp_server(bridge)
        get_responses_tool = await server.get_tool("get_responses")
        self.assertIsNotNone(get_responses_tool)
        get_responses_fn = get_responses_tool.fn

        single = json.loads(await get_responses_fn(msg_id="msg_3"))
        self.assertEqual(single["content"], "msg 3")

        all_res = json.loads(await get_responses_fn())
        self.assertEqual(len(all_res), 5)
        self.assertEqual(list(all_res.keys()), ["msg_5", "msg_4", "msg_3", "msg_2", "msg_1"])

        p1 = json.loads(await get_responses_fn(limit=2))
        self.assertEqual(list(p1.keys()), ["msg_5", "msg_4"])

        p2 = json.loads(await get_responses_fn(limit=2, offset=2))
        self.assertEqual(list(p2.keys()), ["msg_3", "msg_2"])

        bridge.cleanup_socket()

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    async def test_unwritable_db_directory_error_visibility(self):
        from claude_chatter.storage import MessageStore
        from claude_chatter_mcp.mcp_server import build_mcp_server
        bad_dir = os.path.join(self.tmp_dir.name, "bad_dir")
        os.makedirs(bad_dir, exist_ok=True)
        os.chmod(bad_dir, 0o444)
        bad_db = os.path.join(bad_dir, "messages.db")
        try:
            store = MessageStore(bad_db)
            self.assertTrue(store.is_disabled())
            self.assertIsNotNone(store.last_error)

            bridge = ClaudeClient(
                bridge_socket_path=self.sock_path,
                session_name="disabled-store-sess",
                db_path=bad_db
            )
            self.assertIsNotNone(bridge.persistence_error)
            server = build_mcp_server(bridge)
            read_fn = (await server.get_tool("read_messages")).fn
            read_res = json.loads(await read_fn())
            self.assertIn("persistence_error", read_res)

            get_resp_fn = (await server.get_tool("get_responses")).fn
            resp_res = json.loads(await get_resp_fn())
            self.assertIn("persistence_error", resp_res)
            bridge.cleanup_socket()
        finally:
            os.chmod(bad_dir, 0o777)

    async def test_rename_with_unread_messages_pending(self):
        from claude_chatter_mcp.mcp_server import build_mcp_server
        bridge = ClaudeClient(
            bridge_socket_path=self.sock_path,
            session_name="pending-sess",
            db_path=self.db_path
        )
        bridge.response_store["pending_1"] = {
            "direction": "inbound",
            "read": False,
            "content": "unread message payload",
            "timestamp": 1.0
        }
        server = build_mcp_server(bridge)
        rename_fn = (await server.get_tool("rename_session")).fn
        res = json.loads(await rename_fn(new_name="renamed-pending-sess"))
        self.assertTrue(res["success"])

        read_fn = (await server.get_tool("read_messages")).fn
        unread = json.loads(await read_fn(mark_read=False))["messages"]
        self.assertIn("pending_1", unread)
        self.assertEqual(unread["pending_1"]["content"], "unread message payload")
        bridge.cleanup_session_descriptor()
        bridge.cleanup_socket()

    async def test_simulated_db_failure_during_rename_is_surfaced(self):
        from unittest import mock
        import sqlite3
        from fastmcp.exceptions import ToolError
        from claude_chatter_mcp.mcp_server import build_mcp_server
        bridge = ClaudeClient(
            bridge_socket_path=self.sock_path,
            session_name="original-name",
            db_path=self.db_path
        )
        bridge.register_session_descriptor(session_name="original-name")
        server = build_mcp_server(bridge)
        rename_fn = (await server.get_tool("rename_session")).fn

        with mock.patch.object(bridge.store, "rename_session", side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(ToolError):
                await rename_fn(new_name="failed-name")

        self.assertEqual(bridge.session_name, "original-name")
        self.assertEqual(bridge.response_store._session_name, "original-name")
        bridge.cleanup_session_descriptor()
        bridge.cleanup_socket()

    async def test_get_responses_with_persistence_error_does_not_mutate_in_memory_store(self):
        from claude_chatter_mcp.mcp_server import build_mcp_server
        bridge = ClaudeClient(
            bridge_socket_path=self.sock_path,
            session_name=self.session_name,
            db_path=self.db_path
        )
        bridge.response_store["target_msg"] = {
            "direction": "inbound",
            "read": False,
            "content": "test payload",
            "timestamp": time.time()
        }
        bridge.store._disabled = True
        bridge.store.last_error = "disk failure simulated"

        server = build_mcp_server(bridge)
        get_resp_fn = (await server.get_tool("get_responses")).fn
        res = json.loads(await get_resp_fn(msg_id="target_msg"))
        self.assertIn("persistence_error", res)
        self.assertEqual(res["persistence_error"], "disk failure simulated")

        self.assertNotIn("persistence_error", bridge.response_store["target_msg"])
        bridge.cleanup_socket()

    async def test_rename_session_rejected_when_name_in_use_by_active_session(self):
        from fastmcp.exceptions import ToolError
        from claude_chatter_mcp.mcp_server import build_mcp_server
        sessions_dir = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
        os.makedirs(sessions_dir, exist_ok=True)

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.0)"])
        fake_desc = os.path.join(sessions_dir, f"{proc.pid}.json")
        desc_data = {
            "name": "already-active-sess",
            "pid": proc.pid,
            "cwd": "/some/other/cwd"
        }
        with open(fake_desc, "w", encoding="utf-8") as f:
            json.dump(desc_data, f)

        bridge = ClaudeClient(
            bridge_socket_path=self.sock_path,
            session_name="my-curr-sess",
            db_path=self.db_path
        )
        bridge.register_session_descriptor(session_name="my-curr-sess")
        server = build_mcp_server(bridge)
        rename_fn = (await server.get_tool("rename_session")).fn

        try:
            with self.assertRaises(ToolError) as ctx:
                await rename_fn(new_name="already-active-sess")
            self.assertIn("currently in use by an active session", str(ctx.exception))
            self.assertEqual(bridge.session_name, "my-curr-sess")
        finally:
            proc.kill()
            proc.wait()
            if os.path.exists(fake_desc):
                os.remove(fake_desc)
            bridge.cleanup_session_descriptor()
            bridge.cleanup_socket()


class TestCliDelegatesToLibrary(unittest.TestCase):
    """The library modes of bridge-mcp are served by claude_chatter.cli."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "cli.db")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _seed(self, session_name):
        from claude_chatter.storage import MessageStore
        store = MessageStore(self.db_path)
        store.ensure_session(session_name)
        store.save_message(
            "cli_in_1",
            {"direction": "inbound", "read": False, "content": "cli test payload", "timestamp": time.time()},
            session_name
        )
        store.close()

    def _run(self, args, env=None):
        return subprocess.run(
            [sys.executable, "-m", "claude_chatter_mcp.cli", *args],
            capture_output=True, text=True, check=True, env=env
        )

    def test_inbox_and_history_by_name(self):
        self._seed("cli-deleg-sess")
        inbox = json.loads(self._run(["--inbox", "--name", "cli-deleg-sess", "--db", self.db_path]).stdout)
        self.assertIn("cli_in_1", inbox)
        history = json.loads(self._run(["--history", "--name", "cli-deleg-sess", "--db", self.db_path]).stdout)
        self.assertIn("cli_in_1", history)

    def test_agy_session_name_alias_is_honored(self):
        self._seed("cli-alias-sess")
        env = os.environ.copy()
        env.pop("CHATTER_SESSION_NAME", None)
        env["AGY_SESSION_NAME"] = "cli-alias-sess"
        inbox = json.loads(self._run(["--inbox", "--db", self.db_path], env=env).stdout)
        self.assertIn("cli_in_1", inbox)


class TestMCPStdioProtocol(unittest.TestCase):
    def test_stdio_rpc(self):
        """Verify standard JSON-RPC 2.0 stdio MCP methods."""
        script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "bridge_mcp.py"))
        socket_path = f"/tmp/test_rpc_{uuid.uuid4().hex[:8]}.sock"

        # The stdio process's default session name is derived from cwd - run it from
        # an isolated temp directory, not this repo's own directory, so it can never
        # collide with (or overwrite the identity of) a real developer session for
        # this same repo.
        test_cwd = tempfile.mkdtemp(prefix="bridge_mcp_test_")

        proc = subprocess.Popen(
            [sys.executable, script_path, "--socket", socket_path],
            cwd=test_cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            # 1. initialize - FastMCP validates against the real MCP spec, which
            # requires protocolVersion/capabilities/clientInfo (unlike the old
            # hand-rolled server, which accepted anything here).
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-suite", "version": "1.0"}
                }
            }
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()
            init_resp = json.loads(proc.stdout.readline())
            self.assertEqual(init_resp["result"]["serverInfo"]["name"], "claudemessaging")

            # 2. ping
            ping_req = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
            proc.stdin.write(json.dumps(ping_req) + "\n")
            proc.stdin.flush()
            ping_resp = json.loads(proc.stdout.readline())
            self.assertEqual(ping_resp["result"], {})

            # 3. tools/list
            list_req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
            proc.stdin.write(json.dumps(list_req) + "\n")
            proc.stdin.flush()
            list_resp = json.loads(proc.stdout.readline())
            tool_names = [t["name"] for t in list_resp["result"]["tools"]]
            # 4. tools/call list_sessions
            call_req = {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "list_sessions", "arguments": {}}
            }
            proc.stdin.write(json.dumps(call_req) + "\n")
            proc.stdin.flush()
            call_resp = json.loads(proc.stdout.readline())
            self.assertFalse(call_resp["result"]["isError"])
            self.assertIn("content", call_resp["result"])

            # 5. tools/call rename_session
            rename_req = {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "rename_session", "arguments": {"new_name": "renamed-suite-bridge"}}
            }
            proc.stdin.write(json.dumps(rename_req) + "\n")
            proc.stdin.flush()
            rename_resp = json.loads(proc.stdout.readline())
            self.assertFalse(rename_resp["result"]["isError"])
            rename_data = json.loads(rename_resp["result"]["content"][0]["text"])
            self.assertEqual(rename_data["new_name"], "renamed-suite-bridge")

        finally:
            proc.terminate()
            proc.wait(timeout=5)
            if os.path.exists(socket_path):
                try:
                    os.remove(socket_path)
                except OSError:
                    pass
            try:
                os.rmdir(test_cwd)
            except OSError:
                pass

    def test_sigterm_triggers_graceful_shutdown_and_cleanup(self):
        """Verify SIGTERM (not just SIGINT/Ctrl+C) on the default stdio mode
        exits 0 and cleans up its session descriptor + socket - unlike SIGINT,
        SIGTERM has no default Python-level handler, so without an explicit
        one the process is killed raw by the OS (exit 143) and _lifespan's
        cleanup never runs. This is exactly what a host gracefully stopping
        the MCP subprocess for a reload (e.g. Antigravity's `/mcp` reload)
        does, and it reads a non-zero/signal exit as a failed stop."""
        script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "bridge_mcp.py"))
        socket_path = f"/tmp/test_sigterm_{uuid.uuid4().hex[:8]}.sock"
        session_name = f"suite-sigterm-{uuid.uuid4().hex[:8]}"
        test_cwd = tempfile.mkdtemp(prefix="bridge_mcp_sigterm_test_")

        proc = subprocess.Popen(
            [sys.executable, script_path, "--socket", socket_path, "--name", session_name],
            cwd=test_cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        sessions_dir = os.path.expanduser("~/.claude/sessions")
        named_descriptor = os.path.join(sessions_dir, f"bridge-{session_name}.json")

        try:
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-suite", "version": "1.0"}
                }
            }
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()
            proc.stdout.readline()  # wait for the response - confirms the lifespan has started

            # Give the lifespan's registration a moment to land on disk.
            for _ in range(50):
                if os.path.exists(named_descriptor):
                    break
                time.sleep(0.1)
            self.assertTrue(os.path.exists(named_descriptor), "session descriptor was never registered")

            proc.terminate()  # SIGTERM
            proc.wait(timeout=5)

            self.assertEqual(proc.returncode, 0, f"expected clean exit 0, got {proc.returncode} (stderr: {proc.stderr.read()})")
            self.assertFalse(os.path.exists(named_descriptor), "session descriptor was not cleaned up on SIGTERM")
            self.assertFalse(os.path.exists(socket_path), "socket file was not cleaned up on SIGTERM")

        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for p in (named_descriptor, socket_path):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            try:
                os.rmdir(test_cwd)
            except OSError:
                pass


class TestBridgeStandaloneCLI(unittest.TestCase):
    def setUp(self):
        self.script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "bridge_mcp.py"))

    def test_cli_list(self):
        """Verify python3 bridge_mcp.py --list executes and returns valid JSON."""
        res = subprocess.run(
            [sys.executable, self.script_path, "--list"],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertIsInstance(data, dict)

    def test_cli_purge(self):
        """Verify python3 bridge_mcp.py --purge executes successfully."""
        res = subprocess.run(
            [sys.executable, self.script_path, "--purge"],
            capture_output=True,
            text=True
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Purge scan completed", res.stdout)

    def test_cli_standalone_daemon(self):
        """Verify python3 bridge_mcp.py --standalone starts and stops gracefully on SIGTERM."""
        socket_path = f"/tmp/test_standalone_{uuid.uuid4().hex[:8]}.sock"
        proc = subprocess.Popen(
            [sys.executable, self.script_path, "--standalone", "--socket", socket_path, "--name", "test-standalone-daemon"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            time.sleep(1.0)
            self.assertTrue(os.path.exists(socket_path))
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=3)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Standalone daemon shut down gracefully", stderr)
            self.assertFalse(os.path.exists(socket_path))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if os.path.exists(socket_path):
                try:
                    os.remove(socket_path)
                except OSError:
                    pass

    def test_cli_standalone_send(self):
        """Verify python3 bridge_mcp.py --send dispatches to a standalone daemon.
        send_message is fire-and-forget (no auto-reply scaffold, no transcript
        polling) - a real reply, if any, arrives later via a genuine send_message
        call from the target instead."""
        socket_path = f"/tmp/test_pingpong_{uuid.uuid4().hex[:8]}.sock"
        daemon_name = f"suite-daemon-{uuid.uuid4().hex[:6]}"

        proc = subprocess.Popen(
            [sys.executable, self.script_path, "--standalone", "--socket", socket_path, "--name", daemon_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        try:
            time.sleep(1.0)
            self.assertTrue(os.path.exists(socket_path))

            # Send message to the daemon using --send; dispatch should succeed
            # immediately since send_message is fire-and-forget.
            res = subprocess.run(
                [sys.executable, self.script_path, "--send", daemon_name, "hello standalone"],
                capture_output=True,
                text=True,
                timeout=10
            )
            self.assertEqual(res.returncode, 0)
            data = json.loads(res.stdout)
            self.assertTrue(data.get("success"))
            self.assertEqual(data.get("status"), "dispatched")

        finally:
            proc.terminate()
            proc.wait(timeout=3)
            if os.path.exists(socket_path):
                try:
                    os.remove(socket_path)
                except OSError:
                    pass



class TestReadMessagesPushHint(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def read_messages(self, ancestor_names):
        from unittest import mock
        from fastmcp import Client
        from claude_chatter_mcp import mcp_server

        class FakeProc:
            def __init__(self, name):
                self._name = name

            def name(self):
                return self._name

        class FakeSelf:
            def parents(self):
                return [FakeProc(n) for n in ancestor_names]

        protocol = ClaudeClient(
            bridge_socket_path=os.path.join(self.tmp_dir.name, "hint.sock"),
            session_name="hint-test",
            db_path=os.path.join(self.tmp_dir.name, "hint.db")
        )
        with mock.patch.object(mcp_server.psutil, "Process", return_value=FakeSelf()):
            server = mcp_server.build_mcp_server(protocol)
        async with Client(server) as client:
            result = await client.call_tool("read_messages", {})
        return json.loads(result.content[0].text)

    async def test_hint_present_under_agy_when_unprimed(self):
        result = await self.read_messages(["uv", "agy", "bash"])
        self.assertEqual(result["messages"], {})
        self.assertIn("enable_push", result["push_hint"])
        self.assertIn("sandbox bypassed", result["push_hint"])

    async def call_enable_push(self, arguments=None):
        from fastmcp import Client
        from claude_chatter_mcp import mcp_server

        protocol = ClaudeClient(
            bridge_socket_path=os.path.join(self.tmp_dir.name, "pp.sock"), session_name="pp-test"
        )
        async with Client(mcp_server.build_mcp_server(protocol)) as client:
            result = await client.call_tool("enable_push", arguments or {})
        return json.loads(result.content[0].text)

    async def test_enable_push_ignores_an_unexpected_session_id(self):
        result = await self.call_enable_push({"session_id": "2519eecb-f2c7-4996-bebe-69a58c2ea2f8"})
        self.assertIn("--prime", result["command"])

    async def test_send_message_accepts_another_hosts_parameter_names(self):
        from fastmcp import Client
        from fastmcp.exceptions import ToolError
        from claude_chatter_mcp import mcp_server

        protocol = ClaudeClient(
            bridge_socket_path=os.path.join(self.tmp_dir.name, "pp.sock"), session_name="pp-test"
        )
        async with Client(mcp_server.build_mcp_server(protocol)) as client:
            with self.assertRaises(ToolError) as caught:
                await client.call_tool("send_message", {"Recipient": "no-such-session", "Message": "hi"})
        self.assertNotIn("validation error", str(caught.exception))

    async def test_enable_push_tells_the_agent_to_run_the_command(self):
        result = await self.call_enable_push()
        self.assertFalse(result["primed"])
        self.assertIn("--prime", result["command"])
        self.assertIn("Run `command` in your shell tool", result["next_step"])
        self.assertIn("sandbox bypassed", result["next_step"])

    async def test_no_hint_outside_agy(self):
        result = await self.read_messages(["claude", "bash"])
        self.assertNotIn("push_hint", result)


class TestAgyGlue(unittest.TestCase):
    def test_wake_text_tells_the_agent_to_read_messages_and_names_no_peer_text(self):
        from claude_chatter_mcp.agy_glue import wake_text
        text = wake_text(["a", "b"])
        self.assertIn("2 new message(s)", text)
        self.assertIn("read_messages", text)

    def test_prime_command_names_the_bridge_script_and_module(self):
        from claude_chatter_mcp.agy_glue import make_prime
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("antigravity_wakeup.command.sys.executable", os.path.join(tmp, "python3")):
                self.assertEqual(
                    make_prime().command(),
                    f"{os.path.join(tmp, 'python3')} -m claude_chatter_mcp.cli --prime"
                )

    def test_pinning_is_off_unless_the_env_var_is_set(self):
        from claude_chatter_mcp.agy_glue import make_prime
        with mock.patch.dict(os.environ, {"AGY_WAKEUP_PIN_AGY": ""}):
            self.assertFalse(make_prime().new_poker()._pin)
        with mock.patch.dict(os.environ, {"AGY_WAKEUP_PIN_AGY": "1"}):
            self.assertTrue(make_prime().new_poker()._pin)

    def test_prime_hook_cli_injects_the_enable_push_notice(self):
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env["HOME"] = home
            res = subprocess.run(
                [sys.executable, "-m", "claude_chatter_mcp.cli", "--prime-hook"],
                input=json.dumps({"conversationId": "conv-glue-1"}),
                capture_output=True, text=True, check=True, env=env
            )
        step = json.loads(res.stdout)["injectSteps"][0]
        self.assertIn("enable_push", step["ephemeralMessage"])
        self.assertIn("sandbox bypassed", step["ephemeralMessage"])


class _FakeAgy:
    def __init__(self, pwd=None, cwd="/", environ_error=None):
        self._pwd, self._cwd, self._environ_error = pwd, cwd, environ_error

    def environ(self):
        if self._environ_error:
            raise self._environ_error
        return {"PWD": self._pwd} if self._pwd else {}

    def cwd(self):
        return self._cwd


class TestAgyProjectPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.start_dir = os.path.join(self._tmp.name, "start")
        self.moved_dir = os.path.join(self._tmp.name, "moved")
        os.mkdir(self.start_dir)
        os.mkdir(self.moved_dir)
        env = {k: v for k, v in os.environ.items() if k != "CHATTER_PROJECT_PATH"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _under(self, agy):
        return mock.patch("claude_chatter_mcp.agy_glue.AgyLocator.find_ancestor", return_value=agy)

    def test_startup_pwd_wins_over_the_current_cwd(self):
        from claude_chatter_mcp.agy_glue import agy_start_dir
        with self._under(_FakeAgy(pwd=self.start_dir, cwd=self.moved_dir)):
            self.assertEqual(agy_start_dir(), self.start_dir)

    def test_falls_back_to_cwd_when_pwd_is_missing_or_stale(self):
        from claude_chatter_mcp.agy_glue import agy_start_dir
        with self._under(_FakeAgy(pwd=None, cwd=self.moved_dir)):
            self.assertEqual(agy_start_dir(), self.moved_dir)
        with self._under(_FakeAgy(pwd=os.path.join(self._tmp.name, "gone"), cwd=self.moved_dir)):
            self.assertEqual(agy_start_dir(), self.moved_dir)
        with self._under(_FakeAgy(pwd="relative/dir", cwd=self.moved_dir)):
            self.assertEqual(agy_start_dir(), self.moved_dir)

    def test_falls_back_to_cwd_when_environ_is_unreadable(self):
        import psutil
        from claude_chatter_mcp.agy_glue import agy_start_dir
        with self._under(_FakeAgy(cwd=self.moved_dir, environ_error=psutil.AccessDenied(1))):
            self.assertEqual(agy_start_dir(), self.moved_dir)

    def test_none_when_not_under_agy(self):
        from claude_chatter_mcp.agy_glue import agy_start_dir, resolve_project_path
        with self._under(None):
            self.assertIsNone(agy_start_dir())
            self.assertIsNone(resolve_project_path(None))

    def test_explicit_path_and_env_var_win_over_agy(self):
        from claude_chatter_mcp.agy_glue import resolve_project_path
        with self._under(_FakeAgy(pwd=self.start_dir)):
            self.assertEqual(resolve_project_path("/explicit"), "/explicit")
            self.assertEqual(resolve_project_path(None), self.start_dir)
            with mock.patch.dict(os.environ, {"CHATTER_PROJECT_PATH": "/from/env"}):
                self.assertIsNone(resolve_project_path(None))

    def _run_cli(self, argv, resolved, chdir_error=None):
        from claude_chatter_mcp import cli
        with mock.patch.object(sys, "argv", argv), \
                mock.patch("claude_chatter_mcp.agy_glue.resolve_project_path", return_value=resolved) as resolve, \
                mock.patch("claude_chatter_mcp.mcp_server.make_protocol") as make, \
                mock.patch("claude_chatter_mcp.mcp_server.run_mcp_server", new=mock.Mock(return_value=None)), \
                mock.patch("claude_chatter_mcp.cli.asyncio.run"), \
                mock.patch("claude_chatter_mcp.cli.os.chdir", side_effect=chdir_error) as chdir:
            cli.main()
        return resolve, make, chdir

    def test_bridge_cli_passes_the_resolved_path_to_the_client(self):
        resolve, make, _ = self._run_cli(["bridge-mcp"], "/resolved")
        resolve.assert_called_once_with(None)
        self.assertEqual(make.call_args.kwargs["project_path"], "/resolved")

    def test_bridge_cli_moves_into_the_agy_start_directory(self):
        _, _, chdir = self._run_cli(["bridge-mcp"], "/resolved")
        chdir.assert_called_once_with("/resolved")

    def test_bridge_cli_leaves_the_cwd_alone_for_an_explicit_project_path(self):
        _, _, chdir = self._run_cli(["bridge-mcp", "--project-path", "/explicit"], "/explicit")
        chdir.assert_not_called()

    def test_bridge_cli_leaves_the_cwd_alone_outside_agy(self):
        _, make, chdir = self._run_cli(["bridge-mcp"], None)
        chdir.assert_not_called()
        self.assertIsNone(make.call_args.kwargs["project_path"])

    def test_bridge_cli_still_starts_when_the_directory_change_fails(self):
        _, make, _ = self._run_cli(["bridge-mcp"], "/resolved", chdir_error=PermissionError("no"))
        self.assertEqual(make.call_args.kwargs["project_path"], "/resolved")


class _CtlServer:
    """A bridge control socket in a thread: records each request line and answers with a fixed reply."""

    def __init__(self, path, reply):
        import socket
        import threading
        self.requests = []
        self._reply = reply
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                line = conn.makefile("r", encoding="utf-8").readline()
                self.requests.append(json.loads(line))
                conn.sendall((json.dumps(self._reply) + "\n").encode("utf-8"))

    def close(self):
        self._server.close()
        self._thread.join(timeout=2)


class TestStopHook(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sessions = os.path.join(self._tmp.name, "sessions")
        os.mkdir(self.sessions)
        sock = os.path.join(self._tmp.name, "b.sock")
        # This process plays the agy; a child of it plays the bridge started under it.
        bridge = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(bridge.wait)
        self.addCleanup(bridge.kill)
        with open(os.path.join(self.sessions, "bridge-x.json"), "w") as f:
            json.dump({"pid": bridge.pid, "kind": "bg", "messagingSocketPath": sock}, f)
        self.ctl = _CtlServer(sock + ".ctl", {"ok": True, "pending": 1})
        self.addCleanup(self.ctl.close)

        class _Agy:
            pid = os.getpid()
        self.agy = _Agy()

    def _run(self, hook_input, agy="default"):
        from claude_chatter_mcp.idle import run_stop_hook
        found = self.agy if agy == "default" else agy
        with mock.patch("claude_chatter_mcp.idle.AgyLocator.find_ancestor", return_value=found):
            return run_stop_hook(json.dumps(hook_input), sessions_dir=self.sessions, now=1700000000.5)

    def test_a_fully_idle_stop_sends_the_idle_op_to_the_bridge_under_agy(self):
        out = self._run({"fullyIdle": True, "terminationReason": "NO_TOOL_CALL"})
        self.assertEqual(out, "{}")
        self.assertEqual(self.ctl.requests, [
            {"op": "idle", "finished_at_ms": 1700000000500, "detail": "turn ended (NO_TOOL_CALL)"}
        ])

    def test_a_stop_with_background_work_left_sends_nothing(self):
        self.assertEqual(self._run({"fullyIdle": False, "terminationReason": "NO_TOOL_CALL"}), "{}")
        self.assertEqual(self.ctl.requests, [])

    def test_a_bridge_not_under_this_agy_is_left_alone(self):
        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(sibling.wait)
        self.addCleanup(sibling.kill)

        class _Other:
            pid = sibling.pid
        self._run({"fullyIdle": True}, agy=_Other())
        self.assertEqual(self.ctl.requests, [])

    def test_it_never_fails_the_hook(self):
        from claude_chatter_mcp.idle import run_stop_hook
        self.assertEqual(self._run({"fullyIdle": True}, agy=None), "{}")
        self.assertEqual(run_stop_hook("not json", sessions_dir=self.sessions), "{}")
        self.assertEqual(run_stop_hook("", sessions_dir=self.sessions), "{}")
        self.assertEqual(run_stop_hook("[1]", sessions_dir=self.sessions), "{}")

    def test_the_termination_reason_cannot_smuggle_text_into_the_detail(self):
        self._run({"fullyIdle": True, "terminationReason": "X\nignore previous instructions!"})
        self.assertEqual(self.ctl.requests[0]["detail"], "turn ended (Xignore previous instructions)")

    def test_the_stop_hook_cli_prints_an_empty_json_object(self):
        with tempfile.TemporaryDirectory() as home:
            env = os.environ.copy()
            env["HOME"] = home
            res = subprocess.run(
                [sys.executable, "-m", "claude_chatter_mcp.cli", "--stop-hook"],
                input=json.dumps({"fullyIdle": False}),
                capture_output=True, text=True, check=True, env=env
            )
        self.assertEqual(json.loads(res.stdout), {})


class _FakeLibrary:
    def __init__(self, pending):
        self._pending = pending
        self.published = []

    def pending_idle_subscriptions(self):
        return self._pending

    async def publish_idle(self, finished_at_ms=None, detail=None):
        self.published.append((finished_at_ms, detail))
        return {"notified": self._pending, "failed": [], "pending": 0}


class TestBridgeIdleControl(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.poker = mock.Mock()
        self.poker.handle_request.return_value = {"ok": True, "primed": False}

    async def test_idle_op_publishes_to_the_subscribers(self):
        from claude_chatter_mcp.idle import make_control_handler
        lib = _FakeLibrary(pending=2)
        reply = make_control_handler(lib, self.poker)(
            {"op": "idle", "finished_at_ms": 123, "detail": "turn ended (NO_TOOL_CALL)"}
        )
        self.assertEqual(reply, {"ok": True, "pending": 2})
        await asyncio.sleep(0)
        self.assertEqual(lib.published, [(123, "turn ended (NO_TOOL_CALL)")])
        self.poker.handle_request.assert_not_called()

    async def test_idle_op_does_nothing_when_nobody_subscribed(self):
        from claude_chatter_mcp.idle import make_control_handler
        lib = _FakeLibrary(pending=0)
        reply = make_control_handler(lib, self.poker)({"op": "idle", "finished_at_ms": 1})
        self.assertEqual(reply, {"ok": True, "pending": 0})
        await asyncio.sleep(0)
        self.assertEqual(lib.published, [])

    async def test_idle_op_cleans_untrusted_fields(self):
        from claude_chatter_mcp.idle import make_control_handler
        lib = _FakeLibrary(pending=1)
        make_control_handler(lib, self.poker)({"op": "idle", "finished_at_ms": True, "detail": "a\nb" + "x" * 300})
        await asyncio.sleep(0)
        finished_at, detail = lib.published[0]
        self.assertIsNone(finished_at)
        self.assertNotIn("\n", detail)
        self.assertEqual(len(detail), 200)

    async def test_other_ops_still_go_to_the_poker(self):
        from claude_chatter_mcp.idle import make_control_handler
        reply = make_control_handler(_FakeLibrary(pending=1), self.poker)({"op": "status"})
        self.assertEqual(reply, {"ok": True, "primed": False})
        self.poker.handle_request.assert_called_once_with({"op": "status"})

    async def test_idle_op_reaches_a_subscriber_through_the_real_library(self):
        from claude_chatter_mcp.idle import make_control_handler
        with tempfile.TemporaryDirectory() as tmp:
            client = ClaudeClient(
                session_name="idle-e2e", bridge_socket_path=os.path.join(tmp, "bridge.sock"),
                db_path=os.path.join(tmp, "m.db"), as_bridge=True, project_path=tmp
            )
            notices = []

            async def subscriber(reader, writer):
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    notices.append(json.loads(line.decode("utf-8")))
                writer.close()

            reply_path = os.path.join(tmp, "sub.sock")
            sub_server = await asyncio.start_unix_server(subscriber, reply_path)
            listener = asyncio.create_task(client.start_bridge_listener())
            try:
                await asyncio.sleep(0.3)
                sub_id = str(uuid.uuid4())
                _, writer = await asyncio.open_unix_connection(client.bridge_socket_path)
                frame = {"type": "control", "msgV": 1, "action": "notify_when_idle",
                         "from": f"uds:{reply_path}", "msg_id": sub_id, "from_mode": "prompting"}
                writer.write((json.dumps(frame) + "\n").encode("utf-8"))
                await writer.drain()
                writer.close()
                await asyncio.sleep(0.3)
                self.assertEqual(client.pending_idle_subscriptions(), 1)

                reply = make_control_handler(client, self.poker)(
                    {"op": "idle", "finished_at_ms": 5, "detail": "turn ended (NO_TOOL_CALL)"}
                )
                self.assertEqual(reply, {"ok": True, "pending": 1})
                await asyncio.sleep(0.5)
            finally:
                listener.cancel()
                sub_server.close()
                await sub_server.wait_closed()
                client.cleanup_socket()
        control = [n for n in notices if n.get("type") == "control"]
        self.assertEqual(len(control), 1)
        self.assertEqual(control[0]["action"], "peer_idle_notice")
        self.assertEqual(control[0]["orig_msg_id"], sub_id)
        self.assertEqual(control[0]["finished_at"], 5)
        self.assertEqual(control[0]["detail"], "turn ended (NO_TOOL_CALL)")

    async def test_a_library_without_idle_support_is_reported_not_crashed(self):
        from claude_chatter_mcp.idle import make_control_handler
        reply = make_control_handler(object(), self.poker)({"op": "idle"})
        self.assertFalse(reply["ok"])
        self.assertIn("cannot publish idle notices", reply["error"])


class TestPluginPins(unittest.TestCase):
    """The plugin's commands pin the PyPI release; the release PR must bump every pin together."""
    ROOT = os.path.dirname(os.path.abspath(__file__))
    PIN = re.compile(r"claude-chatter-mcp==(\d+\.\d+\.\d+)")

    def _read(self, name):
        with open(os.path.join(self.ROOT, name), "r", encoding="utf-8") as f:
            return f.read()

    def _pin_lines(self):
        lines = []
        for name in ("mcp_config.json", "hooks.json"):
            lines += [(name, line) for line in self._read(name).splitlines() if "claude-chatter-mcp==" in line]
        return lines

    def test_three_commands_are_pinned(self):
        self.assertEqual(sorted(name for name, _ in self._pin_lines()), ["hooks.json", "hooks.json", "mcp_config.json"])

    def test_every_pin_is_the_project_version(self):
        version = re.search(r'^version = "(\d+\.\d+\.\d+)"', self._read("pyproject.toml"), re.M).group(1)
        for name, line in self._pin_lines():
            self.assertEqual(self.PIN.search(line).group(1), version, f"{name}: {line.strip()}")

    def test_every_pin_line_carries_the_release_please_annotation(self):
        for name, line in self._pin_lines():
            self.assertIn("x-release-please-version", line, f"{name}: {line.strip()}")

    def test_release_please_updates_both_files_with_the_generic_updater(self):
        config = json.loads(self._read("release-please-config.json"))
        extra = config["packages"]["."]["extra-files"]
        self.assertEqual(
            sorted((e["path"], e["type"]) for e in extra),
            [("hooks.json", "generic"), ("mcp_config.json", "generic")]
        )

    def test_the_mcp_server_still_replaces_the_shell_with_uvx(self):
        server = json.loads(self._read("mcp_config.json"))["mcpServers"]["bridge"]
        self.assertEqual(server["command"], "sh")
        self.assertEqual(server["args"][0], "-c")
        self.assertTrue(server["args"][1].startswith("exec uvx --from claude-chatter-mcp=="))


if __name__ == "__main__":
    unittest.main()
