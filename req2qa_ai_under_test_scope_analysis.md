# req2qa Scope Clarification: Testing AI-Powered Applications

**Date:** 2026-09-19 · Status: analysis only, no code changes

---

## 1. What this clarifies

req2qa's intake needs to branch based on what kind of system a client's requirements describe:

- **Traditional SaaS/custom app** (today's behavior, unchanged): conventional functional test cases — UI flows, field validation, CRUD, negative paths — generated and executed exactly as req2qa does now.
- **AI-powered application** (new): the requirements describe a system with an AI/LLM component (a chatbot, a recommendation engine, an AI feature embedded in a larger app), and test cases need to additionally cover the diagram's dimensions — Accuracy (semantics/schema correctness), Safety (policy/bias), Consistency (variance across repeated runs), Performance (latency/cost), and Adversarial (red-teaming/jailbreak).

This is squarely where the Phase 1 self-evaluation work (from the earlier enhancement analysis) becomes directly useful — it's the same underlying capability (an LLM-as-judge reviewing AI-generated or AI-produced content against criteria), just aimed at the *client's* AI system instead of req2qa's own generated test cases.

## 2. The one architectural fork this creates, and why it matters

I checked `execute_engine.py` (req2qa's live-execution engine) to see what changes this actually requires, not just what it sounds like it requires. Today, execution is entirely **UI-driven**: it reads a page's accessibility tree and drives it through clicks/typing/form-fills, exactly like a person using a browser. It has no concept of scoring free-text output for correctness, tone, safety, or consistency — it only knows PASS/FAIL/BLOCKED based on whether an element appeared or an action succeeded.

Testing an AI feature's accuracy/safety/consistency needs a fundamentally different kind of assertion: not "did this button appear" but "is this chatbot's answer factually correct, safe, and consistent with a repeat of the same question." That's a judgment call on free text, not a DOM check.

This forks into two possible designs, and **which one applies depends on something only you can answer**:

- **If the client's AI feature is reachable only through their app's UI** (e.g., a chatbot widget embedded in a web page, no separate API access given) — req2qa would still drive it via Playwright as today (type a question into the chat box, read the rendered response text off the page), but then run that captured response text through an LLM-as-judge scoring pass for accuracy/safety/consistency. This is an *extension* of the current UI-driven engine, not a replacement — smaller build.
- **If clients can/will provide direct API access to their AI feature** (an endpoint, a model ID) — req2qa could call it directly for the AI-specific test dimensions (faster, more reliable, no UI-scraping brittleness) while still using the existing UI-driven engine for any conventional parts of the same app. This is a genuinely new execution path alongside the existing one, not an extension of it — bigger build.

**Open question for you:** for the AI-powered applications you expect to test, will you typically have direct API/model access from the client, or only their app's UI (the same sandbox-URL-and-credentials model req2qa uses today)? This single answer determines whether this is a moderate extension or a second execution engine.

## 3. Suggested phased approach for this specific scope, folding in the two prior analyses

**Step 1 (intake/generation only, smallest slice):** Add a detection step at requirements intake — either the client states it, or Claude infers from the requirements text whether an AI/LLM component is present — and branch `qa_engine.py`'s generation prompt to also produce accuracy/safety/consistency/adversarial test case *descriptions* for the AI-flagged parts, using the diagram's categories as the taxonomy. At this step, nothing about execution changes yet — these test cases would generate and display on the curation screen like any other, initially without live execution support, so you can validate the generation quality and market interest before building execution for them.

**Step 2 (execution, UI-driven variant):** Extend `execute_engine.py` to capture a text response from the page (not just check element presence) and pass it to an LLM-as-judge scoring call for the accuracy/safety/consistency dimensions. This is the smaller of the two possible builds from section 2, and doesn't require any new client-side setup (same sandbox-URL model as today).

**Step 3 (execution, API-driven variant — only if you confirm clients will provide API access):** A second, genuinely new execution path for direct model/API testing, including performance/latency/cost measurement (which UI-driven testing can't measure accurately anyway, since browser rendering time would contaminate a latency number).

**Step 4:** Adversarial/red-teaming test generation and execution — the highest-complexity piece, deliberately last, reusing patterns from the standalone-platform PRD's research (garak/PyRIT-style probes) but implemented as prompts/checks within req2qa's existing architecture rather than adopting that tooling wholesale, consistent with the "fewer new files/dependencies" direction from the enhancement analysis.

## 4. What I need from you before any of this becomes a build plan

1. UI-only or API access for the AI-under-test's model/endpoint (section 2's fork) — this is the single biggest scoping decision.
2. Should Step 1 (generation-only, no execution) be the actual quick win, given it's the smallest slice that still tests the core idea?
3. Nothing gets built until you confirm — this is still analysis, matching how we've been working.
