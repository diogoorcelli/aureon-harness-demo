"""v0.4: robustez do harness (argumentos, agenda, orçamento, fuso, ID de sessão, privacidade)."""
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from evals.run_evals import FIXED_NOW
from harness.guardrails import validate_args
from harness.llm import LLMError, LLMResponse, MockLLM, OpenRouterLLM, ToolCall
from harness.loop import Agent
from harness.rag import BM25Retriever
from harness.store import Store
from harness.tenant import ROOT, load_tenant
from harness.tools import ToolContext, default_registry
from harness.tracing import Trace


def make_ctx(tenant_slug="demo_clinica", store=None, now=FIXED_NOW, sid="s"):
    tenant = load_tenant(tenant_slug)
    return ToolContext(
        tenant, store or Store(), BM25Retriever.from_dir(tenant.kb_dir), sid, now, Trace(sid, None, "")
    )


def make_agent(llm, tenant_slug="demo_clinica", **kw):
    tenant = load_tenant(tenant_slug)
    store = Store()
    return Agent(tenant, llm, store, BM25Retriever.from_dir(tenant.kb_dir), **kw), store


class OneToolCallThenAnswer:
    """Pede uma ferramenta com os argumentos dados e, quando recebe o resultado, responde."""

    def __init__(self, name, arguments):
        self.name, self.arguments, self.results = name, arguments, []

    def chat(self, model, messages, tools=None):
        if messages[-1]["role"] == "tool":
            self.results.append(json.loads(messages[-1]["content"]))
            return LLMResponse("ok", [], model)
        return LLMResponse("", [ToolCall("c1", self.name, self.arguments)], model)


class FakeHTTPResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def openrouter_returning(body: bytes):
    """OpenRouterLLM sem rede: urlopen devolve `body` e o backoff não dorme."""
    patches = [
        mock.patch("harness.llm.urllib.request.urlopen", return_value=FakeHTTPResponse(body)),
        mock.patch("harness.llm.time.sleep"),
    ]
    for p in patches:
        p.start()
    return OpenRouterLLM(api_key="k", retries=0), patches


class ToolArgs(unittest.TestCase):
    """SEC-04: argumentos validados em profundidade, sem derrubar o turno."""

    def test_non_object_arguments_do_not_crash_turn(self):
        for bad in ("oi", ["x"], 42, None):
            with self.subTest(arguments=bad):
                llm = OneToolCallThenAnswer("search_knowledge", bad)
                agent, _ = make_agent(llm)
                reply = agent.handle("s", "Quanto custa?", FIXED_NOW)
                self.assertEqual(reply.text, "ok")
                self.assertFalse(reply.handoff)
                self.assertEqual(llm.results[0]["error"], "invalid_arguments")

    def test_nested_schema_rules(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string", "maxLength": 5}, "minItems": 1, "maxItems": 2},
                "n": {"type": "integer", "minimum": 1, "maximum": 10},
                "code": {"type": "string", "pattern": r"^[A-Z]{3}$", "minLength": 3},
            },
        }
        self.assertEqual(validate_args(schema, {"tags": ["a"], "n": 5, "code": "ABC"}), [])
        self.assertTrue(validate_args(schema, {"tags": []}))  # minItems
        self.assertTrue(validate_args(schema, {"tags": ["a", "b", "c"]}))  # maxItems
        self.assertTrue(validate_args(schema, {"tags": [1]}))  # items.type
        self.assertTrue(validate_args(schema, {"tags": ["longo demais"]}))  # items.maxLength
        self.assertTrue(validate_args(schema, {"n": 0}))  # minimum
        self.assertTrue(validate_args(schema, {"n": 11}))  # maximum
        self.assertTrue(validate_args(schema, {"code": "abc"}))  # pattern
        self.assertTrue(validate_args(schema, {"code": "ABCD"}))  # pattern com âncoras, como no JSON Schema
        self.assertTrue(validate_args(schema, "não é objeto"))

    def test_tool_inputs_are_bounded(self):
        reg, ctx = default_registry(), make_ctx()
        bad_calls = [
            ("create_quote", {"procedures": [123]}),
            ("create_quote", {"procedures": ["limpeza de pele"] * 11}),
            ("search_knowledge", {"query": "preço", "k": -5}),
            ("search_knowledge", {"query": "preço", "k": 1000}),
            ("search_knowledge", {"query": ""}),
            ("search_knowledge", {"query": "x" * 501}),
            ("book_appointment", {"day": "amanha", "time": "10h", "procedure": "limpeza de pele"}),
            ("check_availability", {"day": "x" * 50}),
        ]
        for name, args in bad_calls:
            with self.subTest(tool=name, args=str(args)[:60]):
                self.assertEqual(reg.call(name, args, ctx).get("error"), "invalid_arguments")


