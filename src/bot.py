# =================================
# Imports
# =================================
from discord.ext import commands
from discord.ext import tasks

import asyncio
import datetime
import discord
import logging
import os
import zoneinfo

import scheduler
import util


# =================================
# Logging setup
# =================================
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
formatter = logging.Formatter(
  '[%(levelname)s] {%(funcName)s | %(filename)s} %(asctime)s:  %(message)s')

file_handler = logging.FileHandler(
  filename=util.get_logs_folder() / 'kitchen-chores-bot-{}.log'.format(
    datetime.datetime.now()),
  encoding='utf-8', mode='w')
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.DEBUG)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.WARNING)
logger.addHandler(console_handler)


# =================================
# Bot parameters
# =================================
COMMAND_PREFIX = '!'

NOTIFICATION_FREQUENCY = {'minutes': 60.0}

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned_or(COMMAND_PREFIX), 
                   intents=intents)

sch: scheduler.Scheduler = None
_default_channel = None


# =================================
# Bot Commands
# =================================
def get_chore_members():
  try:
    guild_id = int(os.getenv('GUILD'))
    role_id = int(os.getenv('ROLE'))
  except (TypeError, ValueError):
    return None

  guild = discord.utils.get(bot.guilds, id=guild_id)
  if not guild:
    return None

  role = discord.utils.get(guild.roles, id=role_id)
  if not role:
    return None

  bot_role = discord.utils.get(guild.roles, name='bot')
  
  return [
    m for m in guild.members
    if role in m.roles and (not bot_role or bot_role not in m.roles)
  ]


def sync_users():
  if sch is None:
    return
  users = get_chore_members()
  if users:   # None (config error) or [] (no members) — skip reload
    sch.load_state(users)


@bot.event
async def on_ready():
  global _default_channel
  
  try:
    guild_id = int(os.getenv('GUILD'))
    channel_id = int(os.getenv('CHANNEL'))
  except (TypeError, ValueError) as e:
    logger.error("GUILD or CHANNEL environment variables not set correctly: {}".format(e))
    return

  guild = discord.utils.get(bot.guilds, id=guild_id)
  if not guild:
    logger.error("Guild {} not found.".format(guild_id))
    return

  _default_channel = discord.utils.get(guild.channels, id=channel_id)
  if not _default_channel:
    logger.error("Channel {} not found.".format(channel_id))
    return

  # 1. Fetch current members from Discord
  users = get_chore_members()
  if not users:
    logger.error('No members with the chore role found; bot will not start.')
    return

  # 2. Load (or initialize) all schedule state from disk.
  # Only construct the Scheduler once — on reconnect, reuse the existing
  # instance and just reload state so the notify loop stays intact.
  global sch
  if sch is None:
    sch = scheduler.Scheduler()
  sch.load_state(users)

  # 3. Start the scheduler loop — only runs after state is fully loaded.
  # Guard against reconnects: on_ready can fire more than once.
  if not notify.is_running():
    notify.start()


@bot.before_invoke
async def before_any_command(ctx):
  sync_users()


@bot.command(name='today', help='Return the person who is on-call today')
async def on_call_today(ctx):
  user = sch.on_call
  if user is None:
    await ctx.message.channel.send('No one is assigned for dishes today.')
    return
  await ctx.message.channel.send(
    '<@{}> is responsible for the dishes today!'.format(user.id))

@bot.command(name='schedule', help='List the schedule for the seven days')
async def schedule(ctx):
  await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))


@bot.command(name='skip', help='Toggle skip for a member\'s next rotation slot')
async def cmd_skip(ctx, member: discord.Member):
  try:
    skipped = sch.skip_user(member.id)
  except ValueError as e:
    await ctx.message.channel.send(str(e))
    return
  if skipped:
    await ctx.message.channel.send(
      '{} will be skipped for their next turn.'.format(member.display_name))
  else:
    await ctx.message.channel.send(
      '{} has been un-skipped.'.format(member.display_name))


@tasks.loop(**NOTIFICATION_FREQUENCY)
async def notify():
  now_utc = datetime.datetime.now(datetime.timezone.utc)
  curr_time = now_utc.astimezone(zoneinfo.ZoneInfo('America/Denver'))

  if curr_time.hour == 9:
    sync_users()
    user = sch.on_call
    if user is None:
      logger.info('No one on call today; notification suppressed.')
    else:
      await _default_channel.send(
        '<@{}> is responsible for the dishes today'.format(user.id))
      logger.info('{} has been notified.'.format(util.discord_name(user)))
  else:
    logger.info('Notification suppressed.')


@notify.before_loop
async def notifications_init():
  """Sleep so that the notifications start on the hour."""
  now_utc = datetime.datetime.now(datetime.timezone.utc)
  next_hour = now_utc.replace(
    minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)

  delta = next_hour - now_utc
  logger.info('Sleeping {} seconds before activating notifications'.format(
    delta.total_seconds()))
  await asyncio.sleep(delta.total_seconds())


@bot.event
async def on_command_error(ctx, error):
  if isinstance(error, commands.MemberNotFound):
    await ctx.send("Could not find member: {}".format(error.argument))
  elif isinstance(error, commands.BadArgument):
    await ctx.send("Bad argument: {}".format(str(error)))
  elif isinstance(error, commands.MissingRequiredArgument):
    await ctx.send("Missing required argument: {}".format(error.param.name))
  else:
    logger.error('Unhandled exception in command {}: {}'.format(ctx.command, error))


if __name__ == '__main__':
  util.load_env()
  token = os.getenv('TOKEN')
  print(f"LOADED TOKEN: '{token}'", flush=True)
  bot.run(token)