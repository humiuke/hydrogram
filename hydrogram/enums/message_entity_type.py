#  Hydrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-2023 Dan <https://github.com/delivrance>
#  Copyright (C) 2023-present Hydrogram <https://hydrogram.org>
#
#  This file is part of Hydrogram.
#
#  Hydrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Hydrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Hydrogram.  If not, see <http://www.gnu.org/licenses/>.

from hydrogram import raw

from .auto_name import AutoName


class MessageEntityType(AutoName):
    """Message entity type enumeration used in :obj:`~hydrogram.types.MessageEntity`."""

    @classmethod
    def _missing_(cls, value):
        # ── ljlhubuserbot fork fix (humiuke-main) ──
        # Telegram MTProto schema 还在演进(例如 Layer 224 新增的 MessageEntityFormattedDate
        # 在我们 raw schema 已有的版本里仍可能缺失 enum 映射),如果 raw entity class
        # 不在 enum 值表里,``MessageEntityType(unknown_class)`` 会抛 ``ValueError``,
        # 上层 ``MessageEntity._parse`` 直接崩溃,整批 message 解析中断。
        # 这里通过 ``_missing_`` 钩子把未知 entity class 兜底到 UNKNOWN,
        # 配合 ``MessageEntity._parse`` 把原始 raw 对象挂到 ``_raw_entity`` 属性,
        # 下游想拿具体字段(FORMATTED_DATE 的 ``date`` 等)自行读取。
        return cls.UNKNOWN

    MENTION = raw.types.MessageEntityMention
    "``@username``"

    HASHTAG = raw.types.MessageEntityHashtag
    "``#hashtag``"

    CASHTAG = raw.types.MessageEntityCashtag
    "``$USD``"

    BOT_COMMAND = raw.types.MessageEntityBotCommand
    "``/start@hydrogrambot``"

    URL = raw.types.MessageEntityUrl
    "``https://hydrogram.org`` (see ``url``)"

    EMAIL = raw.types.MessageEntityEmail
    "``do-not-reply@hydrogram.org``"

    PHONE_NUMBER = raw.types.MessageEntityPhone
    "``+1-123-456-7890``"

    BOLD = raw.types.MessageEntityBold
    "Bold text"

    ITALIC = raw.types.MessageEntityItalic
    "Italic text"

    UNDERLINE = raw.types.MessageEntityUnderline
    "Underlined text"

    STRIKETHROUGH = raw.types.MessageEntityStrike
    "Strikethrough text"

    SPOILER = raw.types.MessageEntitySpoiler
    "Spoiler text"

    CODE = raw.types.MessageEntityCode
    "Monowidth string"

    PRE = raw.types.MessageEntityPre
    "Monowidth block (see ``language``)"

    BLOCKQUOTE = raw.types.MessageEntityBlockquote
    "Blockquote text"

    TEXT_LINK = raw.types.MessageEntityTextUrl
    "For clickable text URLs"

    TEXT_MENTION = raw.types.MessageEntityMentionName
    "for users without usernames (see ``user``)"

    BANK_CARD = raw.types.MessageEntityBankCard
    "Bank card text"

    CUSTOM_EMOJI = raw.types.MessageEntityCustomEmoji
    "Custom emoji"

    UNKNOWN = raw.types.MessageEntityUnknown
    "Unknown message entity type"
