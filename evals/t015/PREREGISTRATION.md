# T-015 · pre-registration

Written 2026-09-16, **before any output existed**. The procedure follows G1.1's
blind protocol (`clean-ai-engineering/TODO.md`). This is T-015, a *describe*
exercise, and not a G1 generation.

## Inputs, pinned

| | |
|---|---|
| AOAS | `clean-ai-engineering@fa347be` `drafts/examples/support-agent.aoas.yaml` |
| AHC | `ai-harness-catalog@054229a` `capabilities/*.yaml` (110) + `ports/*.yaml` (17) |
| AAC | `ai-assurance-catalog@1a5c143` `catalog/*.yaml` (113) |
| Reference, for comparison only | `reference-agent@5e7d6b0`, never given to the model |
| `specs.txt` sha256 | `2bd31c4add05a56ad222c5b0c8e867a6a4d4a71c2c68beb50f85f4d429861926` |
| `prompt.md` sha256 | `2a8025dee60ba464d4cab13ce6115920de849094d0f4eb4d8e0cec729b2fb9ea` |

**One edit to the specs, declared.** The AOAS `sources:` block lists
`reference-agent@v0.1.0 src/support_agent/{router,policy,approvals,escalation,identity,config}`.
Those two lines are removed because they name the answer's module names outright.
This is the circularity T-008 already declares. The rest of the file is unchanged,
and no AHC or AAC file mentions the reference.

**Not included:** the harness profile (binding), the AWD world, the blueprint and
the scenarios. T-015 names only AOAS, AHC and AAC.

## Generator, pinned

- Claude Code `2.1.241`, `claude -p --model claude-opus-5`
- `--tools ""`: no tools at all, so the model cannot read any file. This is stronger than a deny rule.
- `--system-prompt` replaced, `--setting-sources ""`, `--strict-mcp-config` with no servers, `--no-session-persistence`
- cwd is `/Users/Shared/t015-blind/run-N`, outside the home directory, so no parent `CLAUDE.md` and no project memory is loaded
- **N = 3**, identical inputs, run in parallel
- **Known leak, declared:** `--bare` needs an API key, so the user-global `~/.claude/CLAUDE.md` may still load. It is 3 lines, and its only instruction is "prefer table-driven tests". It says nothing about the reference.
- **Training contamination:** not possible. The reference's first commit is 2026-08-31, after the model's May 2026 knowledge cutoff.

## How the answers will be read, fixed now

Each run is scored against the reference on these questions:

1. **Packages.** How many of the reference's 25 top-level packages have a same-responsibility counterpart, whatever it is called?
2. **Seams.** Which of the 7 protocols (`LLMClient`, `ToolClient`, `CheckpointStore`, `IdempotencyLedger`, `ApprovalStore`, `EscalationStore`, `Clock`) does it declare, and what does it add?
3. **Direction.** Does its layering agree with the 9-layer import contract (serve → reviewer → entrypoint → router|loop → … → contracts)?
4. **Things T-008 predicted no spec constrains.** A prediction that could be wrong:
   - the typed `TurnResult` union
   - the conversation persisted as one blob
   - five policy positions
   - escalation split into wording / capacity / store / workflow
   - gates running before routing
   - size and complexity ratchets
5. **Agreement across the three runs.** Where the runs disagree, the specs leave the choice open. That is T-015's most useful output.

T-015's own rule for reading this: a **close** answer weakens T-010's argument, a **distant** one confirms the structural gap, and a **suspiciously exact** one counts as contamination, not as sufficiency.
