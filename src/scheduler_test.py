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
            MockMember(444, 'David'),
            MockMember(555, 'Eve'),
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
    # Basic rotation access
    # ------------------------------------------------------------------

    def test_on_call(self):
        sch = self.make_scheduler()
        self.assertEqual(sch.on_call.id, 111)  # Alice is day 0

    def test_get_user_for_day(self):
        sch = self.make_scheduler()
        self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
        self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob
        self.assertEqual(sch.get_user_for_day(2).id, 333)  # Charlie
        self.assertEqual(sch.get_user_for_day(3).id, 444)  # David
        self.assertEqual(sch.get_user_for_day(4).id, 555)  # Eve
        # Day 5 extends; round-robin restarts from Alice.
        self.assertEqual(sch.get_user_for_day(5).id, 111)

    def test_start_date_is_today_on_fresh_init(self):
        sch = self.make_scheduler()
        self.assertEqual(sch.rotation_start_date, self.today().isoformat())

    def test_all_active_on_fresh_init(self):
        sch = self.make_scheduler()
        for i in range(5):
            self.assertTrue(sch.get_user_for_day(i).is_active)

    # ------------------------------------------------------------------
    # Active-coverage extension
    # ------------------------------------------------------------------

    def test_rotation_covers_at_least_member_list_active_count(self):
        """After init, active entries in rotation >= len(member_list)."""
        sch = self.make_scheduler()
        active_ids = {u.id for u in sch.member_list}
        active_count = sum(1 for e in sch.rotation_users if e['id'] in active_ids)
        self.assertGreaterEqual(active_count, len(self.users))

    def test_extension_is_round_robin(self):
        """Extensions cycle through member_list in order."""
        sch = self.make_scheduler()  # [A,B,C,D,E], ext_idx=0
        # After fresh init: rotation = [A,B,C,D,E], next = A.
        sch._ensure_active_coverage(10)
        ids = [e['id'] for e in sch.rotation_users[:10]]
        expected = [111, 222, 333, 444, 555, 111, 222, 333, 444, 555]
        self.assertEqual(ids, expected)

    def test_na_entries_do_not_count_toward_minimum(self):
        """When a user is removed (N/A), the rotation extends to compensate."""
        sch = self.make_scheduler()
        # [A,B,C,D,E,A,B] after generate_schedule (7 active).
        sch._ensure_active_coverage(7)
        rotation_before = list(sch.rotation_users)

        # Remove Charlie — one slot becomes N/A, active count drops to 6.
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)

        active_ids = {u.id for u in sch.member_list}
        active_after = sum(1 for e in sch.rotation_users if e['id'] in active_ids)
        # load_state ensures len(member_list)=4 active — 6 active is enough.
        self.assertGreaterEqual(active_after, len(sch.member_list))

    def test_generate_schedule_ensures_7_active(self):
        """generate_schedule extends to at least 7 active entries."""
        sch = self.make_scheduler()
        # Remove Charlie so 1 slot becomes N/A in the first 7 entries.
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)

        sch.generate_schedule()

        active_ids = {u.id for u in sch.member_list}
        active_count = sum(1 for e in sch.rotation_users if e['id'] in active_ids)
        self.assertGreaterEqual(active_count, 7)

    def test_extension_adds_one_extra_when_na_present(self):
        """With [A,B,C,D,E,A,B] and C removed: rotation becomes [A,B,C,D,E,A,B,D]."""
        sch = self.make_scheduler()
        sch._ensure_active_coverage(7)  # → [A,B,C,D,E,A,B], next=C (idx 2)

        # Remove Charlie — ext_idx shifts to D (successor of C in [A,B,D,E]).
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)  # 6 active >= min(4) — no auto-extend yet

        sch._ensure_active_coverage(7)  # 6 < 7 → extend by 1

        ids = [e['id'] for e in sch.rotation_users]
        self.assertEqual(ids, [111, 222, 333, 444, 555, 111, 222, 444])
        # Charlie is still in the rotation at position 2.
        self.assertEqual(sch.rotation_users[2]['id'], 333)

    # ------------------------------------------------------------------
    # next_extension_id round-robin continuity
    # ------------------------------------------------------------------

    def test_readd_user_continues_correct_round_robin(self):
        """Re-adding C goes to the END of member_list; round-robin continues from there.

        Scenario:
          Init [A,B,C,D,E]: rotation=[A,B,C,D,E,A,B], last active=B → next=C
          Remove C → last active in rotation is B → next=D (C skipped, not in member_list)
          generate_schedule adds D: rotation=[A,B,C,D,E,A,B,D], last active=D → next=E
          Re-add C → appended at END: member_list=[A,B,D,E,C]
          Further extensions from E(idx3): E, C(idx4), A, B, D, E, C, …
        """
        sch = self.make_scheduler()
        sch._ensure_active_coverage(7)  # [A,B,C,D,E,A,B]
        sch.save_state()

        # Remove Charlie.
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)       # member_list=[A,B,D,E], last active=B → next=D
        sch._ensure_active_coverage(7)   # adds D → [A,B,C,D,E,A,B,D]
        sch.save_state()

        # Re-add Charlie — goes to the END.
        sch.load_state(self.users)       # member_list=[A,B,D,E,C]
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 444, 555, 333])

        # All 8 rotation entries are now active (C is back in member_list).
        active_ids = {u.id for u in sch.member_list}
        active_count = sum(1 for e in sch.rotation_users if e['id'] in active_ids)
        self.assertEqual(active_count, 8)

        # Last active in rotation is D (id=444), at index 2 in [A,B,D,E,C].
        # Next extensions: E(idx3), C(idx4), A(idx0), B(idx1).
        sch._ensure_active_coverage(12)
        ids = [e['id'] for e in sch.rotation_users]
        # [A,B,C,D,E,A,B,D] + [E,C,A,B] = 12 entries
        self.assertEqual(ids, [111, 222, 333, 444, 555, 111, 222, 444, 555, 333, 111, 222])


    # ------------------------------------------------------------------
    # Past-day cleanup
    # ------------------------------------------------------------------

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_cleanup_removes_past_days(self):
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()

        MockDate._today_val = datetime.date(2026, 7, 8)
        sch._cleanup_past_days()

        self.assertEqual(sch.rotation_start_date, '2026-07-08')
        # Alice (day 0) and Bob (day 1) are gone; Charlie is now first.
        self.assertEqual(sch.rotation_users[0]['id'], 333)

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_cleanup_then_extend_maintains_active_coverage(self):
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()  # [A,B,C,D,E], next=A (idx 0)

        MockDate._today_val = datetime.date(2026, 7, 8)
        sch._cleanup_past_days()    # [C,D,E], active=3 < 5
        sch._ensure_active_coverage()  # extend to 5 active: add A,B

        ids = [e['id'] for e in sch.rotation_users]
        self.assertEqual(ids, [333, 444, 555, 111, 222])

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_load_state_cleanup_and_extend(self):
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()

        MockDate._today_val = datetime.date(2026, 7, 8)
        sch.load_state(self.users)

        self.assertEqual(sch.rotation_start_date, '2026-07-08')
        active_ids = {u.id for u in sch.member_list}
        active_count = sum(1 for e in sch.rotation_users if e['id'] in active_ids)
        self.assertGreaterEqual(active_count, len(self.users))
        self.assertEqual(sch.rotation_users[0]['id'], 333)  # Charlie is now first

    # ------------------------------------------------------------------
    # member_list update rules
    # ------------------------------------------------------------------

    def test_member_list_initialized_from_users(self):
        sch = self.make_scheduler()
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 333, 444, 555])

    def test_remove_user_drops_from_member_list(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        self.assertNotIn(333, [u.id for u in sch.member_list])

    def test_add_user_appends_to_member_list(self):
        sch = self.make_scheduler()
        sch.load_state(self.users + [MockMember(666, 'Frank')])
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 333, 444, 555, 666])

    # ------------------------------------------------------------------
    # N/A behaviour
    # ------------------------------------------------------------------

    def test_removed_user_stays_in_rotation(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)

        rotation_ids = [e['id'] for e in sch.rotation_users]
        self.assertIn(333, rotation_ids)

        charlie = sch.get_user_for_day(2)
        self.assertEqual(charlie.id, 333)
        self.assertFalse(charlie.is_active)

    def test_removed_user_rotation_length_unchanged(self):
        sch = self.make_scheduler()
        original_len = len(sch.rotation_users)
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        self.assertEqual(len(sch.rotation_users), original_len)

    def test_added_user_not_in_current_rotation(self):
        sch = self.make_scheduler()
        sch.load_state(self.users + [MockMember(666, 'Frank')])
        rotation_ids = [e['id'] for e in sch.rotation_users]
        self.assertNotIn(666, rotation_ids)
        self.assertIn(666, [u.id for u in sch.member_list])

    def test_nick_preserved_for_active_users(self):
        sch = self.make_scheduler()
        alice = sch.get_user_for_day(0)
        self.assertEqual(alice.nick, 'Aly')

    def test_nick_none_for_na_users(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        charlie = sch.get_user_for_day(2)
        self.assertIsNone(charlie.nick)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_human_readable_serialization(self):
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()
        sch.save_state()

        with open(self.state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        ml = data.get('member_list', [])
        self.assertEqual(len(ml), len(self.users))

        rot = data.get('rotation', {})
        self.assertIn('start_date', rot)
        self.assertIn('next_extension_id', rot)
        self.assertIn('users', rot)
        self.assertEqual(rot['start_date'], '2026-07-06')

        # Round-trip.
        sch2 = self.make_scheduler()
        self.assertEqual([u.id for u in sch2.member_list], [u.id for u in self.users])
        self.assertEqual(sch2.rotation_start_date, sch.rotation_start_date)

    def test_migration_from_legacy_rotation_format(self):
        """Old rotation-block format (no next_extension_id) migrates cleanly."""
        legacy_data = {
            'member_list': [
                {'id': 111, 'name': 'Alice'}, {'id': 222, 'name': 'Bob'},
                {'id': 333, 'name': 'Charlie'}, {'id': 444, 'name': 'David'},
                {'id': 555, 'name': 'Eve'},
            ],
            'rotation': {
                'start_date': '2026-01-01',
                'users': [
                    {'id': 111, 'name': 'Alice'}, {'id': 222, 'name': 'Bob'},
                    {'id': 333, 'name': 'Charlie'}, {'id': 444, 'name': 'David'},
                    {'id': 555, 'name': 'Eve'},
                ]
            }
        }
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(legacy_data, f)

        sch = self.make_scheduler()

        self.assertEqual([e['id'] for e in sch.rotation_users[:5]], [111, 222, 333, 444, 555])
        # extension_index inferred from last user (Eve, idx 4 → next = 0 = Alice).
        self.assertEqual(sch.extension_index, 0)
        self.assertEqual(sch.rotation_start_date, datetime.date.today().isoformat())

        with open(self.state_file, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        self.assertIn('rotation', saved)
        self.assertIn('next_extension_id', saved['rotation'])

    # ------------------------------------------------------------------
    # generate_schedule display
    # ------------------------------------------------------------------

    def test_generate_schedule_shows_na_for_removed_user(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        output = sch.generate_schedule()
        self.assertIn('Charlie (N/A)', output)
        self.assertNotIn('Alice (N/A)', output)

    def test_generate_schedule_no_na_when_all_active(self):
        sch = self.make_scheduler()
        output = sch.generate_schedule()
        self.assertNotIn('(N/A)', output)

    def test_generate_schedule_has_seven_day_columns(self):
        sch = self.make_scheduler()
        output = sch.generate_schedule()
        # Count column separators in the data row.
        rows = [line for line in output.splitlines() if line.startswith('|')]
        self.assertTrue(all(r.count('|') == 8 for r in rows))  # 7 cols = 8 pipes


if __name__ == '__main__':
    unittest.main()