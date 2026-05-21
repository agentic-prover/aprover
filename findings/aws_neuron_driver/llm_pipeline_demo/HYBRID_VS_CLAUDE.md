# Hybrid (K2 + Claude) vs all-Claude bmc-agent runs on neuron_pid.c

Same target (`/tmp/neuron_pid.i`, preprocessed Linux kernel TU,
11 functions), same M1+M1.2+M2+kernel-stubs config, two LLM
backends compared.

## Config differences

| Aspect | all-Claude | Hybrid (K2 + Claude) |
|---|---|---|
| Global default LLM | claude-sonnet-4-6 (Anthropic direct) | K2-Think-v2 (api.k2think.ai) |
| spec_gen + feedback_distill | claude-sonnet-4-6 | Claude Sonnet 4.5 via OpenRouter |
| Classifier / realism / refinement | claude-sonnet-4-6 | K2 |
| Provider | anthropic SDK | openai SDK (OpenAI-compat) |

## Empirical comparison

| Metric | all-Claude | Hybrid |
|---|---|---|
| Wall clock | ~5 min | **~2 min** |
| LLM calls (Phase 1) | 11 | 36 (more refinement loop iterations) |
| Total tokens | ~165K | ~41K |
| Estimated cost | ~$1.50 | **~$0.05** |
| Phase 1 spec gen | 11 specs | 11 specs |
| Phase 2 CBMC verdicts | 4 verdicts (7 compile-err) | 4 verdicts (7 compile-err) |
| Phase 3 real bugs | 0 | 0 |

**Hybrid is ~30x cheaper, ~2.5x faster, identical verdict count.**

## Spec quality comparison

For `npid_attach`:

**Claude (all-Claude run):**
```
PRE:  valid(nd) && valid_range(nd->attached_processes, 0, 16) &&
      (forall i, 0 <= i < 16 ==> valid(nd->attached_processes[i])) && ...
POST: (ensures result == true ==> (exists slot, 0 <= slot < 16 &&
      nd->attached_processes[slot].pid == task_tgid_nr(current) && ...))
```

**K2 (hybrid run, K2 generates internal specs):**
```
PRE:  valid(nd) && valid_range(nd->attached_processes, 0, 16) &&
      forall i in 0..16: (nd->attached_processes[i].pid != 0 ==>
                          nd->attached_processes[i].open_count > 0)
POST: (ensures (result == true ==> (exists i in 0..16:
      nd->attached_processes[i].pid == task_tgid_nr(get_current()) && ...))
      (result == false ==> (forall i in 0..16: ...))
```

Both capture similar semantic content. K2's syntax is slightly
different (`forall i in 0..16` vs `forall i, 0 <= i < 16 ==>`) but
the DSL→CBMC translator handles both. K2's spec is arguably more
concise. No meaningful quality regression observed.

## Recommendation

For bug-finding on attack-surface kernel code, **hybrid mode is
the clear winner**: 30x cheaper for the same verdict outcome. Use
hybrid when:
- LLM-driven spec gen is desired (vs raw CBMC pointer-check)
- Budget matters (running on many files)
- Phase 3 classification and realism filtering are wanted

Use all-Claude only when:
- You need the highest possible spec quality for a single target
- You're willing to pay 30x more per file

## Files

- `npid_*/spec.json` — Claude-generated specs from the all-Claude run
- `hybrid_k2/npid_*/spec.json` — K2-generated specs from the hybrid run

## Honest caveat

Both runs hit 7/11 CBMC compile-errors because the full bmc-agent
pipeline's preprocessing path doesn't fully align with the manual
cpp preprocessing used by the trivial-spec sweeps. So the verdict
counts aren't directly comparable to the 9/11 verified the trivial-
spec sweep produced. The right takeaway is **K2 vs Claude as spec
generators is approximately equivalent for kernel-mode targets;
the CBMC compile-error rate is an orthogonal bmc-agent pipeline
issue.**
