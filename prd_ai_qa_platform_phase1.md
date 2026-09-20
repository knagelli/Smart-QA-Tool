# PRD: AI Model QA Platform — Phase 1

**Author:** Kalyan (drafted by Claude) · **Date:** 2026-09-19 · **Status:** Draft for review

---

## 1. Why this document exists

You provided an architecture diagram for a full "AI Model QA Platform" — test case generation, an evaluation engine (LLM-as-a-judge), regression suite management, drift monitoring, reporting/visualization, and alerting/integrations. Before writing a line of code, I researched what already exists in the open-source and commercial ecosystem for each piece of that diagram, so we don't rebuild something that's a `pip install` away. This PRD gives you that research, a clear build-vs-buy recommendation, and a phased plan starting with a quick-win v1.

## 2. Headline finding: this leans "integrate," not "build"

Two open-source tools together already cover roughly 90% of your diagram:

| Your diagram box | Covered by |
|---|---|
| Test Case Generation (Prompt, Safety, Adversarial) | **promptfoo** (built-in generator + dedicated red-team/adversarial module) |
| Evaluation Engine (LLM-as-a-Judge, Metrics) | **promptfoo** (assertions + model-graded judges) and **DeepEval** (30+ metrics) |
| Regression Suite Management (Golden Datasets) | **promptfoo** (version-controlled test configs, pass/fail history) and **Langfuse** (Datasets feature) |
| Drift Monitoring (Trends) | **Langfuse** (production tracing + score trends over time) |
| Visualization | **promptfoo**'s built-in web viewer, **Langfuse**'s dashboards |
| Alerting / Integrations (CI/CD, Slack, Jira) | **promptfoo**'s official GitHub Action + CI docs; **Langfuse** webhooks. Neither ships native Jira — that's light glue code, not a platform to build |

**Recommended stack: promptfoo (pre-production: generation, red-teaming, CI gating) + Langfuse (post-deployment: production observability, drift, dashboards).** Both are open source, self-hostable (relevant if AU data residency matters, per the Bedrock migration work already underway), and actively maintained.

What's genuinely left to build is small and specific to you: a thin UI tying the two together, Jira/Slack glue, an MCP wrapper if you want agent-facing access, and any requirements-doc-to-test-case pipeline specific to your existing req2qa workflow that these tools don't already do out of the box.

## 3. Full research findings

### 3.1 Test case generation (incl. adversarial/safety)
- **promptfoo** (MIT, CLI + library) — generates test cases from prompts/specs; dedicated red-team module for jailbreak/injection/PII/bias probes.
- **DeepEval** (Apache 2.0, Python) — `Synthesizer` generates synthetic test cases from docs/context; pytest-style.
- **garak** (Apache 2.0, Python CLI, NVIDIA) — LLM vulnerability scanner, dozens of built-in attack probes.
- **Microsoft PyRIT** (MIT, Python) — orchestrates automated red-teaming conversations against gen-AI systems.
- **Giskard** (Apache 2.0 OSS + commercial enterprise tier) — auto-generates adversarial cases for LLM agents/RAG.

### 3.2 Evaluation engine / LLM-as-a-judge
- **promptfoo** — built-in graders for relevance/factuality/safety, custom rubrics.
- **DeepEval** — 30+ research-backed metrics (faithfulness, bias, toxicity, hallucination, custom G-Eval judges), latency/cost tracking.
- **RAGAS** (Apache 2.0) — RAG-specific metrics (faithfulness, context precision/recall).
- **Arize Phoenix** (source-available self-host license + commercial cloud) — tracing, built-in eval templates, embedding drift viz, **has an official MCP server**.
- **Langfuse** (MIT/Apache-2.0 core + paid cloud) — self-hostable eval + observability, dataset-based evals, prompt management.
- Honorable mentions: Braintrust (commercial/freemium), TruLens (Apache 2.0), MLflow's `evaluate()` module (Apache 2.0).

### 3.3 Regression suite management
No standalone product category — it's a feature bundled into the above. promptfoo (version-controlled YAML/CSV test cases, CI diffing) and Langfuse (Datasets feature, run comparisons over app versions) both cover this well.

