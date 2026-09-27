"""
Req2QA - requirement-text PII masking gateway (2026-09-27).

See claude/pii-masking-and-ephemeral-containers-deep-research-2026-09-27.md
(the /deep-research report this implements) and the council code review
folded into it for the full design rationale. Short version: before a
client's requirement/brief/test-case-import text reaches Claude via
Bedrock, structured PII (emails, phone numbers, SSNs, credit-card numbers)
and, when the optional Presidio+spaCy dependency is installed, unstructured
PII (person names, locations, organizations) is replaced with a stable
placeholder token per unique value, so the same real value always maps to
the same token WITHIN one document (this preserves the LLM's ability to
reason about "the same customer mentioned in requirement 3 and requirement
7 is the same test subject" - a fresh random token per occurrence would
silently break that cross-referencing).

SCOPE, DELIBERATELY NARROW (see the deep-research report's "smallest safe
first step"): this module is wired into qa_engine.py's five GENERATION/
IMPORT/MATCH/IMPACT call sites only - i.e. requirement/brief/test-case
document text supplied by the client before a test case is generated or
analyzed. It is explicitly NOT wired into execute_engine.py's get_snapshot
live page-text path. Masking live page/DOM text risks corrupting the exact
evidence (a real validation banner, a real success message) that
wait_for_text and the clause-evidence gate need to render an honest
PASS/BLOCKED verdict - see the deep-research report for the concrete
failure mode this would cause. That is a separate, harder design problem
deliberately deferred, not an oversight.

FAIL-OPEN BY DESIGN (matching this codebase's established pattern for every
optional safety layer - the wait_for_text fuzzy fallback, the transient-
overlay exclusion, the alert-signature fingerprint): if masking itself
errors for any reason, the ORIGINAL, unmasked text is returned unchanged
and the error is logged - never raised out to break test-case generation.
Losing this optional privacy layer for one request is a much smaller
problem than a masking bug taking down QA generation entirely.

TWO DETECTION LAYERS, independently available:
1. Regex layer (always available, zero extra dependencies): structured PII
   with a fixed, recognizable shape - email addresses, phone numbers (US/
   AU-leaning patterns), USA SSNs, credit card numbers. Deliberately does
   NOT attempt to regex-match person names/locations - free-form names have
   no reliable structural signature and a naive name regex (e.g. "two
   capitalized words") would false-positive constantly on ordinary product/
   screen/field names in a requirements document ("Customer Details",
   "Approve Leave"), which is worse than not masking names via regex at all.
2. NER layer (optional, only active if `presidio-analyzer` + a spaCy model
   are installed - see requirements.txt): catches PERSON/LOCATION/ORG
   entities the regex layer cannot. Not installed in this dev sandbox (no
   PyPI network access here - same situation as rapidfuzz/anthropic/boto3
   earlier this session) - REQ2QA_PII_MASKING_ENABLED still defaults to "1"
   and the regex layer alone still runs; the NER layer silently adds itself
   once its dependency is actually installed on a real deploy, with a loud
   one-time startup warning if it's missing there too, exactly like the
   rapidfuzz pattern in execute_engine.py.
"""
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

PII_MASKING_ENABLED = os.environ.get("REQ2QA_PII_MASKING_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

# --------------------------------------------------------------------------
# Optional NER layer (Presidio + spaCy). Loaded lazily and at most once per
# process - constructing an AnalyzerEngine loads a spaCy model, which is not
# free, and every one of the 5 call sites this is wired into would otherwise
# pay that cost per-request.
# --------------------------------------------------------------------------
_ner_lock = threading.Lock()
_ner_analyzer = None          # None until first attempted; False if unavailable
_ner_warned = False


def _get_ner_analyzer():
    global _ner_analyzer, _ner_warned
    if _ner_analyzer is not None:
        return _ner_analyzer or None
    with _ner_lock:
        if _ner_analyzer is not None:
            return _ner_analyzer or None
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            # 2026-09-27 PRODUCTION INCIDENT FIX: a bare AnalyzerEngine()
            # lets Presidio pick its own default spaCy model, which on this
            # deploy turned out to be en_core_web_lg (the LARGE model,
            # ~560MB on disk, considerably more once its full word-vector
            # tables are loaded into memory) - NOT en_core_web_sm (~12MB),
            # which is the only model this project's requirements.txt/deploy
            # notes ever asked for. On req2qa's actual EC2 instance (1.8GB
            # total RAM - confirmed via `free -h`), loading that large model
            # on top of the existing app footprint plus Playwright/Chromium's
            # own memory use during live test execution was enough to get
            # this service OOM-killed in production (systemd/journalctl:
            # "A process of this unit has been killed by the OOM killer",
            # 2026-09-27 06:47 UTC - see
            # claude/pii-masking-oom-incident-fix-2026-09-27.md for the full
            # incident writeup). Explicitly configuring en_core_web_sm here
            # closes this - it is the smallest of spaCy's English models
            # with materially lower memory use, at some cost to NER recall
            # on person/location/org names (an accuracy-for-memory tradeoff
            # this instance's RAM budget requires, not a preference). Fails
            # open exactly like the bare-AnalyzerEngine() path below if
            # en_core_web_sm itself isn't installed (e.g. a fresh deploy
            # that hasn't yet run `python -m spacy download en_core_web_sm`)
            # - regex-only masking still runs regardless.
            nlp_configuration = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
            nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()
            _ner_analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
        except Exception as e:
            _ner_analyzer = False
            if not _ner_warned:
                logger.warning(
                    "PII masking: presidio-analyzer (+ spaCy model) not "
                    "available (%s) - falling back to the regex-only layer "
                    "(emails/phones/SSNs/credit cards). Name/location/org "
                    "masking is disabled until this dependency is installed. "
                    "See requirements.txt for the pinned version.",
                    e,
                )
                _ner_warned = True
        return _ner_analyzer or None


# --------------------------------------------------------------------------
# Regex layer
# --------------------------------------------------------------------------
# Ordered so a more specific pattern is tried before a more general one that
# could otherwise swallow part of it (e.g. SSN before a loose digit-phone
# pattern). Every pattern is deliberately conservative (favors missing an
# edge-case format over flagging ordinary business text as PII) - see the
# module docstring's fail-open rationale: an under-aggressive masker that
# misses some real PII is a privacy gap to close incrementally; an over-
# aggressive one that mangles legitimate requirement text (a business rule
# that happens to look like an ID) actively breaks the product for every
# customer, which is the worse failure mode for a FIRST slice of this
# feature to ship with.
_REGEX_PATTERNS = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # US SSN (###-##-####) - checked before the generic phone pattern below,
    # since without the dash-position anchoring a phone pattern could also
    # match part of one.
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Credit card: 13-19 digits, optionally grouped by spaces/dashes in
    # 4s (the common on-screen/printed grouping). Deliberately requires
    # grouping OR a full contiguous 13-19 digit run, not a bare "any 9+
    # digit number" - that would collide constantly with ordinary IDs,
    # phone numbers, and record numbers in requirement text.
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    # Phone: a conservative US/AU-leaning shape - an optional leading +CC,
    # then 3 groups of digits separated by spaces/dashes/dots/parens,
    # totaling 9-10 local digits. Requires at least one separator between
    # groups (not a bare run of 9-10 digits) to avoid colliding with plain
    # ID/reference numbers that happen to be that long.
    ("PHONE", re.compile(
        r"\b(?:\+?\d{1,3}[ .-]?)?\(?\d{2,4}\)?[ .-]\d{3,4}[ .-]\d{3,4}\b"
    )),
]


