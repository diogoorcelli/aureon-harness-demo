"""Garante que a SPEC.md só aponta para evals e testes que existem."""
import json
import re
import unittest
from pathlib import Path

from tests import test_harness

ROOT = Path(__file__).resolve().parent.parent
SPEC = (ROOT / "SPEC.md").read_text(encoding="utf-8")


class SpecTraceability(unittest.TestCase):
    def test_every_eval_reference_exists(self):
        cases = {c["id"] for c in json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))}
        refs = set(re.findall(r"`eval:([a-z_]+)`", SPEC))
        self.assertTrue(refs, "a spec não referencia nenhum eval")
        self.assertEqual(refs - cases, set(), "evals citados na spec que não existem")

    def test_every_test_reference_exists(self):
        refs = set(re.findall(r"`test:(\w+\.\w+)`", SPEC))
        self.assertTrue(refs, "a spec não referencia nenhum teste")
        missing = set()
        for ref in refs:
            cls_name, method = ref.split(".")
            cls = getattr(test_harness, cls_name, None)
            if cls is None or not hasattr(cls, method):
                missing.add(ref)
        self.assertEqual(missing, set(), "testes citados na spec que não existem")

    def test_every_eval_case_is_covered_by_spec(self):
        """O inverso: nenhum eval deve existir sem requisito na spec."""
        cases = {c["id"] for c in json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))}
        refs = set(re.findall(r"`eval:([a-z_]+)`", SPEC))
        self.assertEqual(cases - refs, set(), "evals sem requisito correspondente na spec")

    def test_every_requirement_has_verification(self):
        ids = re.findall(r"^\*\*((?:FR|SEC|REL|OBS|NFR)-\d+)", SPEC, flags=re.M)
        self.assertGreaterEqual(len(ids), 10)
        for rid in ids:
            block = re.search(rf"\*\*{rid}\b.*?(?=\n\*\*(?:FR|SEC|REL|OBS|NFR)-|\n## )", SPEC, flags=re.S).group(0)
            self.assertIn("Verificação:", block, f"{rid} sem linha de verificação")


if __name__ == "__main__":
    unittest.main()
