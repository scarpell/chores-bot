from typing import List, Text, Tuple

import calendar
import datetime
import discord
import json
import logging

import util


class Scheduler:
  def __init__(self, users: List[discord.Member], logger_name='discord'):
    """Create a new scheduler.

    Args:
      users (List[discord.Member]): The users to put in the system.
      logger_name (str, optional): Logger's name. Defaults to 'discord'.
    """
    self.base_queue = users
    self.day_index = 0
    self.swaps = {}
    self.skips = []
    self._logger = logging.getLogger(logger_name)
    self._state_file = util.get_data_folder() / 'schedule.json'
    self.load_state(users)

  def load_state(self, initial_users: List[discord.Member]):
    """Load the schedule state from disk."""
    if not self._state_file.exists():
      self.save_state()
      return

    try:
      with open(self._state_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
      self.day_index = data.get('day_index', 0)
      raw_swaps = data.get('swaps', {})
      self.swaps = {int(k): v for k, v in raw_swaps.items()}
      self.skips = data.get('skips', [])
      
      # Rebuild the base queue based on the saved IDs
      saved_ids = data.get('base_queue', data.get('queue', []))
      initial_user_ids = {u.id for u in initial_users}
      user_map = {u.id: u for u in initial_users}
      
      # Detect removed and added users
      removed_ids = [uid for uid in saved_ids if uid not in initial_user_ids]
      saved_ids_set = set(saved_ids)
      added_users = [u for u in initial_users if u.id not in saved_ids_set]
      
      new_queue = []
      for uid in saved_ids:
        if uid in user_map:
          new_queue.append(user_map[uid])
          del user_map[uid]
          
      # Append newly added users to the end
      for user in added_users:
        new_queue.append(user)
        
      if new_queue:
        self.base_queue = new_queue
        
      if removed_ids or added_users:
        self._logger.info(
          'Schedule updated: added users {}, removed user IDs {}'.format(
            [u.name for u in added_users], removed_ids)
        )
        # Clean up any swaps involving removed users (both swapped-in and original owners)
        for abs_day in list(self.swaps.keys()):
          # Check if the swapped-in user is a removed user
          if self.swaps[abs_day] in removed_ids:
            del self.swaps[abs_day]
            continue
            
          # Check if the original owner of this day was a removed user
          if len(saved_ids) > 0:
            original_owner_id = saved_ids[abs_day % len(saved_ids)]
            if original_owner_id in removed_ids:
              del self.swaps[abs_day]
        self.save_state()
      else:
        self._logger.info('Schedule state loaded from disk.')
    except Exception as e:
      self._logger.error('Failed to load schedule state: {}'.format(e))

  def save_state(self):
    """Save the schedule state to disk."""
    try:
      data = {
        'day_index': self.day_index,
        'swaps': self.swaps,
        'skips': self.skips,
        'base_queue': [u.id for u in self.base_queue]
      }
      with open(self._state_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    except Exception as e:
      self._logger.error('Failed to save schedule state: {}'.format(e))

  def get_user_for_day(self, offset: int) -> discord.Member:
    abs_day = self.day_index + offset
    if abs_day in self.swaps:
      user_id = self.swaps[abs_day]
      for u in self.base_queue:
        if u.id == user_id:
          return u
    # Offset queue mapping based on how many skips occurred before or on this day
    num_skips = sum(1 for s in self.skips if s <= abs_day)
    base_idx = (abs_day + num_skips) % len(self.base_queue)
    return self.base_queue[base_idx]

  @property
  def on_call(self) -> discord.Member:
    """Returns:
      discord.Member: Which discord user is on call for the chores tonight
    """
    return self.get_user_for_day(0)

  def generate_schedule(self) -> Text:
    """Generate a markdown representation of who is on call when.

    Returns:
      Text: User forcast for next 7 days, in a formatted ascii table.
    """
    today = datetime.datetime.today()
    
    headers_line1 = []
    headers_line2 = []
    people_line1 = []
    people_line2 = []
    
    any_swaps = any((self.day_index + j) in self.swaps for j in range(7))
    
    for i in range(7):
      target_date = today + datetime.timedelta(days=i)
      date_str = '{} {}'.format(target_date.strftime('%B'), target_date.day)
      
      day_str = calendar.day_abbr[target_date.weekday()]
      if i == 0:
        day_str += ' (today)'
        
      user = self.get_user_for_day(i)
      user_str = util.discord_name(user)
      
      swap_str = '(swapped)' if (self.day_index + i) in self.swaps else ''
      
      col_width = max(len(date_str), len(day_str), len(user_str), len(swap_str)) + 2
      
      headers_line1.append(' {} '.format(date_str).center(col_width))
      headers_line2.append(' {} '.format(day_str).center(col_width))
      people_line1.append(' {} '.format(user_str).center(col_width))
      if any_swaps:
        people_line2.append(' {} '.format(swap_str).center(col_width))
      
    header_sep = '+' + '+'.join('-' * len(h) for h in headers_line1) + '+'
    
    table_str = 'Dishes Schedule\n'
    table_str += '=' * 15 + '\n\n'
    table_str += '|' + '|'.join(headers_line1) + '|\n'
    table_str += '|' + '|'.join(headers_line2) + '|\n'
    table_str += header_sep + '\n'
    table_str += '|' + '|'.join(people_line1) + '|\n'
    if any_swaps:
      table_str += '|' + '|'.join(people_line2) + '|\n'
      
    if self.skips:
      table_str += '\nSkipped members:\n'
      for s in self.skips:
        target_date = today + datetime.timedelta(days=(s - self.day_index))
        date_str = '{} {}'.format(target_date.strftime('%B'), target_date.day)
        num_skips_before = sum(1 for x in self.skips if x < s)
        skipped_user = self.base_queue[(s + num_skips_before) % len(self.base_queue)]
        table_str += '- {} (on {})\n'.format(util.discord_name(skipped_user), date_str)
      table_str += '\nRun `!skip reset` to reset all skipped entries.\n'
    
    if self.swaps:
      table_str += '\nRun `!swap reset` to reset all swapped entries.\n'
      
    return table_str

  def get_next_appearance(self, member: discord.Member) -> int:
    for i in range(100):
      if self.get_user_for_day(i).id == member.id:
        return self.day_index + i
    raise ValueError('Member not found in upcoming schedule')

  def swap(self, mem1: discord.Member, mem2: discord.Member):
    """Swap two user's upcoming positions in the queue."""
    if mem1.id == mem2.id:
      raise ValueError('Members need to be different.')

    day1 = self.get_next_appearance(mem1)
    day2 = self.get_next_appearance(mem2)

    self.swaps[day1] = mem2.id
    self.swaps[day2] = mem1.id

    # If a day maps back to its default user, remove the swap entry
    for day in (day1, day2):
      default_user = self.base_queue[day % len(self.base_queue)]
      if day in self.swaps and self.swaps[day] == default_user.id:
        del self.swaps[day]

    self._logger.info('Swapped {} (day {}) and {} (day {}).'.format(
      util.discord_name(mem1), day1, util.discord_name(mem2), day2))
    self.save_state()

  def skip(self, member: discord.Member):
    """Skip the next appearance of the member."""
    skip_day = self.get_next_appearance(member)
    if skip_day not in self.skips:
      self.skips.append(skip_day)
      self.skips.sort()
      self._logger.info('Skipped {} on day {}.'.format(
        util.discord_name(member), skip_day))
      self.save_state()

  def reset_skips(self):
    """Remove all skipped entries."""
    self.skips = []
    self._logger.info('Reset all skipped entries.')
    self.save_state()

  def reset_swaps(self):
    """Remove all swapped entries."""
    self.swaps = {}
    self._logger.info('Reset all swapped entries.')
    self.save_state()

  def rotate(self):
    """Rotate the queue to the next user."""
    self.day_index += 1
    # Cleanup expired swaps
    old_keys = [k for k in self.swaps.keys() if k < self.day_index]
    for k in old_keys:
      del self.swaps[k]
      
    # Cleanup expired skips and adjust day_index to maintain correct mapping
    old_skips = [s for s in self.skips if s < self.day_index]
    if old_skips:
      self.day_index += len(old_skips)
      self.skips = [s for s in self.skips if s >= self.day_index]
      
    self._logger.info('Queue rotated automatically.')
    self._logger.info('{} (id: {}) is now on call'.format(
      util.discord_name(self.on_call), self.on_call.id))
    self.save_state()