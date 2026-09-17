"""Web investigation form backend: search Sentry by any of request_id /
device_id / msisdn around an approximate local time.

The web analog of /req + /why (first iteration: search only, no LLM verdict).
Every provided identifier is searched (each over its configured key list),
events are merged and deduplicated; for an msisdn the application log lines of
the same window are fetched too — a business-logic rejection often only logs.
"""
import datetime

from app.config import (
    SENTRY_MSISDN_FIELDS, SENTRY_REQUEST_ID_FIELDS, SENTRY_DEVICE_ID_FIELDS,
    SENTRY_LOGS_DATASET, TZ_OFFSET_HOURS, log,
)

WINDOW_MIN = 45          # ± minutes around the given time
WIDE_WINDOW_MIN = 180    # retry window when the narrow one is empty
DEFAULT_PERIOD = "24h"   # lookback when no time is given
EVENT_LIMIT = 100
LOG_LIMIT = 60


class ValidationError(ValueError):
    pass


def parse_local(value):
    """'YYYY-MM-DDTHH:MM[:SS]' from <input type=datetime-local> -> UTC datetime."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            local = datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
        return local - datetime.timedelta(hours=TZ_OFFSET_HOURS)
    raise ValidationError(f"не понял время: {value!r}")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _windows(center_utc):
    """Time-range attempts, narrow first: [{'start':…,'end':…}, …] in UTC ISO."""
    if center_utc is None:
        return [{"stats_period": DEFAULT_PERIOD}]
    return [{"start": _iso(center_utc - datetime.timedelta(minutes=m)),
             "end": _iso(center_utc + datetime.timedelta(minutes=m))}
            for m in (WINDOW_MIN, WIDE_WINDOW_MIN)]


async def investigate(sentry, *, description, request_id=None, device_id=None,
                      msisdn=None, when_local=None, environment=None):
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
    center_utc = parse_local(when_local.strip()) if (when_local or "").strip() else None
    envs = [environment.strip()] if (environment or "").strip() else None

    searches, events_by_id, logs = [], {}, []
    used = {}
    for window in _windows(center_utc):
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
        used = window
        if events_by_id or logs:
            break

    events = sorted(events_by_id.values(),
                    key=lambda ev: ev.get("timestamp") or "", reverse=True)
    return {
        "description": description,
        "environment": (envs or [None])[0],
        "window": used,
        "tz_offset_hours": TZ_OFFSET_HOURS,
        "searches": searches,
        "count": len(events),
        "events": events,
        "logs": logs,
    }
