import unittest
from unittest import mock
import json
import os
import pathlib
import datetime
import calendar

import scheduler
import util

class MockMember:
  def __init__(self, id, name, nick=None):
    self.id = id
    self.name = name
    self.nick = nick

class SchedulerTestCase(unittest.TestCase):
  def setUp(self):
    # Setup some test users
    self.users = [
      MockMember(111, 'Alice', 'Aly'),
      MockMember(222, 'Bob'),
      MockMember(333, 'Charlie', 'Chaz'),
      MockMember(444, 'David')
    ]
    # Clean up test schedule file if it exists
    self.test_data_dir = util.get_data_folder()
    self.state_file = self.test_data_dir / 'schedule.json'
    if self.state_file.exists():
      os.remove(self.state_file)

  def tearDown(self):
    if self.state_file.exists():
      os.remove(self.state_file)

  def test_on_call(self):
    sch = scheduler.Scheduler(self.users)
    self.assertEqual(sch.on_call.id, 111)
    
    # Rotate and check
    sch.rotate()
    self.assertEqual(sch.on_call.id, 222)

  def test_get_user_for_day(self):
    sch = scheduler.Scheduler(self.users)
    # Day 0: Alice
    # Day 1: Bob
    # Day 2: Charlie
    # Day 3: David
    # Day 4: Alice
    self.assertEqual(sch.get_user_for_day(0).id, 111)
    self.assertEqual(sch.get_user_for_day(1).id, 222)
    self.assertEqual(sch.get_user_for_day(4).id, 111)

  def test_swap_happy(self):
    sch = scheduler.Scheduler(self.users)
    
    # Alice (day_index + 0) swaps with Charlie (day_index + 2)
    sch.swap(self.users[0], self.users[2])
    
    # After swap, day 0 should be Charlie (333), day 2 should be Alice (111)
    self.assertEqual(sch.get_user_for_day(0).id, 333)
    self.assertEqual(sch.get_user_for_day(1).id, 222)
    self.assertEqual(sch.get_user_for_day(2).id, 111)
    
    # Test persistence of swap
    sch2 = scheduler.Scheduler(self.users)
    self.assertEqual(sch2.get_user_for_day(0).id, 333)
    self.assertEqual(sch2.get_user_for_day(2).id, 111)

  def test_swap_same_person(self):
    sch = scheduler.Scheduler(self.users)
    with self.assertRaises(ValueError):
      sch.swap(self.users[0], self.users[0])

  def test_swap_not_in_schedule(self):
    sch = scheduler.Scheduler(self.users)
    fake_user = MockMember(999, 'Fake')
    with self.assertRaises(ValueError):
      sch.swap(self.users[0], fake_user)

  def test_rotate_cleanup(self):
    sch = scheduler.Scheduler(self.users)
    # Alice (0) swaps with Bob (1)
    sch.swap(self.users[0], self.users[1])
    
    # Swaps dict should have keys for day 0 and day 1
    self.assertIn(0, sch.swaps)
    self.assertIn(1, sch.swaps)
    
    # Rotate once (day_index becomes 1)
    sch.rotate()
    # Day 0 swap should be cleaned up, Day 1 should remain
    self.assertNotIn(0, sch.swaps)
    self.assertIn(1, sch.swaps)
    
    # Rotate again (day_index becomes 2)
    sch.rotate()
    self.assertNotIn(1, sch.swaps)

  def test_generate_schedule(self):
    sch = scheduler.Scheduler(self.users)
    # Alice swaps with Bob
    sch.swap(self.users[0], self.users[1])
    
    table = sch.generate_schedule()
    self.assertIn("Dishes Schedule", table)
    self.assertIn("Aly", table) # Alice's nick
    self.assertIn("Bob", table)
    self.assertIn("(swapped)", table)

  def test_load_state_add_and_remove_users(self):
    sch = scheduler.Scheduler(self.users)
    self.assertEqual([u.id for u in sch.base_queue], [111, 222, 333, 444])
    
    new_users = [
      MockMember(111, 'Alice', 'Aly'),
      MockMember(333, 'Charlie', 'Chaz'),
      MockMember(555, 'Eve')
    ]
    
    # Day 0 (Alice) swaps with Day 1 (Bob)
    sch.swap(self.users[0], self.users[1])
    self.assertIn(0, sch.swaps)
    self.assertEqual(sch.swaps[0], 222)
    
    sch.load_state(new_users)
    
    # Queue order check
    self.assertEqual([u.id for u in sch.base_queue], [111, 333, 555])
    
    # Swap cleanup check
    self.assertNotIn(0, sch.swaps)
    
    # Persistence check
    self.assertTrue(sch._state_file.exists())

  def test_swap_back_removes_swap_cache(self):
    sch = scheduler.Scheduler(self.users)
    
    # Swap Alice (0) and Bob (1)
    sch.swap(self.users[0], self.users[1])
    self.assertIn(0, sch.swaps)
    self.assertIn(1, sch.swaps)
    
    # Swap them back
    sch.swap(self.users[0], self.users[1])
    # Now they should be removed from the swap cache
    self.assertNotIn(0, sch.swaps)
    self.assertNotIn(1, sch.swaps)

  def test_load_state_remove_user_clears_related_swaps(self):
    sch = scheduler.Scheduler(self.users)
    
    # Swap Bob (222) and Charlie (333)
    sch.swap(self.users[1], self.users[2])
    self.assertIn(1, sch.swaps) # Bob's day
    self.assertIn(2, sch.swaps) # Charlie's day
    self.assertEqual(sch.swaps[1], 333)
    self.assertEqual(sch.swaps[2], 222)
    
    # Load state removing Bob (222)
    new_users = [
      MockMember(111, 'Alice', 'Aly'),
      MockMember(333, 'Charlie', 'Chaz'),
      MockMember(444, 'David')
    ]
    sch.load_state(new_users)
    
    # Both swap entries (day 1 and day 2) should be removed
    self.assertNotIn(1, sch.swaps)
    self.assertNotIn(2, sch.swaps)

  def test_skip(self):
    sch = scheduler.Scheduler(self.users)
    # Default order: Alice (111), Bob (222), Charlie (333), David (444)
    self.assertEqual(sch.get_user_for_day(0).id, 111)
    self.assertEqual(sch.get_user_for_day(1).id, 222)
    self.assertEqual(sch.get_user_for_day(2).id, 333)
    
    # Skip Bob (222)'s next appearance (Day 1)
    sch.skip(self.users[1])
    self.assertIn(1, sch.skips)
    
    # After skip, Day 1 should be Charlie (333), Day 2 should be David (444), Day 3 should be Alice (111), Day 4 should be Bob (222)
    self.assertEqual(sch.get_user_for_day(0).id, 111)
    self.assertEqual(sch.get_user_for_day(1).id, 333)
    self.assertEqual(sch.get_user_for_day(2).id, 444)
    self.assertEqual(sch.get_user_for_day(3).id, 111)
    self.assertEqual(sch.get_user_for_day(4).id, 222)
    
    # Rotate to Day 2 (skipping Day 1 which contains the skip)
    sch.rotate() # Day 1
    sch.rotate() # Day 2
    # Skips should be cleaned up
    self.assertNotIn(1, sch.skips)

  def test_skip_reset_and_display(self):
    sch = scheduler.Scheduler(self.users)
    
    # Skip Bob
    sch.skip(self.users[1])
    self.assertEqual(len(sch.skips), 1)
    
    # Verify schedule table footer matches expected printout
    table = sch.generate_schedule()
    self.assertIn("Skipped members:", table)
    self.assertIn("Bob", table)
    self.assertIn("Run `!skip reset` to reset all skipped entries.", table)
    
    # Reset
    sch.reset_skips()
    self.assertEqual(len(sch.skips), 0)
    
    # Verify schedule table footer is removed
    table_after = sch.generate_schedule()
    self.assertNotIn("Skipped members:", table_after)

  def test_swap_reset_and_display(self):
    sch = scheduler.Scheduler(self.users)
    
    # Swap Alice and Bob
    sch.swap(self.users[0], self.users[1])
    self.assertEqual(len(sch.swaps), 2)
    
    # Verify schedule table footer matches expected printout
    table = sch.generate_schedule()
    self.assertIn("Run `!swap reset` to reset all swapped entries.", table)
    
    # Reset
    sch.reset_swaps()
    self.assertEqual(len(sch.swaps), 0)
    
    # Verify schedule table footer is removed
    table_after = sch.generate_schedule()
    self.assertNotIn("Run `!swap reset` to reset all swapped entries.", table_after)

  def test_skip_undoes_swaps_first(self):
    sch = scheduler.Scheduler(self.users)
    
    # Alice (0) swaps with Bob (1)
    sch.swap(self.users[0], self.users[1])
    self.assertEqual(len(sch.swaps), 2)
    self.assertEqual(sch.swaps[0], 222) # Bob is on Alice's day
    self.assertEqual(sch.swaps[1], 111) # Alice is on Bob's day
    
    # Skip Alice (111)
    sch.skip(self.users[0])
    
    # The swap should be undone completely
    self.assertEqual(len(sch.swaps), 0)
    
    # Alice should be skipped for her next appearance (day 0)
    self.assertIn(0, sch.skips)
    
    # Check the actual schedule mapping after skip:
    # Alice (0) was skipped.
    # Day 0 should be Bob (222)
    # Day 1 should be Charlie (333)
    self.assertEqual(sch.get_user_for_day(0).id, 222)
    self.assertEqual(sch.get_user_for_day(1).id, 333)

if __name__ == '__main__':
  unittest.main()