"""Agenda: consulta de horários livres e reserva (FakeCalendar sobre SQLite).

No sistema real isso é o Google Calendar via service account. A interface das
ferramentas é a mesma; só o backend muda.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from ..textutil import norm
from . import Tool, ToolContext

WEEKDAYS = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"]


def resolve_day(value: str, now: datetime) -> date | None:
    """Aceita hoje | amanha | nome do dia da semana | AAAA-MM-DD."""
    v = norm(value).strip()
    today = now.date()
    if v == "hoje":
        return today
    if v == "amanha":
        return today + timedelta(days=1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    for name in WEEKDAYS:
        if v.startswith(name):
            ahead = (WEEKDAYS.index(name) - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return None


def _all_slots(hours: dict) -> list[str]:
    return [f"{h:02d}:00" for h in range(hours["start"], hours["end"])]


def _starts_at(d: date, slot: str) -> datetime:
    return datetime.combine(d, datetime.strptime(slot, "%H:%M").time())


def _day_error(d: date | None, ctx: ToolContext) -> dict | None:
    """Regras do dia, iguais para consulta e reserva. `ctx.now` é a hora local do tenant."""
    if d is None:
        return {"error": "invalid_day", "message": "Não entendi a data. Pode me dizer o dia, por exemplo 'amanhã' ou 2026-10-08?"}
    if d < ctx.now.date():
        return {"error": "past", "message": f"{d.isoformat()} já passou. Posso ver outro dia?"}
    horizon = ctx.tenant.booking_horizon_days
    if d > ctx.now.date() + timedelta(days=horizon):
        return {"error": "too_far", "message": f"Agendamos com até {horizon} dias de antecedência. Posso ver uma data mais próxima?"}
    if d.weekday() not in ctx.tenant.business_hours["days"]:
        return {"error": "closed", "message": f"Não atendemos em {d.isoformat()} ({WEEKDAYS[d.weekday()]}). Posso ver outro dia?"}
    return None


def _availability(args: dict, ctx: ToolContext) -> dict:
    d = resolve_day(args["day"], ctx.now)
    if (error := _day_error(d, ctx)):
        return error
    taken = ctx.store.booked_times(d.isoformat())
    slots = [
        s for s in _all_slots(ctx.tenant.business_hours)
        if s not in taken and _starts_at(d, s) >= ctx.now  # horário que já passou não é oferecido
    ]
    return {"day": d.isoformat(), "slots": slots}


def _book(args: dict, ctx: ToolContext) -> dict:
    d = resolve_day(args["day"], ctx.now)
    if (error := _day_error(d, ctx)):
        return error
    t = args["time"]
    if t not in _all_slots(ctx.tenant.business_hours):
        return {"error": "invalid_time", "message": f"Horário {t} fora do atendimento. Atendemos de hora em hora, das {ctx.tenant.business_hours['start']:02d}:00 às {ctx.tenant.business_hours['end']:02d}:00."}
    if _starts_at(d, t) < ctx.now:
        return {"error": "past", "message": "Esse horário já passou. Posso ver outro?"}
    status, row = ctx.store.book(ctx.session_id, d.isoformat(), t, args["procedure"], ctx.now)
    if status == "slot_taken":
        return {"error": "slot_taken", "message": f"{t} em {d.isoformat()} já está ocupado. Quer ver os horários livres?"}
    return {"status": status, "day": row["day"], "time": row["time"], "procedure": row["procedure"]}


DAY_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 20}

CHECK_AVAILABILITY = Tool(
    name="check_availability",
    description="Lista horários livres em um dia (hoje, amanha, dia da semana ou AAAA-MM-DD).",
    parameters={"type": "object", "properties": {"day": DAY_SCHEMA}, "required": ["day"]},
    fn=_availability,
)

BOOK_APPOINTMENT = Tool(
    name="book_appointment",
    description="Reserva um horário. Idempotente: repetir o mesmo pedido não duplica a reserva.",
    parameters={
        "type": "object",
        "properties": {
            "day": DAY_SCHEMA,
            "time": {"type": "string", "description": "HH:MM", "pattern": r"^\d{2}:\d{2}$"},
            "procedure": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["day", "time", "procedure"],
    },
    fn=_book,
)