class LLMClient(unittest.TestCase):
    """REL-03 e SEC-04 no cliente OpenRouter (sem rede)."""

    def test_malformed_response_raises_llm_error(self):
        for body in (b'{"choices": []}', b'{"choices": [{}]}', b"[]", b'{"choices": [{"message": "x"}]}', b"nao e json"):
            with self.subTest(body=body):
                llm, patches = openrouter_returning(body)
                try:
                    with self.assertRaises(LLMError):
                        llm.chat("m", [{"role": "user", "content": "oi"}])
                finally:
                    for p in patches:
                        p.stop()

    def test_invalid_json_arguments_are_not_silenced(self):
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [{"id": "c1", "function": {"name": "search_knowledge", "arguments": "{quebrado"}}],
                        }
                    }
                ]
            }
        ).encode()
        llm, patches = openrouter_returning(body)
        try:
            call = llm.chat("m", [{"role": "user", "content": "oi"}]).tool_calls[0]
        finally:
            for p in patches:
                p.stop()
        self.assertNotEqual(call.arguments, {})
        result = default_registry().call(call.name, call.arguments, make_ctx())
        self.assertEqual(result["error"], "invalid_arguments")


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class CompetitorAfterFirstRead:
    """Conexão que deixa um concorrente gravar logo depois da primeira leitura da agenda.

    Reproduz a corrida checar-e-inserir de forma determinística: se a reserva consultar
    antes de inserir, o concorrente ocupa o horário no meio do caminho.
    """

    def __init__(self, conn, competitor):
        self.conn, self.competitor, self.fired = conn, competitor, False

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        if not self.fired and sql.lstrip().upper().startswith("SELECT") and "appointments" in sql:
            rows = cur.fetchall()
            self.fired = True
            self.competitor()
            return _Rows(rows)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


