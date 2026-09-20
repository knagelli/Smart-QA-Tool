# req2qa Enhancement Analysis: Adding Phase 1 Logic Without Losing the Core Ethos

**Date:** 2026-09-19 · Status: analysis only — no code changes made or proposed for implementation yet

---

## 1. The core finding

I had a research pass dig specifically into whether any existing requirements-to-execution AI QA tool (mabl, testRigor, Katalon, Functionize, Virtuoso QA, ACCELQ, Testsigma, QA Wolf, Rainforest QA, Momentic) already does what Phase 1 of the earlier PRD proposed: having the AI **grade the quality of its own generated test cases** — flagging redundant tests, missing negative/edge-case coverage, or weak assertions — before a human ever sees them.

**None of the ten tools researched do this**, based on actual product docs, help centers, and technical blogs (not just homepages). What exists instead, consistently, is one of three adjacent-but-different things:

1. **Coverage gap detection on the application** — mabl's "Active Coverage" explores the live app and flags untested flows ("no coverage for password reset"). This is about the app, not the quality of the AI's own generated test artifacts.
2. **Requirement-quality scoring on the input** — Katalon's "Requirement Analyzer" scores the ambiguity/testability of the requirement text before generation. This scores the input, not the output.
3. **Execution-time verification** — mabl and testRigor replay a freshly generated test to confirm it runs/passes before saving it. This confirms a test *executes*, not that it's a *good* test.

The near-universal pattern across the market is "AI generates → human reviews and approves" with no automated quality gate in between. That's a real, currently-open gap — not a crowded space to catch up in.

**Caveat, stated plainly:** for Functionize, Virtuoso QA, ACCELQ, Testsigma, Rainforest QA, and Momentic, the honest result is "no evidence found," not a confirmed "no." Their internals may not be publicly documented. Treat this as a strong signal, not proof of a market-wide absence.

## 2. What this means for req2qa's ethos

Your core positioning (confirmed by the earlier qasmith comparison already on file) is: no-code, requirements-in, live-execution-out, no engineers required. That stays exactly as-is — this isn't a pivot, it's an addition at one specific point in the existing flow.

The natural place to add self-evaluation is **between generation and the curation screen** (`review_generated.html`) — the same screen you've already been refining this week. Right now: Claude generates test cases → you see and pick from them. The enhancement: Claude generates test cases → Claude reviews its own output for gaps/redundancy/weak coverage → you see the results *plus* the quality flags, still exactly at the same curation step, so the ethos ("requirements to execution, no extra steps for the user") doesn't change at all.

## 3. Important revision to the original PRD's Phase 1

The original PRD (promptfoo + Langfuse) was scoped for a *standalone* AI-eval platform — useful for testing AI models generally. But for *this specific enhancement to req2qa*, adopting promptfoo as new infrastructure would be overkill and cuts against your explicit "fewer files, no vibe-coding sprawl" preference. Since req2qa already makes a Claude API call to generate test cases (`qa_engine.py`), the self-evaluation capability can be built as **one additional Claude call using a judge-style prompt** — no new dependency, no new service, reusing the existing `ai_client.py` abstraction from the Bedrock migration work. This is smaller, cheaper, and fits entirely inside your current architecture.

Concretely (for your review, not yet built): after `qa_engine.py` generates test cases, a second call asks Claude to review that same batch and flag: test cases that are near-duplicates of each other, requirements with only happy-path coverage and no negative/edge case, and weak/vague expected-result assertions. The curation screen would then show a quality badge or note next to affected test cases, instead of (or alongside) currently showing all cases as equally "confirmed."

**Real cost to flag honestly, since you've raised usage concerns before:** this means one extra AI generation call per run, not zero additional cost. It's a modest, bounded cost (one call, not a per-test-case loop), but it's not free, and I want that in front of you before any build decision, not discovered afterward.

## 4. How this becomes a genuine differentiator, not just a feature

Because no competitor researched does this, the honest, defensible positioning line becomes something like: *req2qa doesn't just generate test cases and execute them — it reviews its own generated coverage before you ever see it, flagging gaps and redundancy automatically.* Per the standing rule already on file for external copy (`process-coverage-insights-final-copy-2026-09-16.md`), this should be described on its own terms only — what it does, plainly — without naming or characterizing competitors, even though the research supporting the internal decision to build it is solid.

## 5. Suggested next step

This is analysis, not a build plan yet. Before any code changes: do you want the self-evaluation step scoped as described above (one extra Claude call, surfaced on the existing curation screen), and should it be always-on or an opt-in toggle given the added cost per run?
