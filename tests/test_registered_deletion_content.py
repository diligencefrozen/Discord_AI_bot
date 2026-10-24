"""Operator-approved registered sources publish known originals by default."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import discord
from deletion_audit import DeletionAuditService

try:
    from tests.test_deletion_pipeline_v7 import public_pair, make_message, raw
except ModuleNotFoundError:
    from test_deletion_pipeline_v7 import public_pair, make_message, raw


class RegisteredDeletionContentTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, *, registered=None, routes=None, include_content=True, private_ids="", **options):
        source, destination = public_pair(source_id=937715555232780318, destination_id=1064823080100306995)
        source.name = "moderator-only"
        source.overwrites[source.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        channels = {source.id: source, destination.id: destination}
        with patch.dict("os.environ", {"DELETION_PUBLIC_LOG_PRIVATE_FULL_SOURCE_IDS": private_ids}):
            with patch.dict(os.environ):
                if registered is None:
                    os.environ.pop("DELETION_PUBLIC_LOG_REGISTERED_SOURCE_CONTENT", None)
                else:
                    os.environ["DELETION_PUBLIC_LOG_REGISTERED_SOURCE_CONTENT"] = registered
                service = DeletionAuditService(routes if routes is not None else {source.id: destination.id}, channels.get,
                                               enabled=True, include_content=include_content, all_guild_channels=True, **options)
        return service, source, destination, channels

    async def test_default_private_moderator_only_logs_known_original_for_public_readers(self):
        service, source, destination, _ = self.make_service()
        target = make_message(source, "시321133발")
        target.author.display_name = "Apple Watch"
        await service.delete(target, reason="profanity", matched_rule="시발")
        target.delete.assert_awaited_once()
        destination.send.assert_awaited_once()
        rendered = str(destination.send.call_args.kwargs["embed"].to_dict())
        for expected in ("<@50>", "<#937715555232780318>", "시321133발", "시발"):
            self.assertIn(expected, rendered)
        self.assertNotIn("원문 비공개", rendered)
        self.assertEqual(service.diagnostics(source.guild)["sent_full"], 1)
        self.assertEqual(destination.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_global_content_off_overrides_registered_default_and_private_allowlist(self):
        service, source, destination, _ = self.make_service(include_content=False, private_ids="937715555232780318")
        await service.on_message_delete(make_message(source, "DO-NOT-PUBLISH"))
        rendered = str(destination.send.call_args.kwargs["embed"].to_dict())
        self.assertNotIn("DO-NOT-PUBLISH", rendered)
        self.assertEqual(service.diagnostics(1)["sent_full"], 0)
        self.assertEqual(service.diagnostics(1)["sent_metadata_only"], 1)

    async def test_all_eighteen_existing_source_channels_publish_known_originals(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))
        routes = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(name, ast.Name) and name.id == "LOG_ROUTES" for name in node.targets))
        mapping = {source_id: destination_id for destination_id, source_ids in routes.items() for source_id in source_ids}
        checked = 0
        for destination_id, source_ids in routes.items():
            for source_id in source_ids - {destination_id}:
                with self.subTest(source=source_id, destination=destination_id), patch.dict(os.environ):
                    os.environ.pop("DELETION_PUBLIC_LOG_REGISTERED_SOURCE_CONTENT", None)
                    source, destination = public_pair(source_id=source_id, destination_id=destination_id)
                    source.overwrites[source.guild.default_role] = discord.PermissionOverwrite(view_channel=False)
                    service = DeletionAuditService(mapping, {source_id: source, destination_id: destination}.get,
                                                   enabled=True, include_content=True)
                    await service.on_message_delete(make_message(source, "등록 원본 보존"))
                    destination.send.assert_awaited_once()
                    self.assertIn("등록 원본 보존", str(destination.send.call_args.kwargs["embed"].to_dict()))
                    self.assertEqual(service.diagnostics(1)["sent_full"], 1)
                    checked += 1
        self.assertEqual(checked, 18)

    async def test_unregistered_private_is_not_authorized_by_registered_policy(self):
        service, source, destination, _ = self.make_service(routes={12: 1064823080100306995})
        await service.on_message_delete(make_message(source, "PRIVATE-UNROUTED"))
        destination.send.assert_not_awaited()
        self.assertEqual(service.diagnostics(1)["last_reason"], "private_unrouted_skipped")

    async def test_zero_and_invalid_registered_flag_keep_selective_policy(self):
        for value in ("0", "invalid", '"1"', "", "2"):
            with self.subTest(value=value):
                service, source, destination, _ = self.make_service(registered=value)
                await service.on_message_delete(make_message(source, "PRIVATE-ORIGINAL"))
                self.assertNotIn("PRIVATE-ORIGINAL", str(destination.send.call_args.kwargs["embed"].to_dict()))
                self.assertFalse(service.registered_source_content)
                self.assertEqual(service.diagnostics(1)["sent_metadata_only"], 1)

    async def test_opt_out_retains_existing_per_source_private_allowlist(self):
        service, source, destination, _ = self.make_service(registered="0", private_ids="937715555232780318")
        await service.on_message_delete(make_message(source, "EXPLICIT-APPROVED"))
        self.assertIn("EXPLICIT-APPROVED", str(destination.send.call_args.kwargs["embed"].to_dict()))
        self.assertEqual(service.diagnostics(1)["last_reason"], "private_full_opt_in")

    async def test_known_snapshot_survives_source_lookup_miss_without_inventing_raw_content(self):
        service, source, destination, channels = self.make_service()
        target = make_message(source, "KNOWN-BEFORE-DELETE")
        service.remember(target)
        channels.pop(source.id)
        await service.on_raw_message_delete(raw(source, cached_message=None))
        self.assertIn("KNOWN-BEFORE-DELETE", str(destination.send.call_args.kwargs["embed"].to_dict()))
        self.assertEqual(service.diagnostics(1)["sent_full"], 1)
        destination.send.reset_mock()
        await service.on_raw_message_delete(raw(source, message_id=101, cached_message=None))
        rendered = str(destination.send.call_args.kwargs["embed"].to_dict())
        self.assertNotIn("KNOWN-BEFORE-DELETE", rendered)
        self.assertIn("확인 불가", rendered)
        self.assertEqual(service.diagnostics(1)["sent_metadata_only"], 1)

    async def test_registered_policy_never_bypasses_destination_or_source_identity(self):
        service, source, destination, _ = self.make_service()
        target = make_message(source, "SECRET-ON-WRONG-GUILD")
        source.guild = SimpleNamespace(id=2)
        await service.on_message_delete(target)
        destination.send.assert_not_awaited()
        self.assertEqual(service.diagnostics(1)["last_reason"], "source_guild_mismatch")

    async def test_metadata_body_not_logged_and_permission_failure_keeps_enforcement(self):
        service, source, destination, _ = self.make_service()
        class Forbidden(Exception):
            status = 403
        destination.send.side_effect = Forbidden("DO-NOT-LOG-RESPONSE")
        target = make_message(source, "DO-NOT-LOG-ORIGINAL")
        with self.assertLogs("deletion_audit", level="INFO") as captured:
            self.assertTrue(await service.delete(target, reason="profanity", matched_rule="SECRET-MATCH"))
        console = "\n".join(captured.output)
        for secret in (target.content, "SECRET-MATCH", "DO-NOT-LOG-RESPONSE"):
            self.assertNotIn(secret, console)
        self.assertEqual(service.diagnostics(1)["failed"], 1)


if __name__ == "__main__":
    unittest.main()
