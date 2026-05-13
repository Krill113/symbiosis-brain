---
name: brain-recall
description: >
  Search Symbiosis Brain for relevant context BEFORE acting. Trigger silently on these
  signals: (a) before first Edit/Write in an unfamiliar file or scope; (b) before grep
  across >3 files / a whole subtree; (c) when the user describes a bug or unexpected
  behavior — recall similar fix history; (d) after any subagent error or contradicted
  hypothesis — recall if this mistake is already documented; (e) before using an API
  / library that hasn't appeared in this session yet. Don't ask "should I check?" —
  just check. Report only when something relevant was found.
---

# Brain Recall — Task Context Search

## Automatic action-trigger recall

As of 2026-05-13, brain recall fires automatically before decision-class tool calls (Task, Edit, Write, MultiEdit, NotebookEdit, and whitelisted Bash commands like `git commit`, `pip install`, `docker push`). You will see a `[recall: N hits for "..."]` block in `<system-reminder>` injected by `hooks/brain-pre-action-trigger.sh`.

This skill is still useful for:
- Explicit recall when no tool call is imminent (pure exploration / discussion).
- Searching by topic when the auto-hook query (derived from tool args) is too narrow.

Runtime config: `~/.claude/symbiosis-brain-pre-action.json` (toggle matchers, edit Bash whitelist, etc.).
Kill-switch: `SYMBIOSIS_BRAIN_PRE_ACTION_DISABLED=1`.

Design: `decisions/2026-05-13-brain-recall-action-trigger-design.md` (in vault).

## When to use — concrete triggers

Trigger silently on **any** of these signals (no permission needed, no narration if nothing relevant):

1. **Before first Edit/Write in a new file or scope.** Recall existing patterns + mistakes for that area before touching code.
2. **Before grep across >3 files or a whole subtree.** Memory may already have the answer — saves the read chain.
3. **When the user describes a bug, unexpected behavior, or "что-то не так".** Recall similar fix history before forming a hypothesis.
4. **After a subagent error or a contradicted hypothesis.** Recall whether this exact mistake is already documented as `mistakes/*`.
5. **Before using an API / library that hasn't appeared in this session.** Recall gotchas (`thirdparty-libs-fragile`, etc.).
6. **When the user uses phrases like «как обсуждали», «как раньше», «помнишь».** This is an explicit memory pointer — surface 2-3 brain_search variations, not a single shallow lookup. See [[mistakes/session-start-dove-into-files-skipped-recall-and-delegation]].
7. **Before brainstorming, debugging, or implementation that spans multiple projects.** Cross-project patterns live in `global` scope.

If none of the above apply — don't recall. The hook auto-injects `[memory: …]` for substantive prompts; you only need explicit recall for the moments above.

## Procedure

### Step 1: Extract keywords

From the user's request, identify:
- Project name (if mentioned)
- Technology or API names
- Problem type (debugging, architecture, migration, etc.)

### Step 2: Search memory

Call `brain_search` with extracted keywords. If a project scope is known, pass it.

### Step 3: Process results

**If relevant results found:**
- Call `brain_read` on the most relevant notes (up to 2-3 notes, budget ~5K tokens)
- For complex cross-project topics: call `brain_context` with the key entity to see graph connections. Use L3 ONLY when clearly needed — not for simple questions.
- If a loaded note has a staleness warning — treat it as unreliable. Mention the caveat if you use the information.

**If nothing relevant found:**
- Silently continue. Do NOT say "nothing found in memory" or "I checked memory and..."

**If >10 results returned:**
- Narrow the query with more specific keywords. Don't load everything.

### Step 4: Integrate into response

Weave relevant context naturally into your response or work approach.
Brief summary only (1-2 lines if mentioned at all). Don't dump raw note contents.

### Step 5: Delegate when appropriate

If the search is routine and doesn't require the main conversation context,
delegate to a subagent (haiku model) with a specific question.
Only conclusions come back to the main context.
