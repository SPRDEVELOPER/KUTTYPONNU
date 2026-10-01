import os
import random
import re

from pyrogram import enums, errors, types

from BOMMYMUSIC.misc import db
from BOMMYMUSIC.utils import deck_style
from BOMMYMUSIC.utils.cards import now_playing_card, queue_added_card
from BOMMYMUSIC.utils.database import get_lang
from BOMMYMUSIC.utils.formatters import seconds_to_min
from strings import get_string

_TAG_RE = re.compile(r"<(/?)(b|a)(?:\s+href=([^>]+))?>", re.IGNORECASE)

_consumed = set()

_FORBIDDEN = (errors.ChatSendPhotosForbidden, errors.ChatSendMediaForbidden)


async def _lang(chat_id):
    return get_string(await get_lang(chat_id))


def _parse_inline(segment):
    parts = []
    stack = []
    pos = 0

    for m in _TAG_RE.finditer(segment):
        if m.start() > pos:
            parts.append(segment[pos : m.start()])
        pos = m.end()

        closing, tag, href = m.group(1), m.group(2).lower(), m.group(3)

        if not closing:
            stack.append((tag, href.strip("\"'") if href else None, len(parts)))
        elif stack and stack[-1][0] == tag:
            open_tag, url, start = stack.pop()
            inner = parts[start:]
            del parts[start:]
            inner = inner[0] if len(inner) == 1 else inner if inner else ""
            if open_tag == "b":
                parts.append(types.RichTextBold(text=inner))
            else:
                parts.append(types.RichTextUrl(text=inner, url=url))

    if pos < len(segment):
        parts.append(segment[pos:])

    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else parts


def _balance_lines(caption_html):
    lines = []
    carry = False
    for line in caption_html.split("\n"):
        if not line:
            lines.append(line)
            continue
        if carry:
            line = "<b>" + line
        opened = len(re.findall(r"<b>", line, re.IGNORECASE))
        closed = len(re.findall(r"</b>", line, re.IGNORECASE))
        carry = opened > closed
        if carry:
            line += "</b>"
        lines.append(line)
    return lines


def _html_caption_to_blocks(caption_html):
    return [
        types.InputRichBlockParagraph(text=_parse_inline(line))
        for line in _balance_lines(caption_html)
    ]


def _progress_line(played, dur):
    # 01:24  ▰▰▰◉▱▱▱▱▱▱▱▱  03:45
    return deck_style.progress_line(played, dur)


_BUTTON_STYLES = [
    enums.ButtonStyle.DEFAULT,
    enums.ButtonStyle.PRIMARY,
    enums.ButtonStyle.SUCCESS,
    enums.ButtonStyle.DANGER,
]


# False -> a steady palette: Pause is blue while playing and Resume turns green
# while paused. True -> the old behaviour (colours reshuffled on every refresh).
RANDOM_BUTTON_STYLES = False


def _random_styles():
    styles = list(_BUTTON_STYLES)
    styles.append(random.choice(_BUTTON_STYLES))
    random.shuffle(styles)
    return styles


def _button_styles(playing):
    """[replay, pause/resume, skip, queue, progress]"""
    if RANDOM_BUTTON_STYLES:
        return _random_styles()
    default = enums.ButtonStyle.DEFAULT
    toggle = enums.ButtonStyle.PRIMARY if playing else enums.ButtonStyle.SUCCESS
    return [default, toggle, default, default, default]


def _progress_row(played, dur, style):
    return types.InputRichBlockButtons(
        buttons=[
            types.RichMessageButton(
                text=_progress_line(played, dur),
                style=style,
                callback_data="GetTimer",
            )
        ]
    )


def _queue_len(chat_id):
    tracks = db.get(chat_id)
    return max(len(tracks) - 1, 0) if tracks else 0


