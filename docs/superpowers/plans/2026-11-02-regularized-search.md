# Regularized search implementation plan

Goal: Add evidence-linked proposals, bounded search, trace diagnostics, cost accounting,
repeat evaluation, pre-evaluation review, exploration and optional worktrees.
Architecture: Keep all enforcement in kernel; mutable policy receives evidence and
writes a proposal manifest with its patch. No third-party dependencies or paid trials.

1. Write regression tests for budget enforcement, noise-aware acceptance, exploration,
   history retention, proposal rejection, diagnostics, independent review and worktrees.
2. Add kernel/search.py with validated search settings, paired-score decision,
   trace normalization, proposal review and candidate workspace management.
3. Integrate into Engine: persist usage before calls, collect history and diffs,
   require evidence-linked manifests, review before evaluation, evaluate repeated
   paired trials and retain the existing two-stage ratchet and rollback.
4. Expose settings in CLI, update policy and scripted fixture, document protocols and
   limitations (heuristic diagnostics, reported costs, statistical assumptions).
5. Run python -m unittest discover -s tests -v, inspect diff and run CLI demo in an
   independent temporary state. Preserve all existing experiment state.

Defaults chosen: 2 trials, noise floor .01, 95% paired standard-error margin;
200 changed lines decaying by .9 to a floor of 20; exploration after 2 rejections;
pruning proposal every 5th round; independent reviewer/worktrees opt-in.
A pruning proposal must delete net source lines and pass the normal evaluation.
Token/cost limits require reported usage; missing reports block further calls.

Execution result: implemented and verified 2026-11-02 with Python 3.12, 27 tests pass.
CLI worktree demo accepted [true, true, false], 21 scripted calls, zero model usage.
Independent review found baseline trace failures aborting before proposal feedback;
fixed with a distinct TraceFailure and a red-to-green subprocess lifecycle test.
Original-mechanism checklist lives in README; semantic analyzer, causal mapping,
native trace adapters and mechanism-level ablation remain explicitly partial.

Follow-up: migrate upstream model integration rather than leaving it as a generic
coder hook. kernel/backends.py owns stdlib DeepSeek HTTP and Codex CLI transport;
CLI selects independent task/coder/reviewer roles. Mutable solver owns task prompts
and parsing, with one stateless task-model response supplied by the frozen runner.
Task transport shares the usage ledger; outages propagate instead of scoring zero.
Task configuration is persisted and cannot be mixed into an existing offline state.
Tests use local HTTP and fake CLI only; real-model experiments remain unchecked.
Follow-up validation: 38 tests passed (11 backend tests), Python 3.12. Focused
independent review identified redundant paid score calls, demo/custom-reviewer
leakage and lost Codex usage on missing output; all reproduced red and fixed.
No real model calls. Codex login-status check succeeds; DeepSeek key is absent
from the current process environment.
Codex timeout follow-up: direct ChatGPT request timed out; system-proxy request
returned HTTP 405 in 1.52 seconds. Bridged OS proxies to Codex child environment
without overriding explicit proxy vars. Added opt-in --codex-isolated and retained
partial native events/usage on timeout. Unmodified-config requests were variable;
isolated standalone succeeded in 50.83 seconds, and final project-adapter invocation
with automatic system proxy plus isolation returned 5 in 26.64 seconds (22,957 tokens).
All 42 offline tests pass. Real multi-round self-improvement remains untested.
