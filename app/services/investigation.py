"""Web investigation form backend: search Sentry by any of request_id /
device_id / msisdn over a chosen period.

The web analog of /req + /why (first iteration: search only, no LLM verdict).
Every provided identifier is searched (each over its configured key list),
events are merged and deduplicated; for an msisdn the application log lines of
the same window are fetched too — a business-logic rejection often only logs.

The period is either a preset (1h/24h/3d/7d/14d/30d — Sentry statsPeriod,
default 3d) or a custom LOCAL date range (dates only, inclusive), converted
to UTC via TZ_OFFSET_HOURS.
"""
import datetime

from app.config import (
    SENTRY_MSISDN_FIELDS, SENTRY_REQUEST_ID_FIELDS, SENTRY_DEVICE_ID_FIELDS,
    SENTRY_LOGS_DATASET, TZ_OFFSET_HOURS, log,
)

PERIODS = ("1h", "24h", "3d", "7d", "14d", "30d")
DEFAULT_PERIOD = "3d"
EVENT_LIMIT = 100
LOG_LIMIT = 60


class ValidationError(ValueError):
    pass


def _parse_date(value, name):
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValidationError(f"не понял дату «{name}»: {value!r}")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def build_window(period=None, date_from=None, date_to=None):
    """One Discover time range: {'stats_period': …} for a preset, or
    {'start': …, 'end': …} (UTC ISO) for a custom local date range."""
    date_from = (date_from or "").strip()
    date_to = (date_to or "").strip()
    if date_from or date_to:
        if not (date_from and date_to):
            raise ValidationError("укажите обе даты — «с» и «по»")
        start_local = _parse_date(date_from, "с")
        end_local = _parse_date(date_to, "по") + datetime.timedelta(days=1)
        if start_local >= end_local:
            raise ValidationError("дата «с» позже даты «по»")
        offset = datetime.timedelta(hours=TZ_OFFSET_HOURS)
        return {"start": _iso(start_local - offset),
                "end": _iso(end_local - offset)}
    period = (period or "").strip() or DEFAULT_PERIOD
    if period not in PERIODS:
        raise ValidationError(
            f"период {period!r} не поддерживается ({', '.join(PERIODS)})")
    return {"stats_period": period}


async def investigate(sentry, *, description, request_id=None, device_id=None,
                      msisdn=None, period=None, date_from=None, date_to=None,
                      environment=None):
    """Run the search; returns a JSON-ready dict. Raises ValidationError on
    bad input, propagates Sentry API errors to the caller."""
    description = (description or "").strip()
    ids = [("request_id", SENTRY_REQUEST_ID_FIELDS, (request_id or "").strip()),
           ("device_id", SENTRY_DEVICE_ID_FIELDS, (device_id or "").strip()),
           ("msisdn", SENTRY_MSISDN_FIELDS, (msisdn or "").strip())]
    if not description:
        raise ValidationError("описание обязательно")
    if not any(v for _, _, v in ids):
        raise ValidationError(
            "укажите хотя бы одно из: request id, device id, номер (msisdn)")
    window = build_window(period, date_from, date_to)
    envs = [environment.strip()] if (environment or "").strip() else None

    searches, events_by_id, logs = [], {}, []
    for id_type, keys, value in ids:
        if not value:
            continue
        key, events = await sentry.find_events(
            keys, value, limit=EVENT_LIMIT, environments=envs, **window)
        searches.append({"id_type": id_type, "value": value,
                         "matched_field": key, "count": len(events)})
        for ev in events:
            events_by_id.setdefault(str(ev.get("id")), ev)
    m = next((v for t, _, v in ids if t == "msisdn" and v), None)
    if m and SENTRY_LOGS_DATASET:
        try:
            logs = await sentry.logs_for_user(m, limit=LOG_LIMIT,
                                              environments=envs, **window)
        except Exception as e:
            log.warning("web logs search failed msisdn=%s: %s", m, e)

    events = sorted(events_by_id.values(),
                    key=lambda ev: ev.get("timestamp") or "", reverse=True)
    return {
        "description": description,
        "environment": (envs or [None])[0],
        "window": window,
        "tz_offset_hours": TZ_OFFSET_HOURS,
        "searches": searches,
        "count": len(events),
        "events": events,
        "logs": logs,
    }
