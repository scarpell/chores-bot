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
  
  users = []
  for member in guild.members:
    if role in member.roles:
      if not bot_role or bot_role not in member.roles:
        users.append(member)
  return users


def sync_users():
  if sch is None:
    return
  users = get_chore_members()
  if users is not None:
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

  users = get_chore_members()
  if users is None:
    logger.error("Failed to fetch chore members during startup.")
    users = []

  global sch
  sch = scheduler.Scheduler(users)

  notify.start()
  return


@bot.before_invoke
async def before_any_command(ctx):
  sync_users()


@bot.command(name='today', help='Return the person who is on-call today')
async def on_call_today(ctx):
  await ctx.message.channel.send(
    '<@{}> is responsible for the dishes today!'.format(sch.on_call.id))
  return

  
@bot.command(name='schedule', help='List the schedule for the seven days')
async def schedule(ctx):
  await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))
  return


@bot.command(name='swap', help='Swap on call position with chosen person or reset swaps')
async def swap(ctx, arg1: str = None, arg2: str = None):
  if arg1 is None:
    await ctx.message.channel.send(
      '**Usage:**\n'
      '- `!swap @username1 @username2` (trades the upcoming days of two people)\n'
      '- `!swap reset` (resets all active swapped entries)'
    )
    return

  if arg1.lower() == 'reset':
    sch.reset_swaps()
    await ctx.message.channel.send('All swapped entries have been reset! The new schedule '
                                  'is as follows:')
    await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))
    return

  if arg2 is None:
    await ctx.message.channel.send(
      '**Usage:**\n'
      '- `!swap @username1 @username2` (trades the upcoming days of two people)\n'
      '- `!swap reset` (resets all active swapped entries)'
    )
    return

  # Convert arg1 to a Member
  try:
    member1 = await commands.MemberConverter().convert(ctx, arg1)
  except commands.MemberNotFound:
    await ctx.message.channel.send('Could not find member: {}'.format(arg1))
    return

  # Convert arg2 to a Member
  try:
    member2 = await commands.MemberConverter().convert(ctx, arg2)
  except commands.MemberNotFound:
    await ctx.message.channel.send('Could not find member: {}'.format(arg2))
    return

  try:
    sch.swap(member1, member2)
    await ctx.message.channel.send('Users have been switched! The new schedule '
                                  'should be as follows:')
    await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))
  except ValueError as e:
    await ctx.message.channel.send(str(e))
  except Exception as e:
    logger.error('Error in swap command: {}'.format(e))
    await ctx.message.channel.send('There was an error when trying to swap.')

  return


@bot.command(name='skip', help='Skip the next appearance of a user or reset skips')
async def skip(ctx, arg: str = None):
  if arg is None:
    await ctx.message.channel.send(
      '**Usage:**\n'
      '- `!skip @username` (skips the next appearance of the specified user)\n'
      '- `!skip reset` (resets all active skipped entries)'
    )
    return

  if arg.lower() == 'reset':
    sch.reset_skips()
    await ctx.message.channel.send('All skipped entries have been reset! The new schedule '
                                  'is as follows:')
    await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))
    return

  # Try converting the argument to a Member
  try:
    member = await commands.MemberConverter().convert(ctx, arg)
  except commands.MemberNotFound:
    # If conversion fails, let's output a usage message
    await ctx.message.channel.send(
      '**Usage:**\n'
      '- `!skip @username` (skips the next appearance of the specified user)\n'
      '- `!skip reset` (resets all active skipped entries)'
    )
    return

  try:
    was_skipped = sch.skip(member)
    if was_skipped:
      await ctx.message.channel.send('{} has been skipped for their next appearance! The new schedule '
                                    'is as follows:'.format(util.discord_name(member)))
    else:
      await ctx.message.channel.send('{} is no longer skipped! The new schedule '
                                    'is as follows:'.format(util.discord_name(member)))
    await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))
  except ValueError as e:
    await ctx.message.channel.send(str(e))
  except Exception as e:
    logger.error('Error in skip command: {}'.format(e))
    await ctx.message.channel.send('There was an error when trying to skip.')

  return



@tasks.loop(**NOTIFICATION_FREQUENCY)
async def notify():
  sync_users()
  now_utc = datetime.datetime.now(datetime.timezone.utc)
  curr_time = now_utc.astimezone(zoneinfo.ZoneInfo('America/Denver'))

  if curr_time.hour == 9:
    await _default_channel.send(
      '<@{}> is responsible for the dishes today'.format(
        sch.on_call.id))
  elif curr_time.hour == 0:
    sch.rotate()
  else:
    logger.info('Notification suppressed.')

  logger.info('{} has been notified.'.format(util.discord_name(sch.on_call)))
  return


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
  return


@bot.event
async def on_command_error(ctx, error):
  if isinstance(error, commands.MemberNotFound):
    await ctx.send("Could not find member: {}".format(error.argument))
  elif isinstance(error, commands.BadArgument):
    await ctx.send("Bad argument: {}".format(str(error)))
  elif isinstance(error, commands.MissingRequiredArgument):
    await ctx.send("Missing required argument: {}".format(error.param.name))
  else:
    logger.error("Ignoring exception in command {}: {}".format(ctx.command, error))
    await bot.on_command_error(ctx, error)


if __name__ == '__main__':
  util.load_env()
  token = os.getenv('TOKEN')
  print(f"LOADED TOKEN: '{token}'", flush=True)
  bot.run(token)