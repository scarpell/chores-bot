from typing import List, Text, Tuple

import calendar
import datetime
import discord
import json
import logging
import pytablewriter

import util


class Scheduler:
  def __init__(self, users: List[discord.Member], logger_name='discord'):
    """Create a new scheduler.

    Args:
      users (List[discord.Member]): The users to put in the system.
      logger_name (str, optional): Logger's name. Defaults to 'discord'.
    """
    self.signed_off = False
    self._users = users
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
        
      self.signed_off = data.get('signed_off', False)
      
      # Rebuild the user queue based on the saved IDs
      saved_ids = data.get('queue', [])
      user_map = {u.id: u for u in initial_users}
      
      new_queue = []
      for uid in saved_ids:
        if uid in user_map:
          new_queue.append(user_map[uid])
          del user_map[uid]
          
      # Append any new users not in the saved file
      for user in user_map.values():
        new_queue.append(user)
        
      if new_queue:
        self._users = new_queue
        
      self._logger.info('Schedule state loaded from disk.')
    except Exception as e:
      self._logger.error('Failed to load schedule state: {}'.format(e))

  def save_state(self):
    """Save the schedule state to disk."""
    try:
      data = {
        'signed_off': self.signed_off,
        'queue': [u.id for u in self._users]
      }
      with open(self._state_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    except Exception as e:
      self._logger.error('Failed to save schedule state: {}'.format(e))

  def reset_signoff(self):
    """Reset the signoff status and save state."""
    self.signed_off = False
    self.save_state()
    self._logger.info('Signoff status has been reset.')

  @property
  def on_call(self) -> discord.Member:
    """Returns:
      discord.Member: Which discord user is on call for the chores tonight
    """
    return self._users[0]

  def generate_schedule(self) -> Text:
    """Generate a markdown representation of who is on call when.

    Returns:
      Text: User forcast for next 7 days, in a formatted ascii table.
    """
    today = datetime.datetime.today()
    
    headers_line1 = []
    headers_line2 = []
    people_line = []
    
    for i in range(7):
      target_date = today + datetime.timedelta(days=i)
      date_str = '{} {}'.format(target_date.strftime('%B'), target_date.day)
      
      day_str = calendar.day_abbr[target_date.weekday()]
      if i == 0:
        day_str += ' (today)'
        
      user = self._users[i % len(self._users)]
      user_str = util.discord_name(user)
      
      col_width = max(len(date_str), len(day_str), len(user_str)) + 2
      
      headers_line1.append(' {} '.format(date_str).center(col_width))
      headers_line2.append(' {} '.format(day_str).center(col_width))
      people_line.append(' {} '.format(user_str).center(col_width))
      
    header_sep = '+' + '+'.join('-' * len(h) for h in headers_line1) + '+'
    
    table_str = 'Dishes Schedule\n'
    table_str += '=' * 15 + '\n\n'
    table_str += '|' + '|'.join(headers_line1) + '|\n'
    table_str += '|' + '|'.join(headers_line2) + '|\n'
    table_str += header_sep + '\n'
    table_str += '|' + '|'.join(people_line) + '|\n'
    
    return table_str

  def swap(self, mem1: discord.Member, mem2: discord.Member):
    """Swap two user's positions in the queue.

    Args:
      mem1 (discord.Member): First member to swap
      mem2 (discord.Member): Second member to swap

    Raises:
      ValueError: Members are the same.
    """
    if mem1 == mem2:
      raise ValueError('Members need to be different.')

    mem1_idx = self._users.index(mem1)
    mem2_idx = self._users.index(mem2)

    self._users[mem1_idx] = mem2
    self._users[mem2_idx] = mem1

    self._logger.info('{} (id: {}) and {} (id: {}) have been swapped.'.format(
      util.discord_name(mem1), mem1.id, util.discord_name(mem2), mem2.id))
    self._logger.info('The user queue is now: [{}]'.format(
      ', '.join(util.discord_name(u) for u in self._users)))
    self.save_state()

  def rotate(self):
    """Rotate the queue to the next user."""
    self._users.append(self._users.pop(0))
    self._logger.info('Queue rotated automatically.')
    self._logger.info('{} (id: {}) is now on call'.format(
      util.discord_name(self.on_call), self.on_call.id))
    self.save_state()

  def signoff(self):
    """Signoff a user for completing their task."""
    self._users.append(self._users.pop(0))
    self.signed_off = True

    self._logger.info('{} (id: {}) has been signed off.'.format(
      util.discord_name(self._users[-1]), self._users[-1].id))
    self._logger.info('{} (id: {}) is now on call'.format(
      util.discord_name(self.on_call), self.on_call.id))
    self.save_state()