def _regex_mask(text: str, token_map: dict, counters: dict) -> str:
    def _replace(match: "re.Match", label: str) -> str:
        value = match.group(0)
        key = (label, value)
        if key not in token_map:
            counters[label] = counters.get(label, 0) + 1
            token_map[key] = f"{{{{TEST_{label}_{counters[label]}}}}}"
        return token_map[key]

    for label, pattern in _REGEX_PATTERNS:
        text = pattern.sub(lambda m, _label=label: _replace(m, _label), text)
    return text


# Presidio entity types worth masking here. Deliberately excludes types
# Presidio also detects via regex-like recognizers that overlap what
# _REGEX_PATTERNS already covers (EMAIL_ADDRESS, PHONE_NUMBER, US_SSN,
# CREDIT_CARD) - running both layers on the same value would double-count
# it under two different token numbers depending on which layer ran first.
_NER_ENTITY_TYPES = ["PERSON", "LOCATION", "ORGANIZATION"]
_NER_LABEL_MAP = {"PERSON": "PERSON", "LOCATION": "LOCATION", "ORGANIZATION": "ORG"}


def _ner_mask(text: str, token_map: dict, counters: dict, analyzer) -> str:
    results = analyzer.analyze(text=text, entities=_NER_ENTITY_TYPES, language="en")
    # Apply back-to-front so earlier replacements don't shift the character
    # offsets of results still pending.
    for result in sorted(results, key=lambda r: r.start, reverse=True):
        value = text[result.start:result.end]
        label = _NER_LABEL_MAP.get(result.entity_type, result.entity_type)
        key = (label, value)
        if key not in token_map:
            counters[label] = counters.get(label, 0) + 1
            token_map[key] = f"{{{{TEST_{label}_{counters[label]}}}}}"
        text = text[:result.start] + token_map[key] + text[result.end:]
    return text


def mask_text(text: str) -> tuple[str, int]:
    """Returns (masked_text, replacements_made). Fails open: on any internal
    error, returns (text, 0) unchanged rather than raising - see the module
    docstring. Safe to call with None/empty text (returns it unchanged)."""
    masked_list, count = mask_texts(text)
    return masked_list[0], count


def mask_texts(*texts: str) -> tuple[list, int]:
    """Like mask_text, but masks several documents together under ONE shared
    token map, so the same real value gets the same token whether it
    appears in the first text or the second (needed by
    analyze_requirements_impact: the same customer/record mentioned in both
    the old and new requirement versions must map to the same placeholder
    in both, or the diff/impact analysis would see what looks like two
    different values that happen to occupy the same position). Returns
    (list_of_masked_texts, total_replacements_made) - same length/order as
    the input. Fails open per-text: if masking errors partway through, every
    text is returned unmasked (never a mix of some masked/some not, which
    would defeat the whole point of a shared map).
    """
    texts = list(texts)
    if not PII_MASKING_ENABLED or not any(texts):
        return texts, 0
    try:
        token_map: dict = {}
        counters: dict = {}
        analyzer = _get_ner_analyzer()
        masked = []
        for text in texts:
            if not text:
                masked.append(text)
                continue
            result = _regex_mask(text, token_map, counters)
            if analyzer is not None:
                try:
                    result = _ner_mask(result, token_map, counters, analyzer)
                except Exception as e:
                    logger.warning("PII masking: NER layer errored, regex-only result kept (%s)", e)
            masked.append(result)
        return masked, len(token_map)
    except Exception as e:
        logger.warning("PII masking: masking step failed, passing text through unmasked (%s)", e)
        return texts, 0
