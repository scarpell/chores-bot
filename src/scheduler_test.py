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
  # Swaps
  # ------------------------------------------------------------------

  def test_swap_happy(self):
    sch = self.make_scheduler()
    # Alice is on day 0 (today), Charlie is on day 2
    sch.swap(self.users[0], self.users[2])

    self.assertEqual(sch.get_user_for_day(0).id, 333)  # Charlie on Alice's day
    self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob unchanged
    self.assertEqual(sch.get_user_for_day(2).id, 111)  # Alice on Charlie's day

    # Verify swap entries are stored as dates
    dates_in_swaps = {e['date'] for e in sch.swaps}
    self.assertIn(self.date_offset(0), dates_in_swaps)
    self.assertIn(self.date_offset(2), dates_in_swaps)

    # Verify persistence
    sch2 = self.make_scheduler()
    self.assertEqual(sch2.get_user_for_day(0).id, 333)
    self.assertEqual(sch2.get_user_for_day(2).id, 111)

  def test_swap_same_person(self):
    sch = self.make_scheduler()
    with self.assertRaises(ValueError):
      sch.swap(self.users[0], self.users[0])

  def test_swap_not_in_schedule(self):
    sch = self.make_scheduler()
    fake_user = MockMember(999, 'Fake')
    with self.assertRaises(ValueError):
      sch.swap(self.users[0], fake_user)

  def test_swap_back_removes_entries(self):
    """Swapping the same two people back cancels the swap entirely."""
    sch = self.make_scheduler()
    sch.swap(self.users[0], self.users[1])
    self.assertEqual(len(sch.swaps), 2)

    sch.swap(self.users[0], self.users[1])
    self.assertEqual(sch.swaps, [])

  def test_swap_respects_active_skips_in_redundancy_check(self):
    """When a skip shifts the rotation, the swap redundancy check must use the
    skip-aware effective owner, not the raw queue owner.

    Queue = [Alice, Bob, Charlie, David]; Alice is skipped.
    Effective rotation for today onwards: [Bob, Charlie, David].
      Day 0 → Bob   (Alice's raw slot, but Bob's effective slot)
      Day 1 → Charlie
      Day 2 → David

    Bob swaps with Charlie (their effective days 0 and 1).  After the swap:
      Day 0 → Charlie  (swap entry)
      Day 1 → Bob      (swap entry)

    Previously the raw-queue check saw raw_user(day1) = Bob and incorrectly
    removed the day1 entry, leaving Bob stuck on Charlie's day.
    """
    sch = self.make_scheduler()
    sch.skip(self.users[0])  # Skip Alice; available = [Bob, Charlie, David]

    # Bob is now on day 0, Charlie on day 1
    self.assertEqual(sch.get_user_for_day(0).id, 222)
    self.assertEqual(sch.get_user_for_day(1).id, 333)

    sch.swap(self.users[1], self.users[2])  # Bob ↔ Charlie

    # Both swap entries must survive — neither is redundant
    self.assertEqual(len(sch.swaps), 2)
    self.assertEqual(sch.get_user_for_day(0).id, 333)  # Charlie on day 0
    self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob on day 1

  def test_load_state_all_users_removed(self):
    """load_state with an empty user list must not raise AttributeError."""
    sch = self.make_scheduler()
    sch.load_state([])  # everyone left the server
    self.assertEqual(sch.base_queue, [])

  def test_load_state_corrupt_file_falls_back_to_fresh(self):
    """A corrupt state file must not leave the scheduler in an unusable state."""
    sch = self.make_scheduler()
    with open(sch._state_file, 'w') as f:
      f.write('NOT VALID JSON {{{')

    sch.load_state(self.users)
    # Should have fallen back to fresh defaults
    self.assertEqual([u.id for u in sch.base_queue], [111, 222, 333, 444])
    self.assertEqual(sch.swaps, [])
    self.assertEqual(sch.skips, [])
    self.assertEqual(sch.start_date, self.today().isoformat())

  def test_load_state_prunes_expired_swaps(self):
    """A swap whose date has passed is pruned by load_state."""
    sch = self.make_scheduler()
    # Inject a swap for yesterday (already expired)
    yesterday = (self.today() - datetime.timedelta(days=1)).isoformat()
    with open(sch._state_file, 'w') as f:
      json.dump({
        'start_date': sch.start_date,
        'swaps': [{'date': yesterday, 'user_id': 222}],
        'skips': [],
        'base_queue': [u.id for u in self.users]
      }, f)

    sch.load_state(self.users)
    self.assertEqual(sch.swaps, [], "Expired swap should have been pruned by load_state()")

  def test_swap_for_today_not_pruned(self):
    """A swap dated today is still active and must NOT be pruned."""
    sch = self.make_scheduler()
    sch.swap(self.users[0], self.users[1])

    # Re-load (as before_any_command would) and verify swap survives
    sch.load_state(self.users)
    self.assertTrue(
      any(e['date'] == self.date_offset(0) for e in sch.swaps),
      "Today's swap should still be present after load_state()")
    self.assertEqual(sch.get_user_for_day(0).id, 222)

  def test_generate_schedule_shows_swapped(self):
    sch = self.make_scheduler()
    sch.swap(self.users[0], self.users[1])
    table = sch.generate_schedule()
    self.assertIn("Dishes Schedule", table)
    self.assertIn("(swapped)", table)
    self.assertIn("Run `!swap reset` to reset all swapped entries.", table)

  # ------------------------------------------------------------------
  # Swap reset
  # ------------------------------------------------------------------

  def test_swap_reset_and_display(self):
    sch = self.make_scheduler()
    sch.swap(self.users[0], self.users[1])
    self.assertEqual(len(sch.swaps), 2)

    table = sch.generate_schedule()
    self.assertIn("Run `!swap reset` to reset all swapped entries.", table)

    sch.reset_swaps()
    self.assertEqual(sch.swaps, [])

    table_after = sch.generate_schedule()
    self.assertNotIn("Run `!swap reset` to reset all swapped entries.", table_after)

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

    # Add a swap involving Bob (222), who will be removed
    sch.swap(self.users[0], self.users[1])
    self.assertEqual(len(sch.swaps), 2)

    sch.load_state(new_users)

    self.assertEqual([u.id for u in sch.base_queue], [111, 333, 555])
    # Bob's swap entries should be cleaned up
    self.assertFalse(any(e['user_id'] == 222 for e in sch.swaps))
    self.assertTrue(sch._state_file.exists())

  def test_load_state_remove_user_clears_related_swaps(self):
    sch = self.make_scheduler()
    sch.swap(self.users[1], self.users[2])  # Bob ↔ Charlie

    bob_date = next(e['date'] for e in sch.swaps if e['user_id'] == 333)
    charlie_date = next(e['date'] for e in sch.swaps if e['user_id'] == 222)

    new_users = [
      MockMember(111, 'Alice', 'Aly'),
      MockMember(333, 'Charlie', 'Chaz'),
      MockMember(444, 'David')
    ]
    sch.load_state(new_users)

    self.assertFalse(any(e['user_id'] == 222 for e in sch.swaps))
    self.assertFalse(any(e['user_id'] == 333 and e['date'] == charlie_date
                         for e in sch.swaps))

  # ------------------------------------------------------------------
  # Skip tests
  # ------------------------------------------------------------------

  def test_skip_active_removes_user_from_rotation(self):
    """An active skip should push the user out of only their next appearance slot."""
    sch = self.make_scheduler()
    self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
    self.assertEqual(sch.get_user_for_day(1).id, 222)  # Bob
    self.assertEqual(sch.get_user_for_day(2).id, 333)  # Charlie

    result = sch.skip(self.users[1])  # Skip Bob (next appearance is Day 1)
    self.assertTrue(result)
    self.assertTrue(any(e['user_id'] == 222 for e in sch.skips))

    # Bob is skipped only on Day 1:
    self.assertEqual(sch.get_user_for_day(0).id, 111)  # Alice
    self.assertEqual(sch.get_user_for_day(1).id, 333)  # Charlie (Bob is skipped)
    self.assertEqual(sch.get_user_for_day(2).id, 444)  # David
    self.assertEqual(sch.get_user_for_day(3).id, 111)  # Alice wraps
    self.assertEqual(sch.get_user_for_day(4).id, 222)  # Bob is back!

  def test_skip_toggle_removes_active_skip(self):
    sch = self.make_scheduler()
    was_skipped1 = sch.skip(self.users[1])
    self.assertTrue(was_skipped1)

    was_skipped2 = sch.skip(self.users[1])
    self.assertFalse(was_skipped2)
    self.assertFalse(any(e['user_id'] == 222 for e in sch.skips))

  def test_skip_expired_does_not_block_new_skip(self):
    """After a skip expires, !skip should add a new skip, not toggle one off."""
    sch = self.make_scheduler()

    # Write an already-expired skip for Bob
    yesterday = (self.today() - datetime.timedelta(days=1)).isoformat()
    with open(sch._state_file, 'w') as f:
      json.dump({
        'start_date': sch.start_date,
        'swaps': [],
        'skips': [{'user_id': 222, 'date': yesterday}],
        'base_queue': [u.id for u in self.users]
      }, f)

    # Simulate before_any_command
    sch.load_state(self.users)
    self.assertEqual(sch.skips, [], "Expired skip should be pruned by load_state()")

    result = sch.skip(self.users[1])
    self.assertTrue(result, "Expected a NEW skip since the old one expired")
    active = [e for e in sch.skips if e['user_id'] == 222]
    self.assertEqual(len(active), 1)
    self.assertEqual(active[0]['date'], self.date_offset(1))

  def test_skip_persistence(self):
    sch = self.make_scheduler()
    sch.skip(self.users[1])  # Skip Bob

    sch2 = self.make_scheduler()
    self.assertTrue(any(e['user_id'] == 222 for e in sch2.skips))
    self.assertEqual(sch2.get_user_for_day(1).id, 333)

  def test_skip_reset_and_display(self):
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

  def test_skip_fails_for_swapped_user(self):
    """Attempting to skip a swapped user should raise a ValueError."""
    sch = self.make_scheduler()
    sch.swap(self.users[0], self.users[1])
    with self.assertRaises(ValueError):
      sch.skip(self.users[0])

  def test_swap_fails_for_skipped_user(self):
    """Attempting to swap a skipped user should raise a ValueError."""
    sch = self.make_scheduler()
    sch.skip(self.users[0])
    with self.assertRaises(ValueError):
      sch.swap(self.users[0], self.users[1])

  def test_swap_fails_for_already_swapped_user(self):
    """Attempting to swap a user who is already swapped (with someone else) should raise a ValueError."""
    sch = self.make_scheduler()
    sch.swap(self.users[0], self.users[1])  # Alice and Bob
    # Now try to swap Bob and Charlie - should fail because Bob is already swapped
    with self.assertRaises(ValueError):
      sch.swap(self.users[1], self.users[2])

  # ------------------------------------------------------------------
  # Migration
  # ------------------------------------------------------------------

  def test_legacy_skip_migration(self):
    """A state file with legacy integer skips is migrated (skips cleared)."""
    sch = self.make_scheduler()
    legacy_data = {
      'day_index': 10,
      'swaps': {},
      'skips': [11, 14],  # old integer format
      'base_queue': [111, 222, 333, 444]
    }
    with open(sch._state_file, 'w', encoding='utf-8') as f:
      json.dump(legacy_data, f)

    sch2 = self.make_scheduler()
    self.assertEqual(sch2.skips, [])

  def test_legacy_swap_migration(self):
    """On a legacy state file (missing start_date), only queue order is preserved.
    Swaps and skips are discarded and the rotation restarts from today."""
    sch = self.make_scheduler()
    legacy_data = {
      'day_index': 5,
      'swaps': {'7': 111, '9': 333},  # old integer-keyed swaps
      'skips': [6, 8],                 # old integer skips
      'base_queue': [333, 111, 444, 222]  # non-default order we want preserved
    }
    with open(sch._state_file, 'w', encoding='utf-8') as f:
      json.dump(legacy_data, f)

    sch2 = self.make_scheduler()

    # Queue order is preserved from the file
    self.assertEqual([u.id for u in sch2.base_queue], [333, 111, 444, 222])
    # Swaps and skips are discarded
    self.assertEqual(sch2.swaps, [])
    self.assertEqual(sch2.skips, [])
    # Rotation restarts from today
    self.assertEqual(sch2.start_date, self.today().isoformat())

  def test_human_readable_serialization(self):
    """Verify that usernames are serialized with user IDs on disk."""
    sch = self.make_scheduler()
    
    # Perform a swap and a skip
    sch.swap(self.users[0], self.users[2])  # Alice (111) and Charlie (333)
    sch.skip(self.users[1])  # Bob (222)
    
    # Force saving state if not already saved
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
      # Find original user to match the name
      orig_user = next(u for u in self.users if u.id == entry['id'])
      self.assertEqual(entry['name'], orig_user.name)
      
    # Check swaps
    swaps_data = data.get('swaps', [])
    self.assertEqual(len(swaps_data), 2)
    for entry in swaps_data:
      self.assertIn('date', entry)
      self.assertIn('user_id', entry)
      self.assertIn('name', entry)
      orig_user = next(u for u in self.users if u.id == entry['user_id'])
      self.assertEqual(entry['name'], orig_user.name)
      
    # Check skips
    skips_data = data.get('skips', [])
    self.assertEqual(len(skips_data), 1)
    entry = skips_data[0]
    self.assertIn('date', entry)
    self.assertIn('user_id', entry)
    self.assertIn('name', entry)
    self.assertEqual(entry['user_id'], 222)
    self.assertEqual(entry['name'], 'Bob')

    # Now verify we can load it back correctly
    sch2 = self.make_scheduler()
    self.assertEqual(len(sch2.skips), 1)
    self.assertEqual(sch2.skips[0]['user_id'], 222)
    self.assertEqual(len(sch2.swaps), 2)

  @mock.patch('scheduler.datetime.date', new=MockDate)
  def test_user_requested_swap_scenario(self):
    """Test user requested scenario:
    - Monday: Bob is Tuesday, Alice is Thursday. Swap Bob and Alice.
      Verify Alice is Tuesday, Bob is Thursday.
    - Wednesday: Verify Bob is still Thursday (expired Tuesday swap pruned, Thursday swap survives).
    - Wednesday: Run !swap reset. Verify Alice is back on Thursday.
    """
    # Set up members: Charlie, Bob, David, Alice
    # Users will be: Charlie (333), Bob (222), David (444), Alice (111)
    users = [
      self.users[2], # Charlie (333)
      self.users[1], # Bob (222)
      self.users[3], # David (444)
      self.users[0]  # Alice (111)
    ]
    
    # 1. Today is Monday (2026-07-06)
    MockDate._today_val = datetime.date(2026, 7, 6)
    
    sch = self.make_scheduler(users)
    
    # Verify starting rotation order:
    # Monday (offset 0): Charlie
    # Tuesday (offset 1): Bob
    # Wednesday (offset 2): David
    # Thursday (offset 3): Alice
    self.assertEqual(sch.get_user_for_day(0).id, 333) # Charlie
    self.assertEqual(sch.get_user_for_day(1).id, 222) # Bob
    self.assertEqual(sch.get_user_for_day(2).id, 444) # David
    self.assertEqual(sch.get_user_for_day(3).id, 111) # Alice
    
    # Swap Bob and Alice
    sch.swap(self.users[1], self.users[0]) # Bob (222) and Alice (111)
    
    # Ensure Alice is now Tuesday, Bob is Thursday
    self.assertEqual(sch.get_user_for_day(1).id, 111) # Alice on Tuesday (offset 1)
    self.assertEqual(sch.get_user_for_day(3).id, 222) # Bob on Thursday (offset 3)
    
    # 2. Today is Wednesday (2026-07-08)
    MockDate._today_val = datetime.date(2026, 7, 8)
    
    # Reload/Sync state (which prunes expired swaps like Tuesday's)
    sch.load_state(users)
    
    # Ensure Bob is still Thursday (Thursday is tomorrow, offset 1 relative to Wednesday)
    self.assertEqual(sch.get_user_for_day(1).id, 222) # Bob on Thursday
    
    # Run swap reset
    sch.reset_swaps()
    
    # Ensure Alice is back on Thursday
    self.assertEqual(sch.get_user_for_day(1).id, 111) # Alice on Thursday


if __name__ == '__main__':
  unittest.main()