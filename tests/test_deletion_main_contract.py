"""Load the deployed main deletion construction without importing main side effects."""
import ast
import inspect
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord
from deletion_audit import DeletionAuditService


SOURCE = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
EXPECTED_ROUTES = {
    1064823080100306995: {937715555232780318, 944520863389208606, 1098896878768234556,
                        1064823080100306995, 932654164201336872, 989509986793168926, 944522706894872606},
    1383468537229738156: {865821307969732648, 1134766793249013780, 1176877764608004156,
                        802904099816472619, 820536422808944662, 1383468537229738156},
    1065283543640576103: {1247409483353821335, 721047251862159420, 904343326654885939,
                        862310554567835658, 915207176518270981, 1065283543640576103},
    1383987919454343269: {1247494689876086804, 1247543437478330410, 1383987919454343269},
}


def construction():
    client = SimpleNamespace(get_channel=Mock(return_value=None), get_guild=Mock(return_value=None),
                             fetch_channel=AsyncMock(), user=SimpleNamespace(id=99), run=Mock())
    names = {"_fetch_deletion_bot_member", "_create_deletion_audit"}
    assignments = {"LOG_ROUTES", "CHANNEL_TO_LOG", "DELETION_AUDIT"}
    nodes = [node for node in SOURCE.body
             if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names)
             or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                 and target.id in assignments for target in node.targets))]
    namespace = {"bot": client, "inspect": inspect, "logging": logging, "DeletionAuditService": DeletionAuditService}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace, client


