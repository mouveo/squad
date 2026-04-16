# {Project name} — Plan {N}/{M}: {Plan title}

> One-paragraph description of this plan's scope and why it exists.
> Prérequis : {dependencies on previous plans, or "aucun"}

---

## LOT 1 — {Lot title, action-oriented}

One or two paragraphs describing what this lot does, which files it touches,
which patterns it follows. Keep the scope atomic: one lot must be executable
by Forge in a single autonomous batch.

**Success criteria**:
- Testable criterion 1 (observable from code, tests or filesystem)
- Testable criterion 2
- Testable criterion 3

**Files**: `path/to/file1.py`, `path/to/file2.py`, `tests/test_file1.py`

---

## LOT 2 — {Lot title}

Describe the work. Reference files that exist in the project when relevant.
Keep commits atomic and scoped to this lot only.

**Success criteria**:
- ...

**Files**: `...`

**Depends on**: LOT 1

---

## Format rules for generated plans

- A plan must contain between 5 and 15 lots (inclusive).
- Lots are numbered sequentially starting at 1. No gaps, no duplicates.
- Each lot must include a free-form description, a `**Success criteria**:`
  bullet list, and a `**Files**:` line with comma-separated paths.
- `**Depends on**:` is optional and references earlier lots by number.
- When the scope exceeds 15 lots, split into several linked plans and
  number them `Plan 1/M`, `Plan 2/M`, ..., each starting at LOT 1.
- Plans must be concrete and tied to the target project's stack. Avoid
  generic boilerplate like "set up CI/CD" unless the project actually
  needs it.