def _control_rows(_, chat_id, playing, styles):
    replay_style, toggle_style, skip_style, queue_style = styles
    toggle = (
        types.RichMessageButton(
            text=_["RICH_BTN_PAUSE"],
            style=toggle_style,
            callback_data=f"ADMIN Pause|{chat_id}",
        )
        if playing
        else types.RichMessageButton(
            text=_["RICH_BTN_RESUME"],
            style=toggle_style,
            callback_data=f"ADMIN Resume|{chat_id}",
        )
    )
    return [
        types.InputRichBlockButtons(
            buttons=[
                types.RichMessageButton(
                    text=_["RICH_BTN_REPLAY"],
                    style=replay_style,
                    callback_data=f"ADMIN Replay|{chat_id}",
                ),
                toggle,
                types.RichMessageButton(
                    text=_["RICH_BTN_SKIP"],
                    style=skip_style,
                    callback_data=f"ADMIN Skip|{chat_id}",
                ),
            ]
        ),
        types.InputRichBlockButtons(
            buttons=[
                types.RichMessageButton(
                    text=_["RICH_BTN_QUEUE"].format(_queue_len(chat_id)),
                    style=queue_style,
                    callback_data=f"nowplaying_queue {chat_id}",
                ),
            ]
        ),
    ]


def _eq_header(_, playing, played):
    # ▂▅▇▅▂  ɴᴏᴡ ᴘʟᴀʏɪɴɢ  ▂▅▇▅▂ (dances with every refresh, flat while paused)
    if playing:
        label = _.get("NP_PLAYING", "ɴᴏᴡ ᴘʟᴀʏɪɴɢ")
    else:
        label = _.get("NP_PAUSED", "ᴘᴀᴜsᴇᴅ")
    return deck_style.eq_header(label, playing, deck_style.eq_tick(played))


def build_now_playing_blocks(
    _, photo, caption_html, chat_id, played=None, dur=None, playing=True
):
    blocks = [types.InputRichBlockPhoto(photo=types.InputMediaPhoto(photo))]
    blocks += _html_caption_to_blocks(_eq_header(_, playing, played))
    blocks += _html_caption_to_blocks(caption_html)
    styles = _button_styles(playing)
    if played and dur:
        blocks.append(_progress_row(played, dur, styles[4]))
    blocks += _control_rows(_, chat_id, playing, styles[:4])
    return blocks


def _message_key(message):
    return (message.chat.id, message.id)


def _strip_photo(blocks):
    return [b for b in blocks if not isinstance(b, types.InputRichBlockPhoto)]


async def _edit_rich(message, blocks):
    try:
        return await message.edit_text(
            rich_message=types.InputRichMessage(blocks=blocks)
        )
    except _FORBIDDEN:
        plain = _strip_photo(blocks)
        if len(plain) == len(blocks):
            raise
        return await message.edit_text(
            rich_message=types.InputRichMessage(blocks=plain)
        )


async def _try_deliver(client, target_chat_id, blocks, replace):
    rich = types.InputRichMessage(blocks=blocks)
    if replace is not None:
        try:
            edited = await replace.edit_text(rich_message=rich)
        except _FORBIDDEN:
            raise
        except Exception:
            try:
                await replace.delete()
            except Exception:
                pass
        else:
            _consumed.add(_message_key(replace))
            return edited or replace
    return await client.send_rich_message(target_chat_id, rich_message=rich)


async def _deliver(client, target_chat_id, blocks, replace=None):
    try:
        return await _try_deliver(client, target_chat_id, blocks, replace)
    except _FORBIDDEN:
        plain = _strip_photo(blocks)
        if len(plain) == len(blocks):
            raise
        return await _try_deliver(client, target_chat_id, plain, replace)


def caption_blocks(caption_html):
    return _html_caption_to_blocks(caption_html)


async def edit_rich(message, blocks):
    return await _edit_rich(message, blocks)


async def deliver_rich(client, target_chat_id, blocks, replace=None):
    result = await _deliver(client, target_chat_id, blocks, replace)
    if replace is not None:
        _consumed.discard(_message_key(replace))
    return result


async def release_mystic(mystic):
    if mystic is None:
        return
    key = _message_key(mystic)
    if key in _consumed:
        _consumed.discard(key)
        return
    try:
        await mystic.delete()
    except Exception:
        pass


def _track_for(chat_id, photo):
    """Queue entry that `photo` (cache/<videoid>.jpg) belongs to."""
    tracks = db.get(chat_id) or []
    vidid = os.path.splitext(os.path.basename(str(photo)))[0]
    for track in tracks:
        if str(track.get("vidid")) == vidid:
            return track
    return None


