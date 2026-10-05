"""Canal simulado de webhook, no formato do WhatsApp Cloud API (sem Meta, sem rede).

Mostra que o canal é plugável: o adapter valida a assinatura HMAC-SHA256,
deduplica o evento, extrai (remetente, texto) e entrega ao mesmo Agent do CLI.

    python -m adapters.mock_webhook --text "Quanto custa a limpeza de pele?" --replay
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import uuid

from adapters.common import build_agent


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, header: str, secret: str) -> bool:
    return hmac.compare_digest(sign(body, secret), header or "")


def build_payload(sender: str, text: str, msg_id: str | None = None) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": msg_id or f"wamid.{uuid.uuid4().hex[:12]}", "from": sender, "type": "text", "text": {"body": text}}
                            ]
                        }
                    }
                ]
            }
        ]
    }


def extract_message(payload: dict) -> tuple[str, str, str] | None:
    try:
        msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        if msg.get("type") != "text":
            return None
        return msg["id"], msg["from"], msg["text"]["body"]
    except (KeyError, IndexError, TypeError):
        return None


def handle_webhook(agent, store, body: bytes, signature: str, secret: str) -> dict:
    if not verify(body, signature, secret):
        return {"status": 401, "detail": "assinatura inválida"}
    parsed = extract_message(json.loads(body))
    if parsed is None:
        return {"status": 200, "detail": "evento ignorado (não é texto)"}
    event_id, sender, text = parsed
    if not store.mark_event_processed(event_id):
        return {"status": 200, "detail": "evento duplicado ignorado"}
    reply = agent.handle(f"wa:{sender}", text)
    return {"status": 200, "reply": reply.text, "temperature": reply.temperature}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--sender", default="5547999990000")
    ap.add_argument("--secret", default="segredo-de-demo")
    ap.add_argument("--replay", action="store_true", help="reenvia o mesmo evento (testa dedup)")
    ap.add_argument("--bad-signature", action="store_true")
    args = ap.parse_args()

    agent, store, _ = build_agent()
    body = json.dumps(build_payload(args.sender, args.text, "wamid.demo-1"), ensure_ascii=False).encode()
    sig = "sha256=invalida" if args.bad_signature else sign(body, args.secret)
    for i in range(2 if args.replay else 1):
        print(f"entrega {i + 1}:", json.dumps(handle_webhook(agent, store, body, sig, args.secret), ensure_ascii=False))


if __name__ == "__main__":
    main()