### 3.4 Drift monitoring / observability
Least mature pure-OSS area. Arize Phoenix and Langfuse both do production trace monitoring and score trends. Galileo/WhyLabs/Fiddler are commercial specialists here if you outgrow the OSS option. Most "drift detection" is a statistical-distance calculation over score history — not much proprietary value in building this yourself versus wiring alerts on top of Langfuse/Phoenix trends.

### 3.5 Visualization / reporting
Not a separate market — bundled into whichever eval engine you pick. promptfoo has a built-in local web viewer; Langfuse and Phoenix have full dashboards; MLflow has a generic experiment UI.

### 3.6 Alerting + CI/CD/Slack/Jira integrations
- promptfoo: official GitHub Action, documented CI/CD guide (GitHub Actions/GitLab/CircleCI/Jenkins).
- DeepEval: native pytest integration, runs in any CI; hosted Confident AI layer adds Slack notifications.
- Langfuse: webhooks for score/trace events.
- **None ship native Jira integration** — plan for a small custom webhook, not a platform build.

### 3.7 MCP servers in this space
- **Arize Phoenix MCP server** (official, `@arizeai/phoenix-mcp`) — exposes traces/datasets/experiments/prompts to any MCP client.
- promptfoo can act as an MCP *security scanner* (it red-teams other MCP servers — not itself an eval-via-MCP server).
- `mcp-eval` (lastmile-ai) — evaluates MCP servers themselves, a narrower adjacent use case.
- The MCP ecosystem here is thin; if you want an MCP surface onto this platform later, it's a thin wrapper over Phoenix/promptfoo's existing APIs, not core logic to build.

### 3.8 All-in-one platforms
promptfoo is the single tool covering the most ground across your whole diagram. It does not natively cover production drift monitoring or Slack/Jira alerting — hence the promptfoo + Langfuse pairing recommended above.

## 4. Phased plan

The goal for Phase 1 is a real, usable quick win — not the full diagram. Later phases layer in complexity only once Phase 1 is proven useful.

### Phase 1 (quick win — recommended first build)
**Scope:** Wire promptfoo into a minimal end-to-end loop for one real use case: given a system prompt/spec (reusing your existing req2qa-style requirements input), auto-generate test cases (including a basic adversarial/safety pass), run them against your model under test, and view pass/fail results in promptfoo's built-in viewer.
**Explicitly out of scope for Phase 1:** production drift monitoring, Langfuse integration, Slack/Jira alerting, custom dashboards, MCP wrapper.
**Why this is the right first slice:** it requires zero new infrastructure (promptfoo runs as a CLI/config-driven tool), validates the core value prop (auto-generated + auto-evaluated test cases) end-to-end, and gives you something to react to before investing in observability/alerting plumbing that only matters once something is actually in production.
**Deliverable:** a working promptfoo config + a short internal doc on how to run it, using one real requirements doc as the pilot.

### Phase 2 (regression + reporting)
Add version-controlled regression suites (promptfoo's native capability) and a lightweight shared reporting view, so results are visible to more than one person without opening the CLI output.

### Phase 3 (production observability)
Introduce Langfuse for post-deployment tracing, drift trends, and dashboards once there's an actual production model/prompt to monitor.

### Phase 4 (alerting + integrations)
Slack/Jira glue code on top of Langfuse webhooks and promptfoo's CI exit codes; CI/CD gating in your existing pipeline.

### Phase 5 (optional — MCP surface)
Only if you want agent-facing access to eval results — a thin MCP wrapper over Phoenix/promptfoo/Langfuse's existing APIs.

## 5. Open questions for your review

1. Does "AI Model QA Platform" here mean testing *your own* AI features (e.g., the Bedrock-backed generation in req2qa), testing *third-party* models/prompts for clients, or both? This affects whether Phase 1's pilot spec should be your own system prompt or a client's.
2. Any AU data-residency requirement for this platform specifically, given the Bedrock migration work already in progress? Both promptfoo and Langfuse are self-hostable, which keeps this option open either way.
3. Confirm Phase 1 scope above before any setup work starts — no code/environment changes will be made until you sign off, consistent with how we've been working.
