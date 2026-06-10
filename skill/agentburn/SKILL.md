---
name: agentburn
description: Answer questions about this agent's own token spend and burn. Use when the user asks "how much am I spending", "where do my tokens go", "what did you burn while I slept", "почему так дорого", "сколько я трачу", asks for a cost/usage breakdown, wants to cut the agent's bill, or asks what the agent has been doing (functions, loops, failures). Runs the local agentburn profiler (zero-dependency, read-only, nothing leaves the machine).
---

# agentburn — the agent answers for its own bill

You have access to a local profiler that reads THIS agent's own accounting
database read-only. Use it instead of guessing about costs.

## How to answer a cost question

1. Make sure the CLI exists: run `uvx agentburn --version`
   (fallbacks: `pipx run agentburn --version`, `pip install agentburn`).
2. For "how much / where does it burn":
   run `uvx agentburn --json` and read: `total`, `monthly_projection`,
   `by_source` (cron / heartbeat / gateway:* / subagent / cli), `night`,
   `overhead_per_call`, `recommendations`.
3. For "what have you been doing / why is it expensive":
   run `uvx agentburn why --json` and read: `functions`, `rereads`,
   `storms`, `idle_heartbeats`, `failure_cost`, `observations`.
4. For one channel ("what did you do in telegram?"):
   add `--source telegram` (or cron / heartbeat / subagent / cli).
5. Answer in the user's language, lead with the verdict
   (pace + dominant source), quote at most 3 numbers, then the single
   highest-impact change. Mark estimated costs with "~".

## Fixing things

- Offer `uvx agentburn fix` — it prints ready-to-paste config patches and
  NEVER applies them. Show the patch; apply only if the user explicitly
  confirms, and re-check with `agentburn --compare` afterwards
  (baseline first: `agentburn --save-baseline`).

## Honesty rules (do not skip)

- If the output warns about zero-usage sessions, say the totals are a
  LOWER BOUND and suggest `uvx agentburn doctor`.
- Numbers come from the agent's own accounting; do not invent prices or
  savings beyond what the tool prints.
- Never paste raw session titles or file paths into public channels;
  categories and counters only (the `--share` card is safe by design).
