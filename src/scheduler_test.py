import unittest
from unittest import mock
import json
import os
import datetime

import scheduler
import util


class MockDate(datetime.date):
    _today_val = datetime.date(2026, 7, 6)

    @classmethod
    def today(cls):
        return cls._today_val


class MockMember:
    def __init__(self, id, name, nick=None):
        self.id = id
        self.name = name
        self.nick = nick


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self.users = [
            MockMember(111, 'Alice', 'Aly'),
            MockMember(222, 'Bob'),
            MockMember(333, 'Charlie', 'Chaz'),
            MockMember(444, 'David')
        ]
        self.test_data_dir = util.get_data_folder()
        self.state_file = self.test_data_dir / 'schedule.json'
        if self.state_file.exists():
            os.remove(self.state_file)

    def tearDown(self):
        if self.state_file.exists():
            os.remove(self.state_file)

    def make_scheduler(self, users=None):
        """Create a scheduler and load state — mirrors the real bot startup."""
        sch = scheduler.Scheduler()
        sch.load_state(users if users is not None else self.users)
        return sch

    def today(self):
        return datetime.date.today()

    # ------------------------------------------------------------------
    # Basic rotation
    # ------------------------------------------------------------------

    def test_on_call(self):
        sch = self.make_scheduler()
        # Rotation [Alice, Bob, Charlie, David], start_date = today → Alice is day 0
        self.assertEqual(sch.on_call.id, 111)

    def test_get_user_for_day(self):
        sch = self.make_scheduler()
        self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
        self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob
        self.assertEqual(sch.get_user_for_day(2).id, 333)  # Charlie
        self.assertEqual(sch.get_user_for_day(3).id, 444)  # David
        # Day 4 is beyond the current rotation — next rotation starts with
        # the same member_list, so Alice wraps around first again.
        self.assertEqual(sch.get_user_for_day(4).id, 111)

    def test_start_date_drives_rotation(self):
        """Rotation is anchored to start_date, not a counter."""
        sch = self.make_scheduler()
        self.assertEqual(sch.rotation_start_date, self.today().isoformat())
        sch2 = self.make_scheduler()
        self.assertEqual(sch2.rotation_start_date, sch.rotation_start_date)
        self.assertEqual(sch2.get_user_for_day(0).id, 111)

    def test_all_active_on_fresh_init(self):
        """Every user is marked is_active=True on a fresh scheduler."""
        sch = self.make_scheduler()
        for i in range(4):
            self.assertTrue(sch.get_user_for_day(i).is_active)

    # ------------------------------------------------------------------
    # member_list update rules
    # ------------------------------------------------------------------

    def test_member_list_initialized_from_users(self):
        sch = self.make_scheduler()
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 333, 444])

    def test_remove_user_drops_from_member_list(self):
        sch = self.make_scheduler()
        new_users = [
            MockMember(111, 'Alice', 'Aly'),
            MockMember(333, 'Charlie', 'Chaz'),
            MockMember(444, 'David'),
        ]
        sch.load_state(new_users)
        self.assertEqual([u.id for u in sch.member_list], [111, 333, 444])

    def test_add_user_appends_to_member_list(self):
        sch = self.make_scheduler()
        new_users = self.users + [MockMember(555, 'Eve')]
        sch.load_state(new_users)
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 333, 444, 555])

    def test_member_list_order_preserved_on_reload(self):
        """Saved order of member_list survives a reload."""
        sch = self.make_scheduler()
        sch2 = self.make_scheduler()
        self.assertEqual(
            [u.id for u in sch2.member_list],
            [u.id for u in sch.member_list],
        )

    # ------------------------------------------------------------------
    # Rotation immutability on membership changes
    # ------------------------------------------------------------------

    def test_remove_user_stays_in_rotation(self):
        """A removed user stays in rotation_users and is marked inactive."""
        sch = self.make_scheduler()
        # Remove Bob (id=222).
        new_users = [
            MockMember(111, 'Alice', 'Aly'),
            MockMember(333, 'Charlie', 'Chaz'),
            MockMember(444, 'David'),
        ]
        sch.load_state(new_users)

        rotation_ids = [e['id'] for e in sch.rotation_users]
        self.assertIn(222, rotation_ids, 'Bob should remain in rotation_users')

        # Day 1 is still Bob — but marked N/A.
        bob = sch.get_user_for_day(1)
        self.assertEqual(bob.id, 222)
        self.assertFalse(bob.is_active)
        self.assertEqual(bob.name, 'Bob')

        # Alice on day 0 is still active.
        alice = sch.get_user_for_day(0)
        self.assertEqual(alice.id, 111)
        self.assertTrue(alice.is_active)

    def test_remove_user_rotation_length_unchanged(self):
        """Removing a user does not shorten the current rotation."""
        sch = self.make_scheduler()
        original_len = len(sch.rotation_users)
        sch.load_state([MockMember(111, 'Alice'), MockMember(333, 'Charlie')])
        self.assertEqual(len(sch.rotation_users), original_len)

    def test_add_user_not_in_current_rotation(self):
        """A newly-added user does not appear in the current rotation."""
        sch = self.make_scheduler()
        new_users = self.users + [MockMember(555, 'Eve')]
        sch.load_state(new_users)

        rotation_ids = [e['id'] for e in sch.rotation_users]
        self.assertNotIn(555, rotation_ids)
        # But Eve is in the member_list.
        self.assertIn(555, [u.id for u in sch.member_list])

    def test_add_user_appears_in_next_rotation(self):
        """After the current rotation completes, new users appear in the next one."""
        sch = self.make_scheduler()  # 4-user rotation, start = today

        new_users = self.users + [MockMember(555, 'Eve')]
        sch.load_state(new_users)

        # Days 0-3 belong to the current rotation. Day 4 is the first day of
        # the next rotation (approximated with member_list = all 5 users).
        day4_user = sch.get_user_for_day(4)
        # Next rotation: [Alice, Bob, Charlie, David, Eve]; day 0 = Alice.
        self.assertEqual(day4_user.id, 111)

        # Day 8 = Eve (index 4 in next rotation).
        day8_user = sch.get_user_for_day(8)
        self.assertEqual(day8_user.id, 555)
        self.assertTrue(day8_user.is_active)

    def test_nick_preserved_for_active_users(self):
        """Active users expose their .nick via the returned SimpleNamespace."""
        sch = self.make_scheduler()
        alice = sch.get_user_for_day(0)
        self.assertEqual(alice.nick, 'Aly')

    def test_nick_none_for_na_users(self):
        """N/A (removed) users have .nick = None."""
        sch = self.make_scheduler()
        sch.load_state([MockMember(111, 'Alice', 'Aly')])  # remove all but Alice
        bob = sch.get_user_for_day(1)  # Bob is N/A
        self.assertIsNone(bob.nick)

    # ------------------------------------------------------------------
    # Rotation advancement
    # ------------------------------------------------------------------

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_rotation_advances_after_full_cycle(self):
        """After N days the rotation auto-advances and pulls in member_list."""
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()  # 4-user rotation, start = 2026-07-06

        # Remove Bob, add Eve — member_list becomes [Alice, Charlie, David, Eve].
        new_users = [
            MockMember(111, 'Alice', 'Aly'),
            MockMember(333, 'Charlie', 'Chaz'),
            MockMember(444, 'David'),
            MockMember(555, 'Eve'),
        ]
        sch.load_state(new_users)

        # Advance clock by 4 days (one full rotation cycle completed).
        MockDate._today_val = datetime.date(2026, 7, 10)
        sch._maybe_advance_rotation()

        new_ids = [e['id'] for e in sch.rotation_users]
        self.assertNotIn(222, new_ids, 'Bob should not be in the new rotation')
        self.assertIn(555, new_ids, 'Eve should be in the new rotation')
        self.assertEqual(sch.rotation_start_date, '2026-07-10')

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_rotation_advances_across_multiple_cycles(self):
        """Bot offline for multiple full cycles still advances to the correct date."""
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()  # 4-user rotation

        # 9 days later → 2 full cycles (9 // 4 = 2).
        # New start = 2026-07-06 + 8 days = 2026-07-14.
        MockDate._today_val = datetime.date(2026, 7, 15)
        sch._maybe_advance_rotation()

        self.assertEqual(sch.rotation_start_date, '2026-07-14')

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_rotation_does_not_advance_mid_cycle(self):
        """_maybe_advance_rotation is a no-op while the cycle is still active."""
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()
        original_start = sch.rotation_start_date

        MockDate._today_val = datetime.date(2026, 7, 8)  # day 2 of 4
        sch._maybe_advance_rotation()

        self.assertEqual(sch.rotation_start_date, original_start)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_human_readable_serialization(self):
        """Both member_list and rotation blocks are written to disk correctly."""
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()
        sch.save_state()

        self.assertTrue(sch._state_file.exists())
        with open(sch._state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # member_list block.
        ml = data.get('member_list', [])
        self.assertEqual(len(ml), len(self.users))
        for entry in ml:
            self.assertIn('id', entry)
            self.assertIn('name', entry)
            orig = next(u for u in self.users if u.id == entry['id'])
            self.assertEqual(entry['name'], orig.name)

        # rotation block.
        rot = data.get('rotation', {})
        self.assertIn('start_date', rot)
        self.assertIn('users', rot)
        self.assertEqual(len(rot['users']), len(self.users))
        self.assertEqual(rot['start_date'], '2026-07-06')

        # Reload and verify both structures round-trip correctly.
        sch2 = self.make_scheduler()
        self.assertEqual(
            [u.id for u in sch2.member_list], [u.id for u in self.users])
        self.assertEqual(
            [e['id'] for e in sch2.rotation_users], [u.id for u in self.users])

    def test_migration_from_legacy_format(self):
        """Old base_queue format is migrated; rotation is seeded from it."""
        legacy_data = {
            'start_date': '2026-01-01',
            'base_queue': [
                {'id': 111, 'name': 'Alice'},
                {'id': 222, 'name': 'Bob'},
                {'id': 333, 'name': 'Charlie'},
                {'id': 444, 'name': 'David'},
            ]
        }
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(legacy_data, f)

        sch = self.make_scheduler()

        # rotation_users is seeded from the legacy base_queue order.
        self.assertEqual(
            [e['id'] for e in sch.rotation_users], [111, 222, 333, 444])
        # start_date is reset to today on migration.
        self.assertEqual(sch.rotation_start_date, datetime.date.today().isoformat())
        # member_list is drawn from the current users argument.
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 333, 444])

    # ------------------------------------------------------------------
    # generate_schedule N/A display
    # ------------------------------------------------------------------

    def test_generate_schedule_shows_na_for_removed_user(self):
        """Removed users are shown as 'Name (N/A)' in the schedule output."""
        sch = self.make_scheduler()
        # Remove Bob.
        sch.load_state([
            MockMember(111, 'Alice', 'Aly'),
            MockMember(333, 'Charlie', 'Chaz'),
            MockMember(444, 'David'),
        ])
        output = sch.generate_schedule()
        self.assertIn('Bob (N/A)', output)
        self.assertNotIn('Alice (N/A)', output)

    def test_generate_schedule_active_users_no_na(self):
        """No (N/A) marker appears when all rotation users are still active."""
        sch = self.make_scheduler()
        output = sch.generate_schedule()
        self.assertNotIn('(N/A)', output)


if __name__ == '__main__':
    unittest.main()