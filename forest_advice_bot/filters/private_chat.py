# forest-advice-bot/filters/private_chat.py

"""Фильтры по типу чата. Бот работает только в личных сообщениях (R1)."""

from aiogram import F

PRIVATE_CHAT = F.chat.type == "private"
GROUP_CHAT = F.chat.type.in_({"group", "supergroup"})
