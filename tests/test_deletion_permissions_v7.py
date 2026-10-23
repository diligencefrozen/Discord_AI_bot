"""Real discord.py 2.7.1 cache/overwrite regression fixtures and containment."""
from __future__ import annotations

import unittest
from itertools import combinations
import random
from unittest.mock import Mock

import discord

from deletion_audit import evaluate_public_destination, public_destination_reason


def permission_overwrite(target_id, *, role=True, **values):
    allow, deny = discord.PermissionOverwrite(**values).pair()
    return {"id": str(target_id), "type": 0 if role else 1,
            "allow": str(allow.value), "deny": str(deny.value)}


def real_channel_pair(*, guild_id=1, source_id=11, destination_id=20,
                      source_overwrites=(), destination_overwrites=(), roles=(),
                      members=(), cache_bot=True, category_sync=False):
    """Construct actual Guild, Role, Member and TextChannel without network I/O."""
    client = discord.Client(intents=discord.Intents.default())
    state = client._connection
    bot_user = {"id": "99", "username": "doribot", "discriminator": "0", "avatar": None, "bot": True}
    state.user = discord.ClientUser(state=state, data=bot_user)
    defaults = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    role_data = [
        {"id": str(guild_id), "name": "@everyone", "permissions": str(defaults.value), "position": 0},
        {"id": "2", "name": "bot", "permissions": str(discord.Permissions(administrator=True).value), "position": 1},
        *roles,
    ]
    member_data = list(members)
    if cache_bot:
        member_data.append({"user": bot_user, "roles": ["2"], "flags": 0})
    channels = [
        {"id": str(source_id), "type": 0, "name": "public-source", "position": 0,
         "permission_overwrites": list(source_overwrites)},
        {"id": str(destination_id), "type": 0, "name": "existing-public-log", "position": 1,
         "permission_overwrites": list(destination_overwrites)},
    ]
    if category_sync:
        category_id = max(source_id, destination_id) + 100
        channels[0]["parent_id"] = str(category_id)
        channels.append({"id": str(category_id), "type": 4, "name": "synced-category", "position": 2,
                         "permission_overwrites": list(source_overwrites)})
    guild = discord.Guild(data={"id": str(guild_id), "name": "regression-guild", "owner_id": "10",
                                "roles": role_data, "members": member_data, "channels": channels}, state=state)
    return guild.get_channel(source_id), guild.get_channel(destination_id), guild


