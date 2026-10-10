"""Orçamento: soma procedimentos pelos preços do tenant e grava um .md.

No sistema real é um PDF. Aqui é Markdown para não exigir dependências.
Demonstra human-in-the-loop: acima do limite do tenant, o orçamento fica
`pending_human_approval` e NÃO é liberado ao cliente sem aprovação.

Gravação sem estado parcial: o arquivo vai primeiro para um temporário, depois
o banco, e só então o temporário assume o nome final. Qualquer falha desfaz o
que já foi feito, e o evento de aprovação só sai quando os dois foram gravados.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from ..textutil import brl, norm
from . import Tool, ToolContext

MAX_ITEMS = 10


def _create_quote(args: dict, ctx: ToolContext) -> dict:
    requested = args.get("procedures") or []
    if not requested:  # o schema já exige 1 item; a ferramenta não depende disso
        return {"error": "empty_quote", "message": "Me diga ao menos um procedimento para montar o orçamento."}
    if len(requested) > MAX_ITEMS:
        return {"error": "too_many_items", "message": f"Consigo orçar até {MAX_ITEMS} itens por vez."}
    prices = {norm(k): (k, v) for k, v in ctx.tenant.prices.items()}
    quantities: dict[str, int] = {}
    unknown = []
    for name in requested:
        hit = prices.get(norm(name))
        if hit:
            quantities[hit[0]] = quantities.get(hit[0], 0) + 1  # item repetido = quantidade
        else:
            unknown.append(name)
    if unknown:
        return {"error": "unknown_procedure", "message": f"Não tenho preço para: {', '.join(unknown)}."}
    items = []
    for proc, qty in quantities.items():
        price = ctx.tenant.prices[proc]
        items.append({"procedure": proc, "price": price, "quantity": qty, "subtotal": round(price * qty, 2)})
    total = round(sum(i["subtotal"] for i in items), 2)
    if total <= 0:
        return {"error": "invalid_total", "message": "Não consegui calcular um valor válido para esse orçamento."}
    status = "pending_human_approval" if total > ctx.tenant.approval_threshold else "approved"
    key = "|".join(f"{p}x{q}" for p, q in sorted(quantities.items()))
    digest = hashlib.sha256(f"{ctx.tenant.slug}|{ctx.session_id}|{key}".encode()).hexdigest()
    qid = f"ORC-{digest[:10].upper()}"  # determinístico: mesmo pedido, mesmo id (idempotente)

    out_dir = Path(os.getenv("QUOTES_DIR", "state/quotes")) / ctx.tenant.slug
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Orçamento {qid}", f"Empresa: {ctx.tenant.business}", f"Status: {status}", ""]
    lines += [f"- {i['quantity']}x {i['procedure']}: {brl(i['subtotal'])}" for i in items]
    lines += ["", f"**Total: {brl(total)}**"]
    tmp = out_dir / f".{qid}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text("\n".join(lines), encoding="utf-8")
        if not ctx.store.add_quote(qid, ctx.session_id, total, status, ctx.now):
            return {"error": "quote_id_conflict", "message": "Não consegui registrar o orçamento agora; vou chamar a equipe."}
        try:
            os.replace(tmp, out_dir / f"{qid}.md")
        except OSError:
            ctx.store.delete_quote(qid, ctx.session_id)
            raise
    finally:
        tmp.unlink(missing_ok=True)
    if status == "pending_human_approval":
        ctx.trace.event("human_approval_required", quote_id=qid, total=total)
    return {"quote_id": qid, "items": items, "total": total, "status": status}


CREATE_QUOTE = Tool(
    name="create_quote",
    description="Gera um orçamento com os procedimentos informados (repita um item para mais de uma sessão). Valores altos exigem aprovação humana.",
    parameters={
        "type": "object",
        "properties": {
            "procedures": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 100},
                "minItems": 1,
                "maxItems": MAX_ITEMS,
            }
        },
        "required": ["procedures"],
    },
    fn=_create_quote,
)
