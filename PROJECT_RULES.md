# Project Rules & Conditions

**Established:** 2026-10-15
**Working Directory Root:** `/home/aniruddh_sen/Documents/Kate/at`

---

## 1. Directory Scope Rule

All file operations, including but not limited to reads, writes, edits, searches, and deletions, must be strictly confined to the working directory root above. No operation shall traverse, reference, or modify files outside this boundary without explicit user permission.

- The root is immutable in terms of path; all relative paths resolve under it.
- Glob patterns and recursive searches must never escape this directory.
- External tool calls (browser, remote compile, integrations) used to *support* work are fine, but they must not create or modify files outside this directory.

## 2. Permission Levels

| Action | Permission Required |
|---|---|
| Creating new files (non-destructive) | None — auto-approved |
| Editing existing files | None — auto-approved |
| Deleting files or directories | Explicit user confirmation required |
| Overwriting/destroying existing content | Explicit user confirmation required |
| Running commands with side effects (install, build output) | Safe by default; destructive flags (`rm -rf`, etc.) require confirmation |
| Network/remote operations (deploy, compile in cloud) | Explicit user confirmation required |

## 3. Communication Rules

- Summarize findings in 5 lines or fewer for routine updates.
- Use TODO list tracking for multi-step tasks — always update immediately on status change.
- Ask for input only when genuinely blocked — prefer making a reasonable decision and flagging it for review.
- Commit changes as work progresses — one logical commit per meaningful unit of work.
- Report what files were changed and what remains outstanding at key checkpoints.

## 4. Code Quality Standards

- All code must include a brief header comment describing purpose.
- Prefer clarity over cleverness.
- Leave TODO/FIXME markers only with a clear owner and intent.
- If the project uses a specific language, follow the idiomatic conventions of that language.

## 5. Safety Rules

- Never reset passwords.
- Never complete payments or financial transactions.
- Never delete databases or irrecoverable data.
- Never send emails on your own initiative.
- If a destructive operation is blocked by the system, respect that decision and surface it rather than retrying with overrides.

## 6. Starting the Project

This document serves as the standing agreement for all work in this directory. When a new project is introduced, these rules remain in effect. Additional project-specific rules may be appended below as an addendum, but they cannot override Section 1 (Directory Scope) or Section 5 (Safety Rules).

---

*End of Rules*
