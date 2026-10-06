"""Orçamento: soma procedimentos pelos preços do tenant e grava um .md.

No sistema real é um PDF. Aqui é Markdown para não exigir dependências.
Demonstra human-in-the-loop: acima do limite do tenant, o orçamento fica
`pending_human_approval` e NÃO é liberado ao cliente sem aprovação.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..textutil import brl, norm
from . import Tool, ToolContext


def _create_quote(args: dict, ctx: ToolContext) -> dict:
    prices = {norm(k): (k, v) for k, v in ctx.tenant.prices.items()}
    items, unknown = [], []
    for name in args["procedures"]:
        hit = prices.get(norm(name))
        (items if hit else unknown).append(
            {"procedure": hit[0], "price": hit[1]} if hit else name
        )
    if unknown:
        return {"error": "unknown_procedure", "message": f"Não tenho preço para: {', '.join(unknown)}."}
    total = round(sum(i["price"] for i in items), 2)
    status = "pending_human_approval" if total > ctx.tenant.approval_threshold else "approved"
    digest = hashlib.sha256(f"{ctx.session_id}|{sorted(i['procedure'] for i in items)}".encode()).hexdigest()
    qid = f"ORC-{digest[:6].upper()}"  # determinístico: mesmo pedido, mesmo id (idempotente)
    ctx.store.add_quote(qid, ctx.session_id, total, status, ctx.now)
    out_dir = Path(os.getenv("QUOTES_DIR", "state/quotes"))
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Orçamento {qid}", f"Empresa: {ctx.tenant.business}", f"Status: {status}", ""]
    lines += [f"- {i['procedure']}: {brl(i['price'])}" for i in items]
    lines += ["", f"**Total: {brl(total)}**"]
    (out_dir / f"{qid}.md").write_text("\n".join(lines), encoding="utf-8")
    if status == "pending_human_approval":
        ctx.trace.event("human_approval_required", quote_id=qid, total=total)
    return {"quote_id": qid, "items": items, "total": total, "status": status}


CREATE_QUOTE = Tool(
    name="create_quote",
    description="Gera um orçamento com os procedimentos informados. Valores altos exigem aprovação humana.",
    parameters={
        "type": "object",
        "properties": {"procedures": {"type": "array"}},
        "required": ["procedures"],
    },
    fn=_create_quote,
)
