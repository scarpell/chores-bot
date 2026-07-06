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

  def date_offset(self, days):
    return (self.today() + datetime.timedelta(days=days)).isoformat()

  # ------------------------------------------------------------------
  # Basic rotation
  # ------------------------------------------------------------------

  def test_on_call(self):
    sch = self.make_scheduler()
    # Queue is [Alice, Bob, Charlie, David]; start_date = today → Alice is day 0
    self.assertEqual(sch.on_call.id, 111)

  def test_get_user_for_day(self):
    sch = self.make_scheduler()
    self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
    self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob
    self.assertEqual(sch.get_user_for_day(2).id, 333)  # Charlie
    self.assertEqual(sch.get_user_for_day(3).id, 444)  # David
    self.assertEqual(sch.get_user_for_day(4).id, 111)  # Alice wraps

  def test_start_date_drives_rotation(self):
    """Rotation is anchored to start_date, not a counter."""
    sch = self.make_scheduler()
    self.assertEqual(sch.start_date, self.today().isoformat())
    # Verify that a scheduler loaded from the same file agrees
    sch2 = self.make_scheduler()
    self.assertEqual(sch2.start_date, sch.start_date)
    self.assertEqual(sch2.get_user_for_day(0).id, 111)
  # ------------------------------------------------------------------
  # Skip tests
  # ------------------------------------------------------------------

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_skip_active_removes_user_from_rotation(self):
    """An active skip should push the user out of only their next appearance slot."""
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    # Queue is [Alice, Bob, Charlie, David] (length 4)
    # Alice is on day 0 (2026-07-06), Bob on day 1 (2026-07-07), Charlie on day 2 (2026-07-08), David on day 3 (2026-07-09)
    self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
    self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob
    
    # Skip Bob. He should be skipped for 4 days (until 2026-07-10).
    result = sch.skip(self.users[1])
    self.assertTrue(result)
    self.assertTrue(any(e['user_id'] == 222 for e in sch.skips))
    active_skip = next(e for e in sch.skips if e['user_id'] == 222)
    self.assertEqual(active_skip['start_date'], '2026-07-06')
    self.assertEqual(active_skip['expiry_date'], '2026-07-10')

    # Bob is skipped on Day 1 (2026-07-07 < 2026-07-10)
    self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
    self.assertEqual(sch.get_user_for_day(1).id, 333)  # Charlie (Bob is skipped)
    self.assertEqual(sch.get_user_for_day(2).id, 444)  # David
    self.assertEqual(sch.get_user_for_day(3).id, 111)  # Alice
    self.assertEqual(sch.get_user_for_day(4).id, 222)  # Bob is back (Day 4 is 2026-07-10 >= 2026-07-10)

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_skip_toggle_removes_active_skip(self):
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    was_skipped1 = sch.skip(self.users[1])
    self.assertTrue(was_skipped1)

    was_skipped2 = sch.skip(self.users[1])
    self.assertFalse(was_skipped2)
    self.assertFalse(any(e['user_id'] == 222 for e in sch.skips))

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_skip_expired_does_not_block_new_skip(self):
    """After a skip expires, !skip should add a new skip, not toggle one off."""
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()

    # Write an already-expired skip for Bob
    yesterday = (self.today() - datetime.timedelta(days=1)).isoformat()
    with open(sch._state_file, 'w') as f:
      json.dump({
        'start_date': sch.start_date,
        'skips': [{'user_id': 222, 'start_date': sch.start_date, 'expiry_date': yesterday}],
        'base_queue': [{'id': u.id, 'name': u.name} for u in self.users]
      }, f)

    # Simulate before_any_command
    sch.load_state(self.users)
    # The expired skip remains in history (it is not pruned)
    self.assertEqual(len(sch.skips), 1)

    # Calling skip should add a new active skip
    result = sch.skip(self.users[1])
    self.assertTrue(result, "Expected a NEW skip since the old one expired")
    
    # We should have 2 skips total in the list now
    self.assertEqual(len(sch.skips), 2)
    active = [e for e in sch.skips if e['user_id'] == 222 and datetime.date.fromisoformat(e['expiry_date']) > self.today()]
    self.assertEqual(len(active), 1)
    self.assertEqual(active[0]['expiry_date'], (self.today() + datetime.timedelta(days=4)).isoformat())

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_skip_persistence(self):
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    sch.skip(self.users[1])  # Skip Bob

    sch2 = self.make_scheduler()
    self.assertTrue(any(e['user_id'] == 222 for e in sch2.skips))
    self.assertEqual(sch2.get_user_for_day(1).id, 333)

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_skip_reset_and_display(self):
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    sch.skip(self.users[1])
    self.assertEqual(len(sch.skips), 1)

    table = sch.generate_schedule()
    self.assertIn("Skipped members:", table)
    self.assertIn("Bob", table)
    self.assertIn("Run `!skip reset` to reset all skipped entries.", table)

    sch.reset_skips()
    self.assertEqual(len(sch.skips), 0)

    table_after = sch.generate_schedule()
    self.assertNotIn("Skipped members:", table_after)

  # ------------------------------------------------------------------
  # User membership changes
  # ------------------------------------------------------------------

  def test_load_state_add_and_remove_users(self):
    sch = self.make_scheduler()
    self.assertEqual([u.id for u in sch.base_queue], [111, 222, 333, 444])

    new_users = [
      MockMember(111, 'Alice', 'Aly'),
      MockMember(333, 'Charlie', 'Chaz'),
      MockMember(555, 'Eve')
    ]

    # Add a skip involving Bob (222), who will be removed
    sch.skip(self.users[1])
    self.assertEqual(len(sch.skips), 1)

    sch.load_state(new_users)

    self.assertEqual([u.id for u in sch.base_queue], [111, 333, 555])
    # Bob's skip entries should be cleaned up
    self.assertFalse(any(e['user_id'] == 222 for e in sch.skips))
    self.assertTrue(sch._state_file.exists())

  # ------------------------------------------------------------------
  # Migration
  # ------------------------------------------------------------------

  def test_legacy_skip_migration(self):
    """A state file with legacy skips (missing start_date) is migrated (skips cleared)."""
    sch = self.make_scheduler()
    legacy_data = {
      'day_index': 10,
      'skips': [11, 14],  # old integer format
      'base_queue': [111, 222, 333, 444]
    }
    with open(sch._state_file, 'w', encoding='utf-8') as f:
      json.dump(legacy_data, f)

    sch2 = self.make_scheduler()
    self.assertEqual(sch2.skips, [])
    self.assertEqual(sch2.start_date, self.today().isoformat())

  def test_skip_date_to_expiry_date_migration(self):
    """A state file with skips using the 'date' key is migrated to 'expiry_date'."""
    sch = self.make_scheduler()
    legacy_data = {
      'start_date': sch.start_date,
      'skips': [{'user_id': 111, 'date': '2026-07-08', 'name': 'Alice'}],
      'base_queue': [{'id': u.id, 'name': u.name} for u in self.users]
    }
    with open(sch._state_file, 'w', encoding='utf-8') as f:
      json.dump(legacy_data, f)

    sch2 = self.make_scheduler()
    # It should have migrated 'date' to 'expiry_date' and set default 'start_date'
    self.assertEqual(len(sch2.skips), 1)
    self.assertEqual(sch2.skips[0]['user_id'], 111)
    self.assertEqual(sch2.skips[0]['expiry_date'], '2026-07-08')
    self.assertEqual(sch2.skips[0]['start_date'], sch.start_date)

  # ------------------------------------------------------------------
  # Serialization
  # ------------------------------------------------------------------

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_human_readable_serialization(self):
    """Verify that usernames are serialized with user IDs on disk."""
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    
    # Perform a skip
    sch.skip(self.users[1])  # Bob (222)
    
    # Force saving state
    sch.save_state()
    
    # Read raw JSON from disk
    self.assertTrue(sch._state_file.exists())
    with open(sch._state_file, 'r', encoding='utf-8') as f:
      data = json.load(f)
      
    # Check base_queue
    base_queue_data = data.get('base_queue', [])
    self.assertEqual(len(base_queue_data), len(self.users))
    for entry in base_queue_data:
      self.assertIn('id', entry)
      self.assertIn('name', entry)
      orig_user = next(u for u in self.users if u.id == entry['id'])
      self.assertEqual(entry['name'], orig_user.name)
      
    # Check skips
    skips_data = data.get('skips', [])
    self.assertEqual(len(skips_data), 1)
    entry = skips_data[0]
    self.assertIn('start_date', entry)
    self.assertIn('expiry_date', entry)
    self.assertIn('user_id', entry)
    self.assertIn('name', entry)
    self.assertEqual(entry['user_id'], 222)
    self.assertEqual(entry['name'], 'Bob')

    # Now verify we can load it back correctly
    sch2 = self.make_scheduler()
    self.assertEqual(len(sch2.skips), 1)
    self.assertEqual(sch2.skips[0]['user_id'], 222)

  # ------------------------------------------------------------------
  # Multiple Skips
  # ------------------------------------------------------------------

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_multiple_skips_work_as_expected(self):
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    
    # Skip both Alice and Bob today (Monday 7/06).
    # Since len(base_queue) is 4, both will be skipped for 4 days, expiring on 2026-07-10.
    sch.skip(self.users[0])  # Alice
    sch.skip(self.users[1])  # Bob
    
    # Schedule for the next few days:
    # Day 0 (Monday 7/06): Alice and Bob skipped -> Charlie (333)
    # Day 1 (Tuesday 7/07): David (444)
    # Day 2 (Wednesday 7/08): Alice and Bob skipped -> Charlie (333)
    # Day 3 (Thursday 7/09): David (444)
    # Day 4 (Friday 7/10): Skips expired -> Alice (111)
    # Day 5 (Saturday 7/11): Bob (222)
    self.assertEqual(sch.get_user_for_day(0).id, 333) # Charlie
    self.assertEqual(sch.get_user_for_day(1).id, 444) # David
    self.assertEqual(sch.get_user_for_day(2).id, 333) # Charlie
    self.assertEqual(sch.get_user_for_day(3).id, 444) # David
    self.assertEqual(sch.get_user_for_day(4).id, 111) # Alice
    self.assertEqual(sch.get_user_for_day(5).id, 222) # Bob

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_multiple_skips_5_users(self):
    MockDate._today_val = datetime.date(2026, 7, 6)
    # Queue is 5 users: Alice (111), Bob (222), Charlie (333), David (444), Eve (555)
    users = [
      self.users[0], # Alice
      self.users[1], # Bob
      self.users[2], # Charlie
      self.users[3], # David
      MockMember(555, 'Eve')
    ]
    
    # Test skipping in order of appearance (Bob, Charlie, David)
    sch_forward = self.make_scheduler(users)
    sch_forward.skip(users[1]) # Bob
    sch_forward.skip(users[2]) # Charlie
    sch_forward.skip(users[3]) # David

    self.assertEqual(sch_forward.get_user_for_day(0).id, 111) # Alice
    self.assertEqual(sch_forward.get_user_for_day(1).id, 555) # Eve
    self.assertEqual(sch_forward.get_user_for_day(2).id, 111) # Alice
    self.assertEqual(sch_forward.get_user_for_day(3).id, 555) # Eve
    self.assertEqual(sch_forward.get_user_for_day(4).id, 111) # Alice
    self.assertEqual(sch_forward.get_user_for_day(5).id, 222) # Bob
    self.assertEqual(sch_forward.get_user_for_day(6).id, 333) # Charlie

    # Clean the state file so the reverse test starts fresh
    if self.state_file.exists():
      os.remove(self.state_file)

    # Test skipping in reverse order (David, Charlie, Bob)
    sch_reverse = self.make_scheduler(users)
    sch_reverse.skip(users[3]) # David
    sch_reverse.skip(users[2]) # Charlie
    sch_reverse.skip(users[1]) # Bob

    self.assertEqual(sch_reverse.get_user_for_day(0).id, 111) # Alice
    self.assertEqual(sch_reverse.get_user_for_day(1).id, 555) # Eve
    self.assertEqual(sch_reverse.get_user_for_day(2).id, 111) # Alice
    self.assertEqual(sch_reverse.get_user_for_day(3).id, 555) # Eve
    self.assertEqual(sch_reverse.get_user_for_day(4).id, 111) # Alice
    self.assertEqual(sch_reverse.get_user_for_day(5).id, 222) # Bob
    self.assertEqual(sch_reverse.get_user_for_day(6).id, 333) # Charlie

  # ------------------------------------------------------------------
  # Schedule Stability (No Recalculation Shifts)
  # ------------------------------------------------------------------

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_schedule_stability_after_expiry(self):
    """The schedule must remain stable and not retroactively shift when skips expire."""
    # 1. Today is Monday 7/06. Alice (111) is skipped for 4 days (until Friday 7/10).
    MockDate._today_val = datetime.date(2026, 7, 6)
    sch = self.make_scheduler()
    sch.skip(self.users[0]) # Skip Alice

    # Confirm rotation under active skip:
    # Monday 7/06 (offset 0): Bob (222)
    # Tuesday 7/07 (offset 1): Charlie (333)
    # Wednesday 7/08 (offset 2): David (444)
    # Thursday 7/09 (offset 3): Bob (222)
    # Friday 7/10 (offset 4): Charlie (333)
    self.assertEqual(sch.get_user_for_day(0).id, 222) # Bob
    self.assertEqual(sch.get_user_for_day(1).id, 333) # Charlie
    self.assertEqual(sch.get_user_for_day(2).id, 444) # David
    self.assertEqual(sch.get_user_for_day(3).id, 222) # Bob
    self.assertEqual(sch.get_user_for_day(4).id, 333) # Charlie

    # 2. Advance time to Friday 7/10. Alice's skip expires.
    MockDate._today_val = datetime.date(2026, 7, 10)
    sch.load_state(self.users)

    # The skip must not display in the schedule anymore.
    table = sch.generate_schedule()
    self.assertNotIn("Alice", table)
    self.assertNotIn("Skipped members:", table)

    # Friday 7/10 is offset 0 relative to today (Friday 7/10).
    # Since the skip history is preserved, Friday's assignment must STABLY remain Charlie (333),
    # rather than shifting back to Alice (111).
    self.assertEqual(sch.get_user_for_day(0).id, 333) # Charlie


if __name__ == '__main__':
  unittest.main()