class Booking(unittest.TestCase):
    """FR-04: agenda sem corrida, sem horário passado e dentro do horizonte."""

    def test_second_connection_gets_slot_taken(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = Path(tmp) / "agenda.db"
        mine, other = Store(db), Store(db)
        self.addCleanup(other.close)
        self.addCleanup(mine.close)  # cleanups rodam em ordem inversa: fecha antes de apagar
        statuses = []

        def competitor():
            statuses.append(other.book("outra-sessao", "2026-10-06", "10:00", "peeling químico", FIXED_NOW)[0])

        mine.db = CompetitorAfterFirstRead(mine.db, competitor)
        ctx = make_ctx(store=mine)
        result = default_registry().call(
            "book_appointment", {"day": "2026-10-06", "time": "10:00", "procedure": "limpeza de pele"}, ctx
        )
        statuses.append(result.get("status") or result.get("error"))
        if not mine.db.fired:
            competitor()
        self.assertEqual(sorted(statuses), ["confirmed", "slot_taken"])
        self.assertEqual(other.count_appointments(), 1)
        owner = other.db.execute("SELECT session_id FROM appointments").fetchone()["session_id"]
        again = other.book("outra-sessao", "2026-10-06", "10:00", "peeling químico", FIXED_NOW)[0]
        self.assertEqual(again, "already_booked" if owner == "outra-sessao" else "slot_taken")

    def test_past_slots_are_hidden_today(self):
        ctx = make_ctx(now=datetime(2026, 10, 5, 15, 30))
        slots = default_registry().call("check_availability", {"day": "hoje"}, ctx)["slots"]
        self.assertNotIn("09:00", slots)
        self.assertNotIn("15:00", slots)
        self.assertEqual(slots, ["16:00", "17:00"])

    def test_booking_beyond_horizon_is_refused(self):
        ctx = make_ctx()
        far = FIXED_NOW.date() + timedelta(days=ctx.tenant.booking_horizon_days + 1)
        if far.weekday() not in ctx.tenant.business_hours["days"]:
            far += timedelta(days=1)
        result = default_registry().call(
            "book_appointment", {"day": far.isoformat(), "time": "10:00", "procedure": "limpeza de pele"}, ctx
        )
        self.assertEqual(result.get("error"), "too_far")
        self.assertEqual(ctx.store.count_appointments(), 0)


class QuoteRules(unittest.TestCase):
    """FR-05: orçamento válido, idempotente e sem gravação parcial."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"QUOTES_DIR": self.tmp})
        self.env.start()
        self.reg, self.ctx = default_registry(), make_ctx()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self):
        return self.ctx.store.db.execute("SELECT COUNT(*) c FROM quotes").fetchone()["c"]

    def _files(self):
        return [p for p in Path(self.tmp).rglob("*") if p.is_file()]

    def test_empty_quote_is_refused(self):
        self.assertIn("error", self.reg.call("create_quote", {"procedures": []}, self.ctx))
        tool_fn = self.reg._tools["create_quote"].fn  # defesa em profundidade: a ferramenta também recusa
        self.assertIn("error", tool_fn({"procedures": []}, self.ctx))
        self.assertEqual(self._rows(), 0)
        self.assertEqual(self._files(), [])

    def test_repeated_item_counts_as_quantity(self):
        result = self.reg.call("create_quote", {"procedures": ["microagulhamento"] * 3}, self.ctx)
        self.assertEqual(result["total"], 1260.0)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["quantity"], 3)

    def test_failed_file_write_leaves_no_row(self):
        with mock.patch.object(Path, "write_text", side_effect=OSError("disco cheio")):
            result = self.reg.call("create_quote", {"procedures": ["pacote noivas"]}, self.ctx)
        self.assertEqual(result.get("error"), "tool_failed")
        self.assertEqual(self._rows(), 0)
        self.assertEqual(self._files(), [])
        self.assertNotIn("human_approval_required", self.ctx.trace.kinds())

    def test_failed_db_write_leaves_no_file(self):
        with mock.patch.object(self.ctx.store, "add_quote", side_effect=sqlite3.OperationalError("locked")):
            result = self.reg.call("create_quote", {"procedures": ["pacote noivas"]}, self.ctx)
        self.assertEqual(result.get("error"), "tool_failed")
        self.assertEqual(self._files(), [])
        self.assertNotIn("human_approval_required", self.ctx.trace.kinds())

    def test_same_id_from_other_session_does_not_overwrite(self):
        store = self.ctx.store
        store.add_quote("ORC-0000000000", "sessao-a", 100.0, "approved", FIXED_NOW)
        store.add_quote("ORC-0000000000", "sessao-b", 999.0, "approved", FIXED_NOW)
        row = store.db.execute("SELECT session_id, total FROM quotes WHERE id='ORC-0000000000'").fetchone()
        self.assertEqual((row["session_id"], row["total"]), ("sessao-a", 100.0))


class TimePolicy(unittest.TestCase):
    """FR-13: o relógio é o do tenant, não o do servidor."""

    def test_default_clock_is_tenant_local(self):
        agent, store = make_agent(MockLLM())
        expected = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
        self.assertLess(abs(agent.tenant.now() - expected), timedelta(seconds=5))
        agent.handle("s", "oi")
        recorded = datetime.fromisoformat(store.get_lead("s")["last_user_ts"])
        self.assertLess(abs(recorded - expected), timedelta(seconds=5))

    def test_aware_now_is_converted(self):
        agent, _ = make_agent(MockLLM())
        tuesday_1am_utc = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)  # segunda 22:00 em UTC-3
        reply = agent.handle("s", "Quero ver horario amanha", tuesday_1am_utc)
        self.assertIn("2026-10-06", reply.text)

    def test_invalid_timezone_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = json.loads((ROOT / "tenants" / "demo_clinica" / "config.json").read_text(encoding="utf-8"))
            for bad in ("Brasília", "-3", "+25:00"):
                with self.subTest(timezone=bad):
                    (Path(tmp) / "x").mkdir(exist_ok=True)
                    cfg["timezone"] = bad
                    (Path(tmp) / "x" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_tenant("x", root=Path(tmp))


class SessionIds(unittest.TestCase):
    """SEC-05: o ID de sessão nunca vira caminho de arquivo."""

    def test_trace_path_stays_inside_dir(self):
        ids = ["../fora", "wa:5547999990000", "a/b\\c", "sessão-ção"]
        with tempfile.TemporaryDirectory() as tmp:
            traces = Path(tmp) / "traces"
            agent, _ = make_agent(MockLLM(), trace_dir=traces)
            for sid in ids:
                agent.handle(sid, "oi", FIXED_NOW)
            everything = [p for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertEqual(len(everything), len(ids))
            for path in everything:
                self.assertEqual(path.parent, traces)
                self.assertRegex(path.name, r"^[0-9a-f]{16,}\.jsonl$")

    def test_invalid_session_id_is_rejected(self):
        agent, _ = make_agent(MockLLM())
        for bad in ("", "   ", "x" * 201, None, 123):
            with self.subTest(session_id=bad):
                with self.assertRaises(ValueError):
                    agent.handle(bad, "oi", FIXED_NOW)


class Privacy(unittest.TestCase):
    """OBS-02: dados pessoais não vão para o trace."""

    SECRETS = ["123.456.789-00", "ana@exemplo.com", "99999-0000", "5547999990000", "12.345.678/0001-90"]

    def test_trace_masks_personal_data(self):
        text = (
            "Meu nome é Ana, CPF 123.456.789-00, email ana@exemplo.com, fone (47) 99999-0000, "
            "CNPJ 12.345.678/0001-90. Quanto custa a limpeza de pele?"
        )
        with tempfile.TemporaryDirectory() as tmp:
            agent, _ = make_agent(MockLLM(), trace_dir=Path(tmp))
            reply = agent.handle("wa:5547999990000", text, FIXED_NOW)
            files = list(Path(tmp).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
        for secret in self.SECRETS:
            self.assertNotIn(secret, content)
        self.assertIn("180", reply.text)
        self.assertIn("[CPF]", content)
        self.assertIn("[EMAIL]", content)

    def test_redaction_keeps_prices_dates_and_times(self):
        from harness.privacy import redact

        keep = "Total R$ 1.200,00 em 2026-10-08 às 14:00, orçamento ORC-4712345678, total 4000.0"
        self.assertEqual(redact(keep), keep)
        masked = redact(
            "cpf 12345678900, tel +55 47 99999-0000, fixo (47) 3333-4444, cel 47999990000, "
            "cnpj 12345678000190, mail a.b@c.com.br"
        )
        for raw in ("12345678900", "99999-0000", "3333-4444", "47999990000", "12345678000190", "a.b@c.com.br"):
            self.assertNotIn(raw, masked)


if __name__ == "__main__":
    unittest.main()
