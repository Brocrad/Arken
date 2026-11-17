"""Foundation tests for multi-server isolation (arken_tenancy). Run: python -m unittest tests.test_tenancy -v"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import arken_tenancy as t


class PermissionsTests(unittest.TestCase):
    def test_global_control(self):
        self.assertTrue(t.can_control(global_control=True, is_admin=False, has_dj_role=False, open_play=False))

    def test_legacy_is_owner_kwarg(self):
        self.assertTrue(t.can_control(is_owner=True, is_admin=False, has_dj_role=False, open_play=False))

    def test_open_play(self):
        self.assertTrue(t.can_control(global_control=False, is_admin=False, has_dj_role=False, open_play=True))

    def test_nobody(self):
        self.assertFalse(t.can_control(global_control=False, is_admin=False, has_dj_role=False, open_play=False))

    def test_role_capabilities(self):
        self.assertIn("manage_staff", t.role_capabilities("owner"))
        self.assertIn("global_control", t.role_capabilities("owner"))
        self.assertNotIn("manage_staff", t.role_capabilities("operator"))
        self.assertNotIn("global_control", t.role_capabilities("operator"))
        self.assertTrue(t.has_capability("operator", "manage_guilds"))
        # legacy "trusted" maps to operator — still no cross-server DJ
        self.assertEqual(t.normalize_staff_role("trusted"), "operator")
        self.assertNotIn("global_control", t.role_capabilities("trusted"))

    def test_server_admin(self):
        self.assertTrue(t.can_server_admin(is_staff_owner=True, is_guild_admin=False))
        self.assertTrue(t.can_server_admin(is_staff_owner=False, is_guild_admin=True))
        self.assertFalse(t.can_server_admin(is_staff_owner=False, is_guild_admin=False))


class StaffDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "user_whitelist.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_migrates_to_owner(self):
        self.path.write_text(json.dumps({"authorized_users": [111, 222]}), encoding="utf-8")
        staff = t.StaffDirectory(self.path)
        self.assertEqual(staff.role_of(111), "owner")
        self.assertEqual(staff.role_of(222), "owner")
        staff.set_role(222, "operator")
        again = t.StaffDirectory(self.path)
        self.assertEqual(again.role_of(222), "operator")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 2)
        self.assertIn(111, raw["authorized_users"])

    def test_capabilities(self):
        staff = t.StaffDirectory(self.path)
        staff.set_role(1, "owner")
        staff.set_role(2, "operator")
        self.assertTrue(staff.has(1, "manage_staff"))
        self.assertTrue(staff.has(1, "global_control"))
        self.assertTrue(staff.has(2, "manage_runtime"))
        self.assertFalse(staff.has(2, "manage_staff"))
        self.assertFalse(staff.has(2, "global_control"))
        self.assertTrue(staff.has(2, "diagnose"))


class PresenceTests(unittest.TestCase):
    def test_single_server_shows_title(self):
        self.assertEqual(t.presence_title(1, ["Song A"]), "Song A")

    def test_multi_server_neutral(self):
        self.assertIsNone(t.presence_title(2, ["Song A", "Song B"]))

    def test_empty(self):
        self.assertIsNone(t.presence_title(1, []))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "allowed_guilds.json"
        self.reg = t.GuildRegistry(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_then_seen_pending(self):
        n = self.reg.seed([(111, "Home")])
        self.assertEqual(n, 1)
        self.assertTrue(self.reg.is_approved(111))
        # second seed is a no-op
        self.assertEqual(self.reg.seed([(222, "Other")]), 0)
        status = self.reg.seen(222, "Friend")
        self.assertEqual(status, "pending")
        self.assertFalse(self.reg.is_approved(222))
        self.reg.approve(222, "Friend")
        self.assertTrue(self.reg.is_approved(222))
        self.reg.revoke(222)
        self.assertEqual(self.reg.status(222), "revoked")

    def test_persists(self):
        self.reg.approve(999, "X")
        again = t.GuildRegistry(self.path)
        self.assertTrue(again.is_approved(999))


class LikesIsolationTests(unittest.TestCase):
    def test_likes_do_not_cross_guilds(self):
        data = {}
        dest = t.likes_list(data, 1, 42, create=True)
        dest.append({"title": "Only in guild 1", "vid": "aaa"})
        self.assertEqual(t.likes_list(data, 1, 42), [{"title": "Only in guild 1", "vid": "aaa"}])
        self.assertEqual(t.likes_list(data, 2, 42), [])  # same user, other server
        self.assertEqual(t.guild_likes_pool(data, 2), [])
        self.assertEqual(len(t.guild_likes_pool(data, 1)), 1)

    def test_migrate_likes_to_home_only(self):
        data = {
            "users": {
                "42": {"likes": [{"title": "Old", "vid": "v1"}]},
                "99": {"likes": [{"title": "Other", "vid": "v2"}]},
            },
            "guilds": {"100": {"plays": []}},
        }
        moved = t.migrate_likes(data, 100)
        self.assertEqual(moved, 2)
        self.assertNotIn("users", data)
        self.assertEqual(len(t.likes_list(data, 100, 42)), 1)
        self.assertEqual(t.likes_list(data, 200, 42), [])  # not copied to other guilds
        # idempotent
        self.assertEqual(t.migrate_likes(data, 100), 0)

    def test_home_guild_resolution(self):
        self.assertEqual(t.resolve_home_guild("731372434690670633", []), 731372434690670633)
        self.assertEqual(t.resolve_home_guild(None, ["100"]), 100)
        self.assertIsNone(t.resolve_home_guild(None, ["100", "200"]))
        self.assertIsNone(t.resolve_home_guild(None, []))


class PlaylistTests(unittest.TestCase):
    def test_scope_path(self):
        gid = 731372434690670633
        self.assertEqual(
            t.scope_playlist_path(r"playlists\groovy\x.webm", gid),
            f"playlists/{gid}/groovy/x.webm",
        )
        already = f"playlists/{gid}/groovy/x.webm"
        self.assertEqual(t.scope_playlist_path(already, gid), already)

    def test_migrate_playlists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "groovy.json").write_text(
                json.dumps({
                    "name": "groovy",
                    "files": [{"title": "A", "audio_file": r"playlists\groovy\a.webm",
                               "metadata_file": r"playlists\groovy\a.json"}],
                }),
                encoding="utf-8",
            )
            (root / "groovy").mkdir()
            (root / "groovy" / "a.webm").write_bytes(b"x")
            home = 731372434690670633
            r = t.migrate_playlists(root, home)
            self.assertEqual(r["moved"], 2)
            self.assertTrue((root / str(home) / "groovy.json").is_file())
            self.assertTrue((root / str(home) / "groovy" / "a.webm").is_file())
            # re-run: no double-move
            r2 = t.migrate_playlists(root, home)
            self.assertEqual(r2["moved"], 0)
            data = json.loads((root / str(home) / "groovy.json").read_text(encoding="utf-8"))
            self.assertTrue(data["files"][0]["audio_file"].startswith(f"playlists/{home}/"))

    def test_purge(self):
        data = {"guilds": {"1": {"likes": {}}, "2": {"likes": {}}}}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            t.playlist_dir(root, 1).mkdir(parents=True, exist_ok=True)
            (t.playlist_dir(root, 1) / "x.json").write_text("{}", encoding="utf-8")
            removed = t.purge_guild_data(data, root, 1)
            self.assertTrue(removed["data"])
            self.assertTrue(removed["playlists_dir"])
            self.assertIn("2", data["guilds"])
            self.assertNotIn("1", data["guilds"])


class StartupMigrationTests(unittest.TestCase):
    def test_run_with_env_home(self):
        data = {"users": {"7": {"likes": [{"title": "T", "vid": "v"}]}}, "guilds": {}}
        with tempfile.TemporaryDirectory() as td:
            report = t.run_data_migration(
                data, settings={}, playlists_root=td, home_guild_env="555"
            )
            self.assertEqual(report["home_guild"], 555)
            self.assertEqual(report["likes_moved"], 1)
            self.assertEqual(t.likes_list(data, 555, 7)[0]["vid"], "v")


class StaffMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "user_whitelist.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_adds_without_clobber(self):
        a = t.StaffDirectory(self.path)
        a.set_role(1, "owner")
        b = t.StaffDirectory(self.path)
        b.set_role(2, "operator")
        c = t.StaffDirectory(self.path)
        self.assertEqual(c.role_of(1), "owner")
        self.assertEqual(c.role_of(2), "operator")

    def test_merge_owner_wins(self):
        self.path.write_text(
            json.dumps({"version": 2, "users": {"5": {"role": "operator"}}}),
            encoding="utf-8",
        )
        a = t.StaffDirectory(self.path)
        a.set_role(5, "owner")
        b = t.StaffDirectory(self.path)
        self.assertEqual(b.role_of(5), "owner")

    def test_merge_remove_propagates(self):
        a = t.StaffDirectory(self.path)
        a.set_role(9, "operator")
        a.remove(9)
        b = t.StaffDirectory(self.path)
        self.assertIsNone(b.role_of(9))


class PoolHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_heartbeat_alive_self_always(self):
        self.assertTrue(
            t.token_container_alive(self.state, "gondor", local_token_id="gondor", max_age=10)
        )

    def test_heartbeat_stale_other(self):
        t.write_pool_heartbeat(self.state, "osgiliath")
        self.assertTrue(
            t.token_container_alive(self.state, "osgiliath", local_token_id="gondor", max_age=90)
        )
        path = t.pool_heartbeat_path(self.state, "osgiliath")
        path.write_text(str(time.time() - 200), encoding="utf-8")
        self.assertFalse(
            t.token_container_alive(self.state, "osgiliath", local_token_id="gondor", max_age=90)
        )

    def test_pick_token_skips_dead(self):
        pool_path = Path(self.tmp.name) / "token_pool.json"
        alive = {"t0": True, "t1": False}

        def filt(tid):
            return alive.get(tid, False)

        pool = t.TokenPool(pool_path, token_ids=["t0", "t1"], max_guilds_per_token=2, alive_filter=filt)
        pool.assign(100, token_id="t0")
        self.assertEqual(pool.assign(101), "t0")
        with self.assertRaises(t.PoolFullError) as ctx:
            pool.assign(102)
        self.assertIn("heartbeat", str(ctx.exception).lower())


class TokenPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "token_pool.json"
        self.pool = t.TokenPool(
            self.path,
            token_ids=["t0", "t1"],
            max_guilds_per_token=2,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_env(self):
        self.assertEqual(t.parse_token_pool_env("t0, t1;t2"), ["t0", "t1", "t2"])
        self.assertEqual(t.parse_token_pool_env(""), [])

    def test_member_weight_compresses(self):
        self.assertLess(t.member_weight(1000), 1000)
        self.assertGreater(t.member_weight(1000), t.member_weight(10))

    def test_least_loaded_and_sticky(self):
        a = self.pool.assign(100, members=10, name="A")
        b = self.pool.assign(200, members=10, name="B")
        self.assertEqual(a, "t0")
        self.assertEqual(b, "t1")  # t0 and t1 both empty → pool order, then balance
        # Fill t0 to cap after giving t1 one more small guild... 
        # After A on t0 and B on t1, next goes to lower score.
        c = self.pool.assign(300, members=5)
        # Sticky: re-assign keeps token
        self.assertEqual(self.pool.assign(100, members=99), a)
        self.assertEqual(self.pool.token_of(100), a)

    def test_prefers_fewer_members_when_guild_counts_equal(self):
        self.pool.assign(1, token_id="t0", members=5000)
        self.pool.assign(2, token_id="t1", members=5)
        # Both have 1 guild; t1 has far lower member weight → pick t1
        picked = self.pool.assign(3, members=10)
        self.assertEqual(picked, "t1")

    def test_cap_raises(self):
        self.pool.assign(1, token_id="t0")
        self.pool.assign(2, token_id="t0")
        self.pool.assign(3, token_id="t1")
        self.pool.assign(4, token_id="t1")
        with self.assertRaises(t.PoolFullError):
            self.pool.assign(5)

    def test_active_players_tilt_score(self):
        self.pool.assign(1, token_id="t0", members=10)
        self.pool.assign(2, token_id="t1", members=10)
        self.pool.set_runtime("t0", active_players=2)
        self.pool.set_runtime("t1", active_players=0)
        self.assertGreater(self.pool.score("t0"), self.pool.score("t1"))
        self.assertEqual(self.pool.assign(3), "t1")

    def test_release_and_persist(self):
        self.pool.assign(42, token_id="t0")
        self.assertTrue(self.pool.release(42))
        again = t.TokenPool(self.path, token_ids=["t0", "t1"], max_guilds_per_token=2)
        self.assertIsNone(again.token_of(42))

    def test_snapshot(self):
        self.pool.assign(1, token_id="t0", members=100)
        snap = self.pool.snapshot()
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["assigned_guilds"], 1)
        self.assertEqual(snap["tokens"][0]["guilds"], 1)
        self.assertTrue(snap["tokens"][0]["seats"][0]["callsign"])


class CallsignSeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "token_pool.json"
        self.pool = t.TokenPool(
            self.path,
            token_ids=["gondor", "osgiliath"],
            max_guilds_per_token=5,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_seat_label(self):
        self.assertEqual(t.seat_label("osgiliath", "east-bank"), "Osgiliath · East Bank")
        self.assertEqual(t.seat_label("gondor", "rath-dinen"), "Gondor · Rath Dínen")

    def test_auto_callsigns_unique_per_token(self):
        self.pool.assign(10, token_id="gondor")
        self.pool.assign(11, token_id="gondor")
        self.assertEqual(self.pool.callsign_of(10), "citadel")
        self.assertEqual(self.pool.callsign_of(11), "fountain-court")
        self.assertEqual(self.pool.seat_of(10), "Gondor · Citadel")
        # Same callsign may exist on another city — different token
        self.pool.assign(20, token_id="osgiliath")
        self.assertEqual(self.pool.callsign_of(20), "east-bank")
        self.assertEqual(self.pool.seat_of(20), "Osgiliath · East Bank")

    def test_sticky_seat(self):
        self.pool.assign(10, token_id="gondor")
        seat = self.pool.seat_of(10)
        self.pool.assign(10, token_id="osgiliath")  # sticky: ignore new token request
        self.assertEqual(self.pool.token_of(10), "gondor")
        self.assertEqual(self.pool.seat_of(10), seat)

    def test_backfill_missing_callsign(self):
        # Simulate legacy row without callsign
        self.pool.assignments["99"] = {"token_id": "gondor", "members": 0}
        self.pool.save()
        self.pool.assign(99)
        self.assertEqual(self.pool.callsign_of(99), "citadel")

    def test_release_frees_callsign(self):
        self.pool.assign(1, token_id="gondor")
        self.pool.release(1)
        self.pool.assign(2, token_id="gondor")
        self.assertEqual(self.pool.callsign_of(2), "citadel")


class GifThemeTests(unittest.TestCase):
    def test_seat_terminal_theme(self):
        import gif_art as g

        theme = g.GuildTheme.from_seat("Gondor · Citadel", "citadel", "gondor")
        self.assertEqual(theme.prompt, "citadel@gondor")
        self.assertIn("Gondor", theme.title)
        self.assertEqual(theme.summon, "citadel")
        self.assertEqual(theme.badge_brand(), "GONDOR CITADEL")


class MetricsTests(unittest.TestCase):
    def test_render_prometheus(self):
        import arken_metrics as m

        text = m.render_pool_prometheus(
            {
                "max_guilds_per_token": 5,
                "token_count": 1,
                "assigned_guilds": 1,
                "tokens": [{
                    "token_id": "t0",
                    "guilds": 1,
                    "members_total": 10,
                    "member_weight": 2.4,
                    "active_players": 1,
                    "active_ratio": 1.0,
                    "slots_free": 4,
                    "load_score": 20.4,
                    "at_cap": False,
                }],
            },
            token_id="t0",
        )
        self.assertIn("arken_token_guilds{", text)
        text2 = m.render_process_prometheus(
            token_id="gondor",
            guilds_served=2,
            active_players=1,
            voice_connected=1,
            heartbeat_age=3.5,
        )
        self.assertIn("arken_process_active_players", text2)
        self.assertIn('token_id="t0"', text)
        self.assertIn("arken_process_token_info", text)


if __name__ == "__main__":
    unittest.main()
