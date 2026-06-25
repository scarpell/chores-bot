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
@bot.event
async def on_ready():
  global _default_channel
  _guild = next(filter(lambda g: g.id == int(os.getenv('GUILD')), bot.guilds))
  _default_channel = next(filter(lambda c: c.id == int(os.getenv('CHANNEL')), 
                      _guild.channels))
  role = next(filter(lambda r: r.id == int(os.getenv('ROLE')), _guild.roles))
  bot_role = next(filter(lambda r: r.name == 'bot', _guild.roles))

  users = []
  for member in _guild.members:
    if role in member.roles and bot_role not in member.roles:
      users.append(member)

  global sch
  sch = scheduler.Scheduler(users)

  server_str = """

            ██████╗██████╗███╗   ███╗██████╗███╗   ████████╗              
            ██╔═══████╔══██████╗ ██████╔═══██████╗  ████╔══██╗             
            ██║   ████████╔██╔████╔████║   ████╔██╗ ████║  ██║             
            ██║   ████╔══████║╚██╔╝████║   ████║╚██╗████║  ██║             
            ╚██████╔██║  ████║ ╚═╝ ██╚██████╔██║ ╚██████████╔╝             
            ╚═════╝╚═╝  ╚═╚═╝     ╚═╝╚═════╝╚═╝  ╚═══╚═════╝              
   
    ████████╗  ██╗██████╗██████╗███████╗        ███╗   ███╗██████╗██████╗ 
    ██╔════██║  ████╔═══████╔══████╔════╝        ████╗ ██████╔════╝██╔══██╗
    ██║    █████████║   ████████╔█████╗          ██╔████╔████║  █████████╔╝
    ██║    ██╔══████║   ████╔══████╔══╝          ██║╚██╔╝████║   ████╔══██╗
    ╚████████║  ██╚██████╔██║  █████████╗        ██║ ╚═╝ ██╚██████╔██║  ██║
    ╚═════╚═╝  ╚═╝╚═════╝╚═╝  ╚═╚══════╝        ╚═╝     ╚═╝╚═════╝╚═╝  ╚═╝

Registered Users:
{}

Configured to notify every {} {}.

Now Serving.\n""".format('\n'.join(u.nick or u.name for u in users),
                         list(NOTIFICATION_FREQUENCY.values())[0],
                         list(NOTIFICATION_FREQUENCY.keys())[0])

  logger.info(server_str)
  print(server_str, flush=True)
  notify.start()
  return


@bot.command(name='today', help='Return the person who is on-call today')
async def on_call_today(ctx):
  await ctx.message.channel.send(
    '<@{}> is responsible for the kitchen tonight!'.format(sch.on_call.id))
  return

  
@bot.command(name='schedule', help='List the schedule for the seven days')
async def schedule(ctx):
  await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))
  return


@bot.command(name='swap', help='Swap on call position with chosen person')
async def swap(ctx, member: discord.Member):
  try:
    sch.swap(ctx.message.author, member)
  
    await ctx.message.channel.send('Users have been switched! The new schedule '
                                  'should be as follows:')
    await ctx.message.channel.send('```{}```'.format(sch.generate_schedule()))
  except ValueError:
    await ctx.message.channel.send('You can\'t swap with yourself!')
  except:
    await ctx.message.channel.send('There was an error when trying to swap.')

  return



@tasks.loop(**NOTIFICATION_FREQUENCY)
async def notify():
  now_utc = datetime.datetime.now(datetime.timezone.utc)
  curr_time = now_utc.astimezone(zoneinfo.ZoneInfo('America/Denver'))

  if curr_time.hour == 9:
    await _default_channel.send(
      'Reminder that <@{}> is responsible for the kitchen tonight!'.format(
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

  
if __name__ == '__main__':
  util.load_env()
  token = os.getenv('TOKEN')
  print(f"LOADED TOKEN: '{token}'", flush=True)
  bot.run(token)