class RealPermissionRegressionTests(unittest.TestCase):
    def test_public_unknown_role_other_permission_no_longer_false_negative(self):
        source, destination, guild = real_channel_pair(source_overwrites=[
            permission_overwrite(404, send_messages=False),
        ])
        target = next(iter(source.overwrites))
        self.assertIsInstance(source, discord.TextChannel)
        self.assertIsInstance(target, discord.Object)
        self.assertIs(target.type, discord.Role)
        self.assertIsNone(guild.get_role(404))
        self.assertTrue(source.permissions_for(guild.default_role).view_channel)
        self.assertTrue(destination.permissions_for(guild.default_role).view_channel)
        self.assertIsNone(public_destination_reason(source, destination, guild.id))
        result = evaluate_public_destination(source, destination, guild.id)
        self.assertEqual(result.status, "PUBLIC_VERIFIED")
        self.assertEqual(result.reason_code, "reader_containment_verified")
        self.assertEqual(result.stage, "complete")
        self.assertIsNone(result.exception_type)

    def test_real_public_models_are_verified(self):
        source, destination, guild = real_channel_pair()
        self.assertIsInstance(guild.default_role, discord.Role)
        self.assertIsInstance(guild.me, discord.Member)
        self.assertTrue(evaluate_public_destination(source, destination, guild.id).allowed)

    def test_actual_synced_category_permissions_are_verified(self):
        source, destination, guild = real_channel_pair(category_sync=True, source_overwrites=[
            permission_overwrite(1, view_channel=True), permission_overwrite(404, send_messages=False),
        ])
        self.assertTrue(source.permissions_synced)
        self.assertTrue(evaluate_public_destination(source, destination, guild.id).allowed)

    def test_uncached_member_object_user_is_accepted_when_scope_safe(self):
        source, destination, guild = real_channel_pair(source_overwrites=[
            permission_overwrite(404, role=False, view_channel=True),
        ])
        target = next(iter(source.overwrites))
        self.assertIsInstance(target, discord.Object)
        self.assertIs(target.type, discord.abc.User)
        self.assertIsNot(target.type, discord.User)
        self.assertTrue(evaluate_public_destination(source, destination, guild.id).allowed)

    def test_explicit_object_member_type_is_accepted_for_compatible_callers(self):
        source, destination, guild = real_channel_pair()
        source_proxy = Mock(spec=discord.TextChannel)
        source_proxy.id, source_proxy.guild = source.id, guild
        source_proxy.permissions_for.side_effect = source.permissions_for
        source_proxy.overwrites = {discord.Object(id=404, type=discord.Member): discord.PermissionOverwrite(view_channel=True)}
        self.assertTrue(evaluate_public_destination(source_proxy, destination, guild.id).allowed)

    def test_known_member_source_denial_protects_private_content(self):
        member = {"user": {"id": "44", "username": "member", "discriminator": "0", "avatar": None}, "roles": [], "flags": 0}
        source, destination, guild = real_channel_pair(members=[member], source_overwrites=[
            permission_overwrite(44, role=False, view_channel=False),
        ])
        # The members intent is disabled, matching production member caching.
        # Supply a real cached member to check the definitive private verdict.
        actual = discord.Member(data=member, guild=guild, state=guild._state)
        guild._add_member(actual)
        self.assertIs(next(iter(source.overwrites)), actual)
        result = evaluate_public_destination(source, destination, guild.id)
        self.assertEqual(result.status, "PRIVATE_VERIFIED")
        self.assertEqual(result.reason_code, "source_member_scope_exceeded")

    def test_unknown_member_denial_is_distinct_from_verified_private(self):
        source, destination, guild = real_channel_pair(source_overwrites=[
            permission_overwrite(44, role=False, view_channel=False),
        ])
        result = evaluate_public_destination(source, destination, guild.id)
        self.assertEqual(result.status, "UNKNOWN_PERMISSIONS")
        self.assertEqual(result.reason_code, "source_member_roles_unavailable")
        self.assertEqual(result.stage, "member_containment")
        self.assertFalse(result.allowed)

    def test_unknown_owner_overwrite_still_respects_owner_bypass(self):
        source, destination, guild = real_channel_pair(source_overwrites=[
            permission_overwrite(10, role=False, view_channel=False),
        ])
        self.assertTrue(evaluate_public_destination(source, destination, guild.id).allowed)

    def test_missing_bot_member_requires_recovery_and_can_use_rest_member(self):
        source, destination, guild = real_channel_pair(cache_bot=False)
        result = evaluate_public_destination(source, destination, guild.id)
        self.assertEqual((result.status, result.reason_code, result.stage),
                         ("UNKNOWN_PERMISSIONS", "bot_member_unavailable", "bot_member"))
        bot_user = {"id": "99", "username": "doribot", "discriminator": "0", "avatar": None, "bot": True}
        recovered = discord.Member(data={"user": bot_user, "roles": ["2"], "flags": 0}, guild=guild, state=guild._state)
        self.assertTrue(evaluate_public_destination(source, destination, guild.id, bot_member=recovered).allowed)
        self.assertIsNone(guild.me)

    def test_unknown_role_source_view_deny_does_not_leak(self):
        source, destination, guild = real_channel_pair(source_overwrites=[permission_overwrite(404, view_channel=False)])
        result = evaluate_public_destination(source, destination, guild.id)
        self.assertEqual(result.status, "UNKNOWN_PERMISSIONS")
        self.assertEqual(result.reason_code, "role_cache_incomplete")
        self.assertFalse(result.allowed)

    def test_unknown_role_matching_deny_is_safe(self):
        overwrite = permission_overwrite(404, view_channel=False)
        source, destination, guild = real_channel_pair(source_overwrites=[overwrite], destination_overwrites=[overwrite])
        self.assertTrue(evaluate_public_destination(source, destination, guild.id).allowed)

    def test_everyone_public_does_not_override_explicit_known_role_denial(self):
        role = {"id": "3", "name": "blocked", "permissions": "0", "position": 2}
        source, destination, guild = real_channel_pair(roles=[role], source_overwrites=[permission_overwrite(3, view_channel=False)])
        self.assertTrue(source.permissions_for(guild.default_role).view_channel)
        result = evaluate_public_destination(source, destination, guild.id)
        self.assertEqual((result.status, result.reason_code), ("PRIVATE_VERIFIED", "source_reader_scope_exceeded"))

    def test_administrator_ignores_role_denial_in_both_channels(self):
        role = {"id": "3", "name": "administrator", "permissions": str(discord.Permissions(administrator=True).value), "position": 2}
        source, destination, guild = real_channel_pair(roles=[role], source_overwrites=[permission_overwrite(3, view_channel=False)])
        self.assertTrue(evaluate_public_destination(source, destination, guild.id).allowed)

    def test_private_everyone_source_stays_restricted(self):
        source, destination, guild = real_channel_pair(source_overwrites=[permission_overwrite(1, view_channel=False)])
        result = evaluate_public_destination(source, destination, guild.id)
        self.assertEqual(result.status, "PRIVATE_VERIFIED")
        self.assertEqual(result.reason_code, "source_reader_scope_exceeded")

    def test_permission_exception_exposes_only_type_and_stage(self):
        source, destination, guild = real_channel_pair()
        proxy = Mock(spec=discord.TextChannel)
        proxy.id, proxy.guild = destination.id, guild
        proxy.permissions_for.side_effect = RuntimeError("SECRET message https://cdn.invalid/a token")
        result = evaluate_public_destination(source, proxy, guild.id)
        self.assertEqual((result.status, result.reason_code, result.stage, result.exception_type),
                         ("UNKNOWN_PERMISSIONS", "permission_evaluation_failed", "destination_bot_permissions", "RuntimeError"))
        self.assertNotIn("SECRET", repr(result))
        self.assertNotIn("https", repr(result))

    def test_wrong_guild_and_missing_channels_have_precise_codes(self):
        source, destination, guild = real_channel_pair()
        self.assertEqual(evaluate_public_destination(None, destination, guild.id).status, "SOURCE_MISSING")
        self.assertEqual(evaluate_public_destination(source, None, guild.id).status, "DESTINATION_MISSING")
        self.assertEqual(evaluate_public_destination(source, destination, 555).status, "SOURCE_GUILD_MISMATCH")

    def test_containment_matches_real_permissions_for_every_role_combination(self):
        rng = random.Random(271)
        for case in range(36):
            role_data = []
            for role_id in range(3, 7):
                permissions = discord.Permissions(view_channel=rng.choice((True, False)),
                                                  administrator=rng.choice((True, False, False, False)))
                role_data.append({"id": str(role_id), "name": f"role-{role_id}",
                                  "permissions": str(permissions.value), "position": role_id})
            overwrite_data = []
            for _ in range(2):
                overwrite_data.append([permission_overwrite(role_id, view_channel=value)
                                       for role_id in (1, 3, 4, 5, 6)
                                       if (value := rng.choice((None, False, True))) is not None])
            source, destination, guild = real_channel_pair(roles=role_data,
                                                          source_overwrites=overwrite_data[0],
                                                          destination_overwrites=overwrite_data[1])
            counterexample = False
            for count in range(5):
                for assigned in combinations(range(3, 7), count):
                    actual = discord.Member(data={"user": {"id": "44", "username": "reader", "discriminator": "0", "avatar": None},
                                                  "roles": [str(item) for item in assigned], "flags": 0},
                                            guild=guild, state=guild._state)
                    if destination.permissions_for(actual).view_channel and not source.permissions_for(actual).view_channel:
                        counterexample = True
            with self.subTest(case=case):
                self.assertEqual(evaluate_public_destination(source, destination, guild.id).allowed, not counterexample)


if __name__ == "__main__":
    unittest.main()
