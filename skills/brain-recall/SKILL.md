---
name: brain-recall
description: >
  Search Symbiosis Brain for relevant context before starting work. Use when the user
  gives a new task, asks about a project or technology, before brainstorming or debugging.
  Check memory SILENTLY — don't ask "should I check?", just check. Report only if
  something relevant was found.
---

# Brain Recall — Task Context Search

## When to use

- User gives a new task or asks a question
- Before starting brainstorming, debugging, or implementation
- When encountering unfamiliar code, API, or pattern
- When the topic touches multiple projects

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
