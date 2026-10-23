from unittest.mock import AsyncMock

from keyboards.set_menu import set_main_menu


async def test_menu_registers_all_commands():
    bot = AsyncMock()
    await set_main_menu(bot)

    commands = bot.set_my_commands.await_args.args[0]
    assert [c.command for c in commands] == ["start", "help", "mydocs"]
    assert all(c.description for c in commands)
