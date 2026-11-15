---
name: handoff
description: >-
  Write or resume from a local handoff note (.handoff/*.md) so work survives
  context compaction or moves to a new conversation. Use when the user says
  "写交接", "handoff", "保存进展", "接着上次的做", "从交接文件继续", or before
  deliberately starting a fresh conversation.
---

# Handoff

Handoff notes live in `<workspace>/.handoff/`. Each conversation has one
human-quality note `<YYYYMMDD-HHMM>_<conv8>.md`, written by the agent, and one
`*.auto.md` mechanical snapshot written by the `context-handoff` hook.

## Write a note (manual `/handoff`)

1. Find this conversation's note: the newest `.handoff/*_<first 8 chars of the conversation id>.md`.
   If none exists, create `.handoff/<YYYYMMDD-HHMM>_<conv8>.md`.
2. Overwrite it with the template from the Context Handoff rule (目标 / 当前状态 /
   已完成 / 下一步 / 关键决策 / 坑 / 关键路径). Be concrete: absolute paths, exact
   commands, config values, verbatim error text. Max ~150 lines.
3. "下一步" must be executable by someone who never saw this conversation.
4. Tell the user the note path in one line.

## Resume from a note (new conversation)

1. List `.handoff/*.md` (excluding `*.auto.md`) by modification time; pick the
   one the user means (ask if ambiguous).
2. Read it and its `.auto.md` sibling (includes `git status` / `diff --stat`
   of the repos touched). If the note looks thin, check older versions in
   `.handoff/history/` (`*.vNN_*.md` = every saved version, `*.cN.md` = frozen
   at compaction N).
3. Verify the state is still true before acting (files exist, services running,
   tests status), because the note can be stale.
4. Briefly confirm the plan with the user, then continue from "下一步".
5. From now on, keep writing to a new note for this conversation (the hook will
   give you its path); copy over still-relevant sections.

## Housekeeping

- The hook adds `.handoff/` to the repo-local `.git/info/exclude` automatically,
  so `.gitignore` needs no change. Add it to `.gitignore` only if you want that
  to be shared with the team.
- Old notes and `history/` can be deleted freely; nothing depends on them.