async def _styled_photo(chat_id, photo):
    """Neon Vinyl card for the playing track; the plain thumbnail on any problem."""
    track = _track_for(chat_id, photo)
    if not track:
        return photo
    inuse = {t[0].get("np_photo") for t in db.values() if t}
    return await now_playing_card(photo, track, protect=inuse)


async def send_now_playing_rich(
    client, chat_id, target_chat_id, photo, caption_html, replace=None
):
    _ = await _lang(chat_id)
    photo = await _styled_photo(chat_id, photo)
    blocks = build_now_playing_blocks(_, photo, caption_html, chat_id)
    msg = await _deliver(client, target_chat_id, blocks, replace)
    if db.get(chat_id):
        db[chat_id][0]["np_photo"] = photo
        db[chat_id][0]["np_caption"] = caption_html
    return msg


def _queue_extras(_, chat_id, qid):
    """◉ ─ ◌ ─ ✦ lane plus 'plays in ~m:ss' for the track that was just added."""
    tracks = db.get(chat_id) or []
    index = next((i for i, t in enumerate(tracks) if i > 0 and t.get("qid") == qid), None)
    if index is None:
        return []
    line = deck_style.queue_lane(index)
    wait = deck_style.eta_seconds(tracks, index)
    if wait is not None:
        line += "  ·  " + _.get("RICH_ETA", "⏳ ~{0}").format(deck_style.fmt_time(wait))
    return _html_caption_to_blocks(line)


def build_queue_blocks(_, caption_html, chat_id, qid, photo=None):
    blocks = []
    if photo:  # the ticket image already shows lane + ETA
        blocks.append(types.InputRichBlockPhoto(photo=types.InputMediaPhoto(photo)))
    blocks += _html_caption_to_blocks(caption_html)
    if not photo:
        blocks += _queue_extras(_, chat_id, qid)
    default = enums.ButtonStyle.DEFAULT
    blocks.append(
        types.InputRichBlockButtons(
            buttons=[
                types.RichMessageButton(
                    text=_["RICH_BTN_PLAYNOW"],
                    style=default,
                    callback_data=f"ADMIN PlayNow|{chat_id}_{qid}",
                ),
                types.RichMessageButton(
                    text=_["RICH_BTN_SKIP"],
                    style=default,
                    callback_data=f"ADMIN Skip|{chat_id}",
                ),
                types.RichMessageButton(
                    text=_["RICH_BTN_END"],
                    style=default,
                    callback_data=f"ADMIN Stop|{chat_id}",
                ),
            ]
        )
    )
    return blocks


async def send_queue_rich(
    client, chat_id, target_chat_id, caption_html, qid, replace=None
):
    _ = await _lang(chat_id)
    photo = None
    tracks = db.get(chat_id) or []
    index = next((i for i, t in enumerate(tracks) if i > 0 and t.get("qid") == qid), None)
    if index is not None:
        photo = await queue_added_card(tracks[index], index, deck_style.eta_seconds(tracks, index))
    blocks = build_queue_blocks(_, caption_html, chat_id, qid, photo)
    return await _deliver(client, target_chat_id, blocks, replace)


async def update_now_playing_progress(mystic, chat_id, played, dur, playing=True):
    info = db.get(chat_id)
    if not info:
        return None
    photo = info[0].get("np_photo")
    caption_html = info[0].get("np_caption")
    if not photo or not caption_html:
        return None
    _ = await _lang(chat_id)
    blocks = build_now_playing_blocks(_, photo, caption_html, chat_id, played, dur, playing)
    return await _edit_rich(mystic, blocks)


async def set_now_playing_state(chat_id, playing):
    info = db.get(chat_id)
    if not info:
        return None
    mystic = info[0].get("mystic")
    photo = info[0].get("np_photo")
    caption_html = info[0].get("np_caption")
    if not mystic or not photo or not caption_html:
        return None
    played = seconds_to_min(info[0].get("played", 0)) or None
    dur = info[0].get("dur")
    _ = await _lang(chat_id)
    blocks = build_now_playing_blocks(_, photo, caption_html, chat_id, played, dur, playing)
    try:
        return await _edit_rich(mystic, blocks)
    except Exception:
        return None
