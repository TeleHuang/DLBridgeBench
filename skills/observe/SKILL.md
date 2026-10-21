---
name: observe
description: "Start a recorded manual observation trial: you become the migration agent, work in a prepared ME/AR workspace, and every prompt/tool/transcript is recorded by plugin hooks. Use when the user wants to watch the model work under the benchmark flow."
argument-hint: "<natural-language instruction, e.g. 把 L1 的 gelu/relu 适配到 torchax>"
allowed-tools: [Bash, Read, Write, Edit, Glob, Grep]
---

# Manual Observation Trial

Start a real ME/AR trial in which **this interactive session is the observed
agent**. The audited Python runner prepares the workspace, prompt, references
and self-check endpoint; the TBBCC plugin hooks record what this session does.

Results are labelled `manual_observation`, `formal_experiment=false`: they are
for watching and debugging the flow, not paper data. Never relabel them.

## 1. Parse the operator instruction

`$ARGUMENTS` is the natural-language instruction. Extract:

- which case(s) to adapt — use `Glob`/`Grep` under
  `${CLAUDE_PLUGIN_ROOT}/benchmarks/v1.0.0/cases/` to resolve case ids;
  if none is named, use `L1/act/gelu.json` and `L1/act/relu.json`;
- the bridge/approach — default `torchax`; only change it if the instruction
  names another supported bridge;
- the number of repair rounds — default `2`.

Do not modify pristine cases. The runner copies them into the trial workspace.

## 2. Record the instruction verbatim

Use `Write` to create `.agentic-instruction.md` in the current directory with
the operator's instruction unchanged. This file is embedded in the runner
prompt and is also captured by the `UserPromptSubmit` hook.

## 3. Start the trial

Resolve the sibling ME/AR repository and target interpreter, then run:

```bash
CHAT="${AGENTIC_CHAT_ROOT:-$(dirname "${CLAUDE_PLUGIN_ROOT}")/HTLsAgentsChat-main}"
TP="${AGENTIC_TARGET_PYTHON:-$CHAT/.venv-torchax/bin/python}"
[ -x "$TP" ] || TP="$CHAT/.venv-torchax/Scripts/python.exe"

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/observe_session.py" start \
  --root "$PWD/trial" \
  --session-dir "$PWD" \
  --trial-id "$(basename "$PWD")-$(date -u +%Y%m%dT%H%M%SZ)" \
  --case "<absolute case path 1>" --case "<absolute case path 2>" \
  --rounds 2 \
  --instruction-file "$PWD/.agentic-instruction.md" \
  --target-python "$TP"
```

Add `--target-python` only when the interpreter above exists; otherwise report
the missing environment instead of guessing.

The command returns JSON. Check `status`:

- `ready` — continue. Keep `run_dir`, `workspace`, `prompt`, `submission`.
- `failed_to_start` — read `state`/`log_tail`, report the blocking reason, and
  stop. Do not retry with loosened checks.

Verify the recorder is live once: `<run_dir>/observe/hook_events.jsonl` should
appear after the next tool call. If it never appears, the session was started
without `--plugin-dir`; say so and continue only if the user accepts an
unrecorded run.

## 4. Be the observed agent

After `start` returns, read `prompt` (`prompts/r1.md`) and **begin the migration
work immediately**. Do not spend turns inspecting the runner, grader, reference
generation or TBBCC internals: the prompt is the authoritative contract and
everything needed is in the workspace. A normal round is: read the case and
official bridge docs/examples → edit the workspace → run at most the allowed
self-check batches → write the submission. Then:

- Work only inside `workspace` (`cases/`, `adapter.json`, `helpers/`,
  `requirements.txt`). Never edit `refs/`, `grader/`, the TBBCC repository, or
  this session's own `.agentic-observe.json`.
- Use the self-check endpoint from the prompt; its quota is enforced by the
  runner. Do not run `/torchbridgebench:eval`, `tbbcc.py eval-suite`, or edit
  the grader — those bypass the metered flow.
- Do not spawn subagents for the migration work; the transcript and hooks
  observe this session, and subagent behaviour would be recorded separately.
- Never read credentials, `~/.claude/settings.json`, `.secrets`, or environment
  dumps: tool outputs are recorded (key-name and common token patterns are
  redacted, but that is not a guarantee).

When this round is done, write the structured submission exactly as the prompt
specifies, to `submission`:

```json
{"schema_version":1,"round_id":1,"outcome":"ready","changed_files":[],"unresolved_cases":[]}
```

The submission file is the only signal that ends the round.

## 5. Wait for grading, then continue or finish

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/observe_session.py" wait "<run_dir>" \
  --round 1 --timeout 3600
```

- If `next_prompt` is non-null, read it and run the next round from step 4.
- If `result` is present, stop and report.
- `timed_out` means grading is still running; report the state, do not resubmit.

## 6. Report

Include: `run_dir`, trial label (`manual_observation`), rounds, per-round
passed/n and failing channels, `workspace`, and the recorder paths
(`observe/hook_events.jsonl`, `observe/transcripts/`, `observe/session_summary.json`).
State plainly that these are manual-observation records, not formal paper data.
