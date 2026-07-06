"""/help — list the commands."""
from app.telegram.formatting import HELP_TEXT
from app.telegram.commands._helpers import reply


async def on_help(update, ctx):
    await reply(update, HELP_TEXT)