class MainDeletionContractTests(unittest.IsolatedAsyncioTestCase):
    def test_T20_actual_main_import_and_bot_construction_offline_with_isolated_state(self):
        repository = Path(__file__).resolve().parents[1]
        dependencies = Path(discord.__file__).resolve().parents[1]
        script = textwrap.dedent('''
            import json
            import socket
            from pathlib import Path
            import sys
            from unittest.mock import patch

            state = Path(sys.argv[1]).resolve()
            blocked = AssertionError("offline smoke test forbids network")
            with patch("dotenv.load_dotenv", return_value=False) as dotenv_loader, \\
                 patch.object(socket.socket, "connect", side_effect=blocked) as connect, \\
                 patch.object(socket.socket, "connect_ex", side_effect=blocked) as connect_ex, \\
                 patch.object(socket, "create_connection", side_effect=blocked) as create_connection, \\
                 patch.object(socket, "getaddrinfo", side_effect=blocked) as getaddrinfo, \\
                 patch("discord.Client.run", side_effect=blocked) as run:
                import main
                import deletion_audit
                assert isinstance(main.DELETION_AUDIT, deletion_audit.DeletionAuditService)
                assert main.DELETION_AUDIT.channel_fetcher.__self__ is main.bot
                assert main.DELETION_AUDIT.bot_member_fetcher is main._fetch_deletion_bot_member
                assert main.ADMIN_SERVICE.delete_service.__self__ is main.DELETION_AUDIT
                assert main.DELETION_AUDIT.private_full_sources == frozenset()
                assert main.DELETION_AUDIT.registered_source_content is True
                assert main.XP_DATA_FILE.resolve().parent == state
                assert main.ACHIEVEMENTS_FILE.resolve().parent == state
                assert main.TYPE_REMINDER_COOLDOWN_FILE.resolve().parent == state
                assert main.bot.user is None and not main.bot.is_ready()
                assert main.bot.intents.message_content
                assert dotenv_loader.called
                for operation in (connect, connect_ex, create_connection, getaddrinfo, run):
                    operation.assert_not_called()
                main._executor.shutdown(wait=False, cancel_futures=True)
                print(json.dumps({"imported": True, "gateway_started": False,
                                  "routes": sorted(main.LOG_ROUTES),
                                  "discord_version": main.discord.__version__}))
        ''')
        with tempfile.TemporaryDirectory(prefix="doribot-import-") as temporary:
            temporary_path = Path(temporary)
            state = temporary_path / "state"
            state.mkdir()
            # Whitelist interpreter/OS paths only; no production secrets or dotenv.
            environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATH")
                           if name in os.environ}
            environment.update({
                "PYTHONPATH": os.pathsep.join((str(repository), str(dependencies))),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                "DATA_DIR": str(state), "DISCORD_TOKEN": "offline-dummy-not-a-discord-token",
                "SPECIAL_ADMIN_USER_ID": "855749166764654653",
                "SPECIAL_ADMIN_USER_IDS": "855749166764654653,491239880005910538",
                "GEMINI_API_KEY": "", "DELETION_PUBLIC_LOG_ENABLED": "1",
                "DELETION_PUBLIC_LOG_INCLUDE_CONTENT": "1", "DELETION_LOG_ALL_GUILD_CHANNELS": "1",
                "DELETION_PUBLIC_LOG_PRIVATE_FULL_SOURCE_IDS": "",
                "DELETION_PUBLIC_LOG_REGISTERED_SOURCE_CONTENT": "1",
            })
            result = subprocess.run([sys.executable, "-c", script, str(state)], cwd=temporary,
                                    env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["imported"])
            self.assertFalse(payload["gateway_started"])
            self.assertEqual(payload["routes"], sorted(EXPECTED_ROUTES))
            self.assertEqual(payload["discord_version"], "2.7.1")
            self.assertFalse(any(path.suffix == ".pkl" for path in temporary_path.rglob("*")))

    async def test_T20_deployed_constructor_arguments_match_real_module_without_gateway(self):
        namespace, client = construction()
        service = namespace["DELETION_AUDIT"]
        self.assertIsInstance(service, DeletionAuditService)
        self.assertIs(service.channel_fetcher, client.fetch_channel)
        self.assertIs(service.bot_member_fetcher, namespace["_fetch_deletion_bot_member"])
        self.assertEqual(service.routes, namespace["CHANNEL_TO_LOG"])
        client.run.assert_not_called()
        client.get_channel.assert_not_called()
        client.fetch_channel.assert_not_awaited()

    async def test_T20_stale_module_fails_clearly_before_constructing(self):
        namespace, client = construction()
        constructed = Mock()

        class StaleService:
            def __init__(self, routes, channel_resolver, *, guild_resolver=None):
                constructed()

        with self.assertLogs(level="CRITICAL") as captured:
            with self.assertRaisesRegex(RuntimeError, "deletion_audit_constructor_incompatible"):
                namespace["_create_deletion_audit"](StaleService, client, {})
        constructed.assert_not_called()
        self.assertIn("reason=constructor_incompatible", "\n".join(captured.output))

    async def test_T04_missing_guild_me_fetches_only_own_bot_member(self):
        namespace, _client = construction()
        member = SimpleNamespace(id=99)
        guild = SimpleNamespace(me=None, fetch_member=AsyncMock(return_value=member))
        self.assertIs(await namespace["_fetch_deletion_bot_member"](guild), member)
        guild.fetch_member.assert_awaited_once_with(99)
        guild.me = member
        guild.fetch_member.reset_mock()
        self.assertIs(await namespace["_fetch_deletion_bot_member"](guild), member)
        guild.fetch_member.assert_not_awaited()

    async def test_T16_all_four_original_destinations_and_sources_are_preserved(self):
        namespace, _client = construction()
        self.assertEqual(namespace["LOG_ROUTES"], EXPECTED_ROUTES)
        for destination, sources in EXPECTED_ROUTES.items():
            for source in sources:
                self.assertEqual(namespace["CHANNEL_TO_LOG"][source], destination)

    async def test_permission_or_channel_delete_events_invalidate_rest_permission_models(self):
        audit = SimpleNamespace(invalidate_channel=Mock())
        nodes = []
        for node in SOURCE.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in {"on_guild_channel_update", "on_guild_channel_delete"}:
                node.decorator_list = []
                nodes.append(node)
        self.assertEqual(len(nodes), 2)
        namespace = {"DELETION_AUDIT": audit}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
        await namespace["on_guild_channel_update"](SimpleNamespace(id=11), SimpleNamespace(id=11))
        await namespace["on_guild_channel_delete"](SimpleNamespace(id=20))
        self.assertEqual([call.args[0] for call in audit.invalidate_channel.call_args_list], [11, 20])


if __name__ == "__main__":
    unittest.main()
