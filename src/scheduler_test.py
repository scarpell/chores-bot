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
        self.state_file = util.get_data_folder() / 'schedule.json'
        if self.state_file.exists():
            os.remove(self.state_file)

    def tearDown(self):
        if self.state_file.exists():
            os.remove(self.state_file)

    def make_scheduler(self, users=None):
        sch = scheduler.Scheduler()
        sch.load_state(users if users is not None else self.users)
        return sch

    def visible_ids(self, sch):
        return [e['id'] for _, e in sch.printable_schedule()]

    # ------------------------------------------------------------------
    # load_state / init
    # ------------------------------------------------------------------

    def test_empty_users_raises(self):
        sch = scheduler.Scheduler()
        with self.assertRaises(ValueError):
            sch.load_state([])

    def test_member_list_initialized_from_users(self):
        sch = self.make_scheduler()
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 333, 444, 555])

    def test_start_date_is_today_on_fresh_init(self):
        sch = self.make_scheduler()
        self.assertEqual(sch.rotation_start_date, datetime.date.today().isoformat())

    # ------------------------------------------------------------------
    # printable_schedule — the core filter
    # ------------------------------------------------------------------

    def test_printable_schedule_excludes_removed(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        ids = [e['id'] for _, e in sch.printable_schedule()]
        self.assertNotIn(333, ids)

    def test_printable_schedule_all_visible_on_fresh_init(self):
        sch = self.make_scheduler()
        for _, entry in sch.printable_schedule():
            self.assertFalse(entry.get('removed', False))

    def test_printable_schedule_has_dates(self):
        sch = self.make_scheduler()
        sched = sch.printable_schedule()
        today = datetime.date.today()
        self.assertEqual(sched[0][0], today)              # first date is today
        self.assertEqual(sched[1][0], today + datetime.timedelta(days=1))

    # ------------------------------------------------------------------
    # on_call
    # ------------------------------------------------------------------

    def test_on_call(self):
        sch = self.make_scheduler()
        user = sch.on_call
        self.assertIsNotNone(user)
        self.assertEqual(user.id, 111)   # Alice is day 0
        self.assertFalse(user.removed)

    def test_on_call_returns_none_for_removed_slot(self):
        """on_call is None when today's slot belongs to a removed user."""
        sch = self.make_scheduler()      # day 0 = Alice
        no_alice = [u for u in self.users if u.id != 111]
        sch.load_state(no_alice)         # Alice removed → day 0 gap
        self.assertIsNone(sch.on_call)

    def test_on_call_nick(self):
        sch = self.make_scheduler()
        self.assertEqual(sch.on_call.nick, 'Aly')

    # ------------------------------------------------------------------
    # _extend_rotation_by_one
    # ------------------------------------------------------------------

    def test_extension_is_round_robin(self):
        sch = self.make_scheduler()   # [A,B,C,D,E], ext_idx=0
        for _ in range(5):
            sch._extend_rotation_by_one()
        ids = [e['id'] for e in sch.rotation_users[:10]]
        self.assertEqual(ids, [111, 222, 333, 444, 555, 111, 222, 333, 444, 555])

    def test_extension_index_wraps(self):
        sch = self.make_scheduler()
        # Fresh init: 5 entries, extension_index=0
        self.assertEqual(sch.extension_index, 0)
        sch._extend_rotation_by_one()
        self.assertEqual(sch.rotation_users[-1]['id'], 111)  # Alice again
        self.assertEqual(sch.extension_index, 1)

    # ------------------------------------------------------------------
    # Remove user → removed:true in rotation
    # ------------------------------------------------------------------

    def test_remove_user_marks_rotation_entries_removed(self):
        sch = self.make_scheduler()
        for _ in range(5):
            sch._extend_rotation_by_one()   # add A,B,C,D,E again

        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)

        charlie_entries = [e for e in sch.rotation_users if e['id'] == 333]
        self.assertTrue(all(e.get('removed') for e in charlie_entries))

    def test_remove_user_drops_from_member_list(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        self.assertNotIn(333, [u.id for u in sch.member_list])

    def test_removed_entry_flagged_in_rotation_users(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        # rotation_users[2] is Charlie (day 2)
        charlie_slot = sch.rotation_users[2]
        self.assertEqual(charlie_slot['id'], 333)
        self.assertTrue(charlie_slot.get('removed'))

    def test_extension_compensates_for_removed(self):
        """remove C from [A,B,C,D,E]: 4 visible → load_state ensures 4 → no change.
        Then extend to 7 visible: adds A,B,D (skipping C)."""
        sch = self.make_scheduler()      # [A,B,C,D,E], 5 visible
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)       # marks C removed, 4 visible == len(ml)=4

        while len(sch.printable_schedule()) < 7:
            sch._extend_rotation_by_one()

        all_ids = [e['id'] for e in sch.rotation_users]
        visible = self.visible_ids(sch)
        self.assertEqual(all_ids, [111, 222, 333, 444, 555, 111, 222, 444])
        self.assertNotIn(333, visible)
        self.assertEqual(len(visible), 7)

    # ------------------------------------------------------------------
    # Re-add user — old entries stay removed, new entries appear via extension
    # ------------------------------------------------------------------

    def test_readd_user_appended_to_member_list_end(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        sch.save_state()

        sch.load_state(self.users)   # Charlie re-added
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 444, 555, 333])

    def test_readd_user_removed_flag_is_cleared(self):
        """Re-adding a user clears removed=True from their rotation entries."""
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)    # Charlie marked removed
        sch.save_state()

        sch.load_state(self.users)    # Charlie re-added

        charlie_entries = [e for e in sch.rotation_users if e['id'] == 333]
        self.assertTrue(charlie_entries)
        self.assertFalse(any(e.get('removed') for e in charlie_entries),
                         'removed flag must be cleared when user regains the role')

    def test_readd_user_no_duplicate_slot(self):
        """Re-adding restores existing entries; coverage loop should not add extras."""
        sch = self.make_scheduler()          # [A,B,C,D,E]
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)            # marks C removed
        while len(sch.printable_schedule()) < 7:
            sch._extend_rotation_by_one()    # extends to 7 visible
        sch.save_state()

        count_before = sum(1 for e in sch.rotation_users if e['id'] == 333)
        sch.load_state(self.users)           # re-add Charlie (restore)
        count_after = sum(1 for e in sch.rotation_users if e['id'] == 333)

        # Restore should not add new entries — same count, flag cleared.
        self.assertEqual(count_before, count_after)
        charlie_entries = [e for e in sch.rotation_users if e['id'] == 333]
        self.assertFalse(any(e.get('removed') for e in charlie_entries))

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
        self.assertEqual(sch.rotation_users[0]['id'], 333)   # Charlie now first

    @mock.patch('scheduler.datetime.date', new=MockDate)
    def test_load_state_cleanup_and_extend(self):
        MockDate._today_val = datetime.date(2026, 7, 6)
        sch = self.make_scheduler()

        MockDate._today_val = datetime.date(2026, 7, 8)
        sch.load_state(self.users)

        self.assertEqual(sch.rotation_start_date, '2026-07-08')
        self.assertEqual(sch.rotation_users[0]['id'], 333)
        self.assertGreaterEqual(len(sch.printable_schedule()), len(self.users))

    # ------------------------------------------------------------------
    # member_list update rules
    # ------------------------------------------------------------------

    def test_add_user_appended_to_member_list_end(self):
        sch = self.make_scheduler()
        sch.load_state(self.users + [MockMember(666, 'Frank')])
        self.assertEqual([u.id for u in sch.member_list], [111, 222, 333, 444, 555, 666])

    def test_add_user_appears_in_rotation_via_coverage(self):
        sch = self.make_scheduler()
        sch.load_state(self.users + [MockMember(666, 'Frank')])
        ids = self.visible_ids(sch)
        self.assertIn(666, ids)
        self.assertEqual(ids[:5], [111, 222, 333, 444, 555])
        self.assertEqual(ids[5], 666)

    # ------------------------------------------------------------------
    # generate_schedule
    # ------------------------------------------------------------------

    def test_generate_schedule_has_seven_columns(self):
        sch = self.make_scheduler()
        output = sch.generate_schedule()
        rows = [line for line in output.splitlines() if line.startswith('|')]
        self.assertTrue(all(r.count('|') == 8 for r in rows))

    def test_generate_schedule_hides_removed_user(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        output = sch.generate_schedule()
        self.assertNotIn('Charlie', output)
        self.assertNotIn('Chaz', output)

    def test_generate_schedule_extends_if_too_short(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)   # 4 visible
        sch.generate_schedule()
        self.assertGreaterEqual(len(sch.printable_schedule()), 7)

    def test_generate_schedule_still_seven_columns_with_removed(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        output = sch.generate_schedule()
        rows = [line for line in output.splitlines() if line.startswith('|')]
        self.assertTrue(all(r.count('|') == 8 for r in rows))

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

        self.assertEqual(len(data['member_list']), len(self.users))
        rot = data['rotation']
        self.assertEqual(rot['start_date'], '2026-07-06')
        self.assertIn('users', rot)

    def test_removed_flag_persisted_on_disk(self):
        sch = self.make_scheduler()
        no_charlie = [u for u in self.users if u.id != 333]
        sch.load_state(no_charlie)
        sch.save_state()

        with open(self.state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        charlie_on_disk = [e for e in data['rotation']['users'] if e['id'] == 333]
        self.assertTrue(charlie_on_disk)
        self.assertTrue(all(e.get('removed') for e in charlie_on_disk))

    # ------------------------------------------------------------------
    # Skip
    # ------------------------------------------------------------------

    def test_skip_sets_flag_on_first_slot(self):
        """skip_user sets skip=True on the first non-removed entry for that user."""
        sch = self.make_scheduler()
        result = sch.skip_user(333)   # Charlie is at index 2
        self.assertTrue(result)       # returns True (was skipped)
        self.assertTrue(sch.rotation_users[2].get('skip'))

    def test_skip_toggle_clears_flag(self):
        """Calling skip_user twice clears the flag."""
        sch = self.make_scheduler()
        sch.skip_user(333)
        result = sch.skip_user(333)
        self.assertFalse(result)      # returns False (was un-skipped)
        self.assertFalse(sch.rotation_users[2].get('skip'))

    def test_skip_only_affects_first_slot(self):
        """Only the first upcoming slot is skipped; later slots are untouched."""
        sch = self.make_scheduler()
        for _ in range(5):
            sch._extend_rotation_by_one()   # add A,B,C,D,E again
        sch.skip_user(333)
        charlie_entries = [e for e in sch.rotation_users if e['id'] == 333]
        skipped = [e for e in charlie_entries if e.get('skip')]
        not_skipped = [e for e in charlie_entries if not e.get('skip')]
        self.assertEqual(len(skipped), 1)
        self.assertGreaterEqual(len(not_skipped), 1)

    def test_skip_raises_if_no_slot(self):
        sch = self.make_scheduler()
        sch.rotation_users = []
        with self.assertRaises(ValueError):
            sch.skip_user(333)

    def test_skip_excluded_from_printable_schedule(self):
        """A skipped entry does not appear in the printable schedule."""
        sch = self.make_scheduler()
        sch.skip_user(111)   # Alice is at index 0 (today)
        ids = [e['id'] for _, e in sch.printable_schedule()]
        # Alice should not be first
        self.assertNotEqual(ids[0], 111)

    def test_skip_today_means_no_one_on_call(self):
        """If today's slot is skipped, on_call returns None."""
        sch = self.make_scheduler()
        sch.skip_user(111)   # Alice has today (index 0)
        self.assertIsNone(sch.on_call)

    def test_skip_flag_persisted_on_disk(self):
        sch = self.make_scheduler()
        sch.skip_user(333)
        with open(self.state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        charlie_slots = [e for e in data['rotation']['users'] if e['id'] == 333]
        self.assertTrue(any(e.get('skip') for e in charlie_slots))

    def test_skip_flag_survives_load_state(self):
        """skip=True is preserved when the state file is reloaded."""
        sch = self.make_scheduler()
        sch.skip_user(333)

        sch2 = self.make_scheduler()   # re-loads from disk
        charlie_entries = [e for e in sch2.rotation_users if e['id'] == 333]
        self.assertTrue(any(e.get('skip') for e in charlie_entries))

    def test_generate_schedule_skips_skipped_slot(self):
        """generate_schedule does not show a skipped user in their skipped slot."""
        sch = self.make_scheduler()
        sch.skip_user(111)   # skip Alice's slot today
        output = sch.generate_schedule()
        lines = [l for l in output.splitlines() if l.startswith('|')]
        first_col = lines[-1].split('|')[1].strip()
        self.assertNotIn('Alice', first_col)
        self.assertNotIn('Aly', first_col)

    def test_generate_schedule_skipped_footer(self):
        """A 'Skipped: ...' footer appears when a user is skipped."""
        sch = self.make_scheduler()
        sch.skip_user(111)   # Alice skipped
        output = sch.generate_schedule()
        self.assertIn('Skipped:', output)
        self.assertIn('Aly', output.split('Skipped:')[1])

    def test_generate_schedule_multiple_skipped_footer(self):
        """Multiple skipped users all appear in the footer."""
        sch = self.make_scheduler()
        sch.skip_user(111)   # Alice
        sch.skip_user(333)   # Charlie
        output = sch.generate_schedule()
        footer = output.split('Skipped:')[1]
        self.assertIn('Aly', footer)
        self.assertIn('Chaz', footer)

    def test_generate_schedule_no_footer_when_none_skipped(self):
        """No footer when no one is skipped."""
        sch = self.make_scheduler()
        output = sch.generate_schedule()
        self.assertNotIn('Skipped:', output)


if __name__ == '__main__':
    unittest.main()