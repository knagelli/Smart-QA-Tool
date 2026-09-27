"""
Unit tests for the 2026-09-27 PII masking gateway (priority: "definite win
material", per Kalyan) - app/pii_masking.py and its wiring into
app/qa_engine.py's 5 call sites.

Only the regex layer is exercised here: presidio-analyzer/spacy are not
installable in this offline sandbox (no PyPI network access - same
situation as rapidfuzz/anthropic/boto3 stubbed elsewhere this session), so
_get_ner_analyzer() naturally returns None here and pii_masking.py's own
fail-open design (regex-only when the NER dependency is missing) is exactly
what's under test - this is the real, shipped behavior for any deploy that
hasn't yet run `pip install presidio-analyzer presidio-anonymizer spacy`,
not a workaround. No stubbing of pii_masking.py itself is needed or done.

Run with: python3 -m pytest app/test_pii_masking.py -v
(or, with no pytest available: python3 app/test_pii_masking.py)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import pii_masking  # noqa: E402


class TestRegexLayerBasics(unittest.TestCase):
    def test_masks_an_email_address(self):
        masked, count = pii_masking.mask_text("Contact john.smith@example.com for details.")
        self.assertNotIn("john.smith@example.com", masked)
        self.assertIn("{{TEST_EMAIL_1}}", masked)
        self.assertEqual(count, 1)

    def test_masks_a_us_ssn(self):
        masked, count = pii_masking.mask_text("SSN on file: 123-45-6789.")
        self.assertNotIn("123-45-6789", masked)
        self.assertIn("{{TEST_SSN_1}}", masked)
        self.assertEqual(count, 1)

    def test_masks_a_credit_card_number(self):
        masked, count = pii_masking.mask_text("Card number 4111111111111111 was declined.")
        self.assertNotIn("4111111111111111", masked)
        self.assertIn("TEST_CREDIT_CARD_1", masked)

    def test_masks_a_grouped_phone_number(self):
        masked, count = pii_masking.mask_text("Call the customer at 415-555-0199 to confirm.")
        self.assertNotIn("415-555-0199", masked)
        self.assertGreaterEqual(count, 1)

    def test_leaves_ordinary_business_text_untouched(self):
        text = "The Approve Leave button must be disabled until Manager Sign-off is complete."
        masked, count = pii_masking.mask_text(text)
        self.assertEqual(masked, text)
        self.assertEqual(count, 0)

    def test_empty_and_none_text_pass_through(self):
        self.assertEqual(pii_masking.mask_text(""), ("", 0))
        self.assertEqual(pii_masking.mask_text(None), (None, 0))


class TestConsistentTokensWithinADocument(unittest.TestCase):
    """The whole point of placeholder substitution (vs. blanket redaction)
    is that the SAME real value gets the SAME token every time it recurs in
    one document, so the LLM can still tell "this is the same test subject
    mentioned twice" - a fresh token per occurrence would silently break
    that cross-referencing (see pii_masking.py's module docstring)."""

    def test_the_same_email_gets_the_same_token_every_occurrence(self):
        text = (
            "Requirement 3: notify jane.doe@example.com on approval. "
            "Requirement 7: also cc jane.doe@example.com on rejection."
        )
        masked, count = pii_masking.mask_text(text)
        self.assertEqual(count, 1)  # one unique value, not two replacements counted separately
        occurrences = masked.count("{{TEST_EMAIL_1}}")
        self.assertEqual(occurrences, 2)

    def test_two_distinct_emails_get_two_distinct_tokens(self):
        text = "From alice@example.com to bob@example.com"
        masked, count = pii_masking.mask_text(text)
        self.assertEqual(count, 2)
        self.assertIn("{{TEST_EMAIL_1}}", masked)
        self.assertIn("{{TEST_EMAIL_2}}", masked)


class TestMaskTexts(unittest.TestCase):
    """mask_texts() is what analyze_requirements_impact uses: two documents
    masked under ONE shared token map so a value appearing in both gets the
    same token in both (see qa_engine.py's analyze_requirements_impact
    comment for why this matters for a correct MODIFIED-vs-unchanged
    diff)."""

    def test_shared_value_across_two_texts_gets_the_same_token(self):
        old_text = "Owner: maria@example.com approves all requests."
        new_text = "Owner: maria@example.com approves requests over $0."
        (masked_old, masked_new), count = pii_masking.mask_texts(old_text, new_text)
        self.assertEqual(count, 1)
        self.assertIn("{{TEST_EMAIL_1}}", masked_old)
        self.assertIn("{{TEST_EMAIL_1}}", masked_new)

    def test_a_value_only_in_the_second_text_still_gets_a_token(self):
        old_text = "No contact info here."
        new_text = "Contact new-owner@example.com for questions."
        (masked_old, masked_new), count = pii_masking.mask_texts(old_text, new_text)
        self.assertEqual(masked_old, old_text)
        self.assertIn("{{TEST_EMAIL_1}}", masked_new)

    def test_returns_same_length_and_order_as_input(self):
        texts = ["a@example.com", "no pii here", "b@example.com"]
        masked, _ = pii_masking.mask_texts(*texts)
        self.assertEqual(len(masked), 3)
        self.assertIn("{{TEST_EMAIL_1}}", masked[0])
        self.assertEqual(masked[1], "no pii here")
        self.assertIn("{{TEST_EMAIL_2}}", masked[2])


class TestFailOpenAndDisableSwitch(unittest.TestCase):
    def test_disabling_via_env_var_returns_text_unchanged(self):
        os.environ["REQ2QA_PII_MASKING_ENABLED"] = "0"
        try:
            import importlib
            importlib.reload(pii_masking)
            text = "Contact john.smith@example.com"
            masked, count = pii_masking.mask_text(text)
            self.assertEqual(masked, text)
            self.assertEqual(count, 0)
        finally:
            os.environ["REQ2QA_PII_MASKING_ENABLED"] = "1"
            import importlib
            importlib.reload(pii_masking)

    def test_a_regex_layer_exception_fails_open_to_unmasked_text(self):
        text = "Contact john.smith@example.com"
        original_patterns = pii_masking._REGEX_PATTERNS

        class ExplodingPattern:
            def sub(self, *a, **k):
                raise RuntimeError("simulated regex engine failure")

        pii_masking._REGEX_PATTERNS = [("EMAIL", ExplodingPattern())]
        try:
            masked, count = pii_masking.mask_text(text)
            self.assertEqual(masked, text)
            self.assertEqual(count, 0)
        finally:
            pii_masking._REGEX_PATTERNS = original_patterns

    def test_a_broken_ner_analyzer_keeps_the_regex_layers_result(self):
        text = "Contact john.smith@example.com"

        class ExplodingAnalyzer:
            def analyze(self, *a, **k):
                raise RuntimeError("simulated NER engine failure")

        original_get_analyzer = pii_masking._get_ner_analyzer
        pii_masking._get_ner_analyzer = lambda: ExplodingAnalyzer()
        try:
            masked, count = pii_masking.mask_text(text)
            self.assertNotIn("john.smith@example.com", masked)
            self.assertIn("{{TEST_EMAIL_1}}", masked)
        finally:
            pii_masking._get_ner_analyzer = original_get_analyzer


class TestNerEngineExplicitlyPinsSmallModel(unittest.TestCase):
    """2026-09-27 production OOM incident fix (see pii_masking.py's
    _get_ner_analyzer docstring): a bare AnalyzerEngine() let Presidio pick
    its own default model, which turned out to be the large en_core_web_lg
    on the real deploy - loading it OOM-killed the live service on a
    1.8GB-RAM EC2 instance. This test confirms the CODE actually requests
    en_core_web_sm explicitly (by asserting NlpEngineProvider is called
    with that model name), rather than relying on hoping Presidio's
    default never changes again."""

    def test_get_ner_analyzer_requests_en_core_web_sm_explicitly(self):
        import sys
        import types

        captured = {}

        presidio_analyzer_mod = types.ModuleType("presidio_analyzer")
        nlp_engine_mod = types.ModuleType("presidio_analyzer.nlp_engine")

        class _FakeAnalyzerEngine:
            def __init__(self, *a, **k):
                captured["analyzer_kwargs"] = k

        class _FakeNlpEngineProvider:
            def __init__(self, nlp_configuration=None):
                captured["nlp_configuration"] = nlp_configuration

            def create_engine(self):
                return "fake-engine"

        presidio_analyzer_mod.AnalyzerEngine = _FakeAnalyzerEngine
        nlp_engine_mod.NlpEngineProvider = _FakeNlpEngineProvider
        presidio_analyzer_mod.nlp_engine = nlp_engine_mod

        original_modules = {
            k: sys.modules.get(k) for k in ("presidio_analyzer", "presidio_analyzer.nlp_engine")
        }
        sys.modules["presidio_analyzer"] = presidio_analyzer_mod
        sys.modules["presidio_analyzer.nlp_engine"] = nlp_engine_mod
        pii_masking._ner_analyzer = None
        pii_masking._ner_warned = False
        try:
            pii_masking._get_ner_analyzer()
            config = captured.get("nlp_configuration") or {}
            models = config.get("models", [])
            self.assertTrue(
                any(m.get("model_name") == "en_core_web_sm" for m in models),
                f"expected en_core_web_sm explicitly requested, got: {config}",
            )
            self.assertNotIn(
                "en_core_web_lg",
                str(config),
                "must not silently pick the large model that caused the production OOM incident",
            )
        finally:
            for k, v in original_modules.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
            pii_masking._ner_analyzer = None
            pii_masking._ner_warned = False


class TestNerAnalyzerUnavailableInThisSandbox(unittest.TestCase):
    def test_ner_analyzer_gracefully_reports_unavailable(self):
        # presidio-analyzer is not installed here (no PyPI network access) -
        # confirm _get_ner_analyzer() degrades to None rather than raising,
        # which is the real fail-open path this deploy exercises today.
        analyzer = pii_masking._get_ner_analyzer()
        self.assertIsNone(analyzer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
