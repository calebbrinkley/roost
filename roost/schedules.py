"""Minute-resolution local schedules, without a cron dependency."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import StoreError


def cron(expression):
    if not isinstance(expression, str) or len(expression) > 200 or len(expression.split()) != 5:
        raise StoreError('Cron needs five numeric fields: minute hour day month weekday.')
    parsed = []
    for field, low, high in zip(expression.split(), (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        values = set()
        for part in field.split(','):
            match = re.fullmatch(r'(\*|\d+(?:-\d+)?)(?:/(\d+))?', part)
            if not match:
                raise StoreError('Invalid cron field.')
            base, step = match.groups()
            step = int(step or 1)
            if not 1 <= step <= high + 1:
                raise StoreError('Invalid cron step.')
            if base == '*':
                start, end = low, high
            elif '-' in base:
                start, end = map(int, base.split('-'))
            else:
                start, end = int(base), high if match[2] else int(base)
            if not low <= start <= end <= high:
                raise StoreError('Cron value is out of range.')
            values.update(range(start, end + 1, step))
        parsed.append(values)
    parsed[4] = {v % 7 for v in parsed[4]}
    return parsed


def zone(name):
    try:
        if not isinstance(name, str) or len(name) > 100:
            raise ValueError
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        raise StoreError('Choose an IANA time zone.') from None


def local_instant(local, tz):
    # fold=0 chooses the first instant in an overlap. Gaps advance to the first valid minute.
    for _ in range(24 * 60 + 1):
        aware = local.replace(tzinfo=tz, fold=0)
        if datetime.fromtimestamp(aware.timestamp(), tz).replace(tzinfo=None) == local:
            return aware.timestamp()
        local = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    raise StoreError('Local time cannot be resolved.')


def validate(schedule):
    if not isinstance(schedule, dict):
        raise StoreError('Schedule must be an object with kind cron, every or once.')
    kind = schedule.get('kind')
    if kind == 'cron' and set(schedule) == {'kind', 'expr'}:
        cron(schedule['expr'])
    elif kind == 'every' and set(schedule) == {'kind', 'value', 'unit'}:
        if type(schedule['value']) is not int or not 1 <= schedule['value'] <= 525600 or schedule['unit'] not in ('minutes', 'hours', 'days'):
            raise StoreError('Every needs a positive integer and minutes, hours or days.')
    elif kind == 'once' and set(schedule) == {'kind', 'at'}:
        try:
            if not isinstance(schedule['at'], str) or len(schedule['at']) > 32:
                raise ValueError
            at = datetime.fromisoformat(schedule['at'])
            if at.tzinfo is not None or 'T' not in schedule['at']:
                raise ValueError
        except ValueError:
            raise StoreError('Once needs an ISO local datetime without an offset.') from None
    else:
        raise StoreError('Invalid schedule fields.')
    return schedule


def occurrence(schedule, time_zone, anchor, instant, *, previous=False):
    """Next occurrence strictly after instant, or latest occurrence at/before it."""
    tz = zone(time_zone)
    kind = schedule['kind']
    if kind == 'once':
        value = local_instant(datetime.fromisoformat(schedule['at']), tz)
        return value if (value <= instant if previous else value > instant) else None
    local = datetime.fromtimestamp(instant, tz).replace(tzinfo=None)
    if kind == 'every':
        seconds = schedule['value'] * {'minutes': 60, 'hours': 3600, 'days': 86400}[schedule['unit']]
        start = datetime.fromtimestamp(anchor, tz).replace(tzinfo=None)
        index = max(1, int((local - start).total_seconds() // seconds))
        # Resolving a gap may put a candidate after instant; a fold may put it before.
        index = max(1, index - 2)
        last = None
        while True:
            value = local_instant(start + timedelta(seconds=index * seconds), tz)
            if value > instant:
                return last if previous else value
            last = value
            index += 1
    minutes, hours, days, months, weekdays = cron(schedule['expr'])
    fields = schedule['expr'].split()
    direction = -1 if previous else 1
    date = local.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(366 * 8):
        day, weekday = date.day in days, (date.weekday() + 1) % 7 in weekdays
        # POSIX-style wildcard semantics include steps: */1 is unrestricted for the day OR rule.
        matches = day and weekday if fields[2].startswith('*') or fields[4].startswith('*') else day or weekday
        if date.month in months and matches:
            values = sorted({local_instant(date.replace(hour=h, minute=m), tz) for h in hours for m in minutes}, reverse=previous)
            for value in values:
                if value <= instant if previous else value > instant:
                    return value
        date += timedelta(days=direction)
    return None
