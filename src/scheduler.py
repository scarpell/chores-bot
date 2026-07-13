from typing import List, Optional, Text

import calendar
import datetime
import discord
import json
import logging
import types

import util


def _make_user(entry: dict, member=None):
    """Return a user-like SimpleNamespace for a rotation entry.

    Args:
      entry:  dict with 'id', 'name', and optional 'removed' key.
      member: Live discord.Member (or MockMember in tests) for .nick lookup.
    """
    removed = entry.get('removed', False)
    return types.SimpleNamespace(
        id=entry['id'],
        name=entry['name'],
        nick=member.nick if member is not None and not removed else None,
        removed=removed,
    )


class Scheduler:
    def __init__(self, logger_name='discord'):
        """Set up scheduler infrastructure.

        Note: Does NOT initialize rotation state. Call load_state(users) first.
        """
        self._logger = logging.getLogger(logger_name)
        self._state_file = util.get_data_folder() / 'schedule.json'

    # ------------------------------------------------------------------
    # Persistence — fetch rotation from disk
    # ------------------------------------------------------------------

    def load_state(self, users: List[discord.Member]):
        """Load (or initialize) rotation state from disk.

        Raises ValueError if users is empty (no members with the chore role).

        member_list
            Ordered list of current role-holders.  Removed members are
            dropped; newly-added members are appended to the end.

        rotation
            Day-by-day list of assignments from rotation_start_date.
            Never re-ordered — only cleaned up (past days trimmed),
            flagged (removed=True for lost-role members), or extended
            at the end.
        """
        if not users:
            raise ValueError('No members with the chore role found.')

        if not self._state_file.exists():
            self._init_fresh(users)
            self.save_state()
            return

        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            user_map = {u.id: u for u in users}

            # Reconcile member_list: preserve saved order, drop removed, append new.
            saved_ml_ids = [int(e['id']) for e in data.get('member_list', [])]
            self.member_list = [user_map[uid] for uid in saved_ml_ids if uid in user_map]
            self.member_list += [u for u in users if u.id not in set(saved_ml_ids)]

            # Load rotation (with legacy fallback).
            rot = data.get('rotation') or {}
            self.rotation_start_date = rot.get(
                'start_date', datetime.date.today().isoformat())
            raw_users = rot.get('users', [])

            if raw_users:
                self.rotation_users = [
                    {
                        'id': int(e['id']),
                        'name': e.get('name', str(e['id'])),
                        **({'removed': True} if e.get('removed') else {}),
                    }
                    for e in raw_users
                ]
            else:
                legacy = data.get('base_queue', data.get('queue', []))
                if legacy:
                    self._logger.info('Migrating legacy base_queue format.')
                    self.rotation_users = [
                        {
                            'id': int(e['id'] if isinstance(e, dict) else e),
                            'name': (
                                e.get('name', str(e['id']))
                                if isinstance(e, dict) else str(e)
                            ),
                        }
                        for e in legacy
                    ]
                else:
                    self.rotation_users = [
                        {'id': u.id, 'name': u.name} for u in self.member_list
                    ]
                self.rotation_start_date = datetime.date.today().isoformat()

            # Trim past days, then flag entries for members who lost the role.
            self._cleanup_past_days()
            removed_ids = set(saved_ml_ids) - set(user_map.keys())
            self._mark_removed(removed_ids)

            # Compute round-robin position and ensure a starting buffer.
            self.extension_index = self._compute_extension_index()
            while len(self.printable_schedule()) < len(self.member_list):
                self._extend_rotation_by_one()

            self.save_state()
            self._logger.info('Rotation state loaded from disk.')

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            self._logger.error(
                'Failed to load rotation state ({}). Starting fresh.'.format(e))
            self._init_fresh(users)
            self.save_state()

    def _mark_removed(self, user_ids: set):
        """Set removed=True on all rotation entries for the given user IDs."""
        if not user_ids:
            return
        for entry in self.rotation_users:
            if entry['id'] in user_ids:
                entry['removed'] = True

    def _compute_extension_index(self) -> int:
        """Find where to resume the round-robin from the last visible entry."""
        if not self.member_list:
            return 0
        id_to_index = {u.id: i for i, u in enumerate(self.member_list)}
        for entry in reversed(self.rotation_users):
            idx = id_to_index.get(entry['id'])
            if idx is not None and not entry.get('removed', False):
                return (idx + 1) % len(self.member_list)
        return 0

    def _init_fresh(self, users: List[discord.Member]):
        """Initialize all state from scratch."""
        self.member_list = list(users)
        self.rotation_users = [{'id': u.id, 'name': u.name} for u in users]
        self.rotation_start_date = datetime.date.today().isoformat()
        self.extension_index = 0

    def save_state(self):
        """Persist member_list and rotation to disk."""
        try:
            data = {
                'member_list': [{'id': u.id, 'name': u.name} for u in self.member_list],
                'rotation': {
                    'start_date': self.rotation_start_date,
                    'users': self.rotation_users,
                },
            }
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self._logger.error('Failed to save rotation state: {}'.format(e))

    # ------------------------------------------------------------------
    # Rotation maintenance
    # ------------------------------------------------------------------

    def _cleanup_past_days(self):
        """Trim days before today from rotation_users, advancing start_date."""
        today = datetime.date.today()
        start = datetime.date.fromisoformat(self.rotation_start_date)
        days_to_drop = (today - start).days
        if days_to_drop <= 0:
            return
        self.rotation_users = (
            self.rotation_users[days_to_drop:]
            if days_to_drop < len(self.rotation_users)
            else []
        )
        self.rotation_start_date = today.isoformat()
        self._logger.info(
            'Trimmed {} past day(s). Rotation start: {}.'.format(
                days_to_drop, self.rotation_start_date))

    def _extend_rotation_by_one(self):
        """Append the next round-robin member to the end of the rotation."""
        m = self.member_list[self.extension_index]
        self.rotation_users.append({'id': m.id, 'name': m.name})
        self.extension_index = (self.extension_index + 1) % len(self.member_list)

    # ------------------------------------------------------------------
    # Schedule — printable view of the rotation
    # ------------------------------------------------------------------

    def printable_schedule(self):
        """Return the rotation with removed entries filtered out.

        Each item is a (date, entry) tuple where date is the calendar day
        that entry is assigned.  The list preserves rotation order.
        """
        start = datetime.date.fromisoformat(self.rotation_start_date)
        return [
            (start + datetime.timedelta(days=i), e)
            for i, e in enumerate(self.rotation_users)
            if not e.get('removed', False)
        ]

    @property
    def on_call(self) -> Optional[object]:
        """Today's assigned user, or None if today's slot is removed.

        Generates the printable schedule and selects the first entry.
        If that entry's date is not today (today is a removed gap),
        returns None — no one is on call.
        """
        while not self.printable_schedule():
            self._extend_rotation_by_one()
        date, entry = self.printable_schedule()[0]
        if date != datetime.date.today():
            return None  # Today is a removed/gap day.
        member_map = {u.id: u for u in self.member_list}
        return _make_user(entry, member_map.get(entry['id']))

    def generate_schedule(self) -> Text:
        """Generate a 7-column schedule table.

        Extends the rotation one entry at a time until the printable
        schedule has at least 7 entries, then renders the first 7.
        """
        while len(self.printable_schedule()) < 7:
            self._extend_rotation_by_one()
        self.save_state()

        today = datetime.date.today()
        member_map = {u.id: u for u in self.member_list}

        cols = []
        for entry_date, entry in self.printable_schedule()[:7]:
            d_str = entry_date.strftime('%B ') + str(entry_date.day)
            day_str = (
                calendar.day_abbr[entry_date.weekday()]
                + (' (today)' if entry_date == today else '')
            )
            display = util.discord_name(_make_user(entry, member_map.get(entry['id'])))
            w = max(len(d_str), len(day_str), len(display)) + 2
            cols.append((d_str.center(w), day_str.center(w), display.center(w), '-' * w))

        table_str = 'Dishes Schedule\n' + '=' * 15 + '\n\n'
        table_str += '|' + '|'.join(c[0] for c in cols) + '|\n'
        table_str += '|' + '|'.join(c[1] for c in cols) + '|\n'
        table_str += '+' + '+'.join(c[3] for c in cols) + '+\n'
        table_str += '|' + '|'.join(c[2] for c in cols) + '|\n'
        return table_str