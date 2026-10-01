# BOMMYMUSIC/utils/deck_style.py
#
# "Neon Deck" text styling shared by the now-playing UI and the queue UI.
# Pure helpers only (no project / third-party imports) so they are cheap to
# import from anywhere and easy to test.
#
# Glyph vocabulary used across the whole UI:
#   ◉  playing / now        ◌  waiting in queue       ✦  the track you just added
#   ▰▱ progress             ▂▅▇ equalizer             ┃ ┣ ┗ tree lines
import html

BAR_SLOTS = 12
BAR_FILLED, BAR_EMPTY, BAR_KNOB = "▰", "▱", "◉"

# Equalizer frames (5 bars). One frame is picked per progress refresh so the
# header "dances" while music plays and goes flat when paused.
_EQ_FRAMES = ("▂▅▇▅▂", "▃▇▅▂▄", "▅▃▆▇▃", "▇▄▂▅▆", "▄▆▇▃▂", "▂▄▅▇▅")
_EQ_FLAT = "▁▁▁▁▁"

REFRESH_SECONDS = 7  # markup_timer refresh interval


# ── time helpers ─────────────────────────────────────────────────────────────
def to_seconds(value) -> int:
    """'3:45' / '03:45' / '1:02:03' / 225 -> seconds. Unparseable -> 0."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    try:
        total = 0
        for part in str(value).strip().split(":"):
            total = total * 60 + int(part)
        return max(total, 0)
    except (ValueError, TypeError):
        return 0


def fmt_time(seconds) -> str:
    """225 -> '3:45', 3725 -> '1:02:05'."""
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ── now playing ──────────────────────────────────────────────────────────────
def progress_bar(played, dur, slots: int = BAR_SLOTS) -> str:
    """▰▰▰◉▱▱▱▱▱▱▱▱ – filled trail, a knob, then the empty track."""
    total = to_seconds(dur)
    ratio = min(to_seconds(played) / total, 1.0) if total else 0.0
    pos = min(int(ratio * slots), slots - 1)
    return BAR_FILLED * pos + BAR_KNOB + BAR_EMPTY * (slots - pos - 1)


def progress_line(played, dur) -> str:
    """'01:24  ▰▰▰◉▱▱▱▱▱▱▱▱  03:45' (used as a button label)."""
    return f"{played}  {progress_bar(played, dur)}  {dur}"


def eq_tick(played) -> int:
    """Stable animation frame index derived from the played time."""
    return to_seconds(played) // REFRESH_SECONDS


def eq_header(label: str, playing: bool = True, tick: int = 0) -> str:
    """<b>▂▅▇▅▂  ɴᴏᴡ ᴘʟᴀʏɪɴɢ  ▂▅▇▅▂</b>  (flat bars while paused)."""
    bars = _EQ_FRAMES[tick % len(_EQ_FRAMES)] if playing else _EQ_FLAT
    return f"<b>{bars}  {label}  {bars[::-1]}</b>"


# ── queue ────────────────────────────────────────────────────────────────────
def queue_lane(position: int, max_between: int = 5) -> str:
    """Where a freshly queued track sits:  ◉ ─ ◌ ─ ◌ ─ ✦"""
    between = max(int(position) - 1, 0)
    shown = min(between, max_between)
    stops = ["◉"] + ["◌"] * shown
    if between > shown:
        stops.append(f"+{between - shown}")
    stops.append("✦")
    return " ─ ".join(stops)


def eta_seconds(tracks, index: int):
    """Seconds until tracks[index] starts, or None if any length is unknown.

    tracks[0] is the track currently playing (its `played` seconds are
    subtracted); everything between it and `index` is summed.
    """
    if index < 1 or index >= len(tracks):
        return None
    current = tracks[0]
    length = to_seconds(current.get("dur"))
    if not length:
        return None
    wait = max(length - int(current.get("played") or 0), 0)
    for track in tracks[1:index]:
        length = to_seconds(track.get("dur"))
        if not length:
            return None
        wait += length
    return wait


def total_seconds(tracks):
    """(seconds, has_unknown) for a list of queue entries."""
    total, unknown = 0, False
    for track in tracks:
        length = to_seconds(track.get("dur"))
        if length:
            total += length
        else:
            unknown = True
    return total, unknown


def _clip(text: str, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: max(width - 1, 1)].rstrip() + "…"


def queue_rows(tracks, start: int = 1, limit: int = 8, title_width: int = 26, escape=True):
    """['<b>01</b> ┃ Title · 3:45', ...] for the upcoming tracks."""
    rows = []
    for number, track in enumerate(tracks[:limit], start=start):
        title = _clip(track.get("title"), title_width)
        if escape:
            title = html.escape(title)
        rows.append(f"<b>{number:02d}</b> ┃ {title} · {track.get('dur', '?')}")
    return rows


def queue_popup(tracks, header: str, more: str = "+{0}", budget: int = 190) -> str:
    """Plain-text alert body. Telegram caps alert text at 200 chars, so the
    list is trimmed to fit and finished with a '+N' counter."""
    lines = [header]
    used = len(header)
    shown = 0
    for number, track in enumerate(tracks, start=1):
        line = f"{number:02d} ┃ {_clip(track.get('title'), 22)} · {track.get('dur', '?')}"
        remaining = len(tracks) - number
        tail = len(more.format(remaining)) + 1 if remaining else 0
        if used + len(line) + 1 + tail > budget:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < len(tracks):
        lines.append(more.format(len(tracks) - shown))
    return "\n".join(lines)
