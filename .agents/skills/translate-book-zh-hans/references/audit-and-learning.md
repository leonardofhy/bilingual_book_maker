# Audit records and learning loop

Maintain a complete, reversible trail from source unit to accepted translation.

## Unit record

Use stable content-based IDs. A correction record should contain at least:

```json
{
  "unit_id": "book_chapter.body.0001",
  "before": "Previous target text",
  "after": "Accepted target text",
  "reason": "Specific naturalness or fidelity defect and how it was resolved",
  "basis": "Source clause, term decision, or named review pass",
  "reviewers": ["human_reviewer", "fidelity_backcheck"],
  "status": "human_accepted"
}
```

Keep source text, source hash, content type, formatting requirements, and note anchors in the underlying unit store. Do not overwrite history with an unexplained final string.

## Status model

- `draft`: generated but not reviewed
- `llm_reviewed`: native and fidelity passes completed by the model
- `human_accepted`: human accepted the exact target text
- `needs_review`: a later audit found a material issue

Use the project's established status names when they differ. Never mark a unit human accepted by inference.

## Human review protocol

- Default to five target-language paragraphs when the user wants fluent reading.
- Move to one paragraph when the reasoning or wording needs focused discussion.
- Do not force side-by-side reading.
- When requesting a decision, show only the exact source clause needed to evaluate the change, followed by current text, diagnosis, and revision.
- Re-display the full changed batch after edits so the reader can detect new interactions.
- Record acceptance immediately; do not rely on conversation memory as the only ledger.

## Promote feedback carefully

Classify each correction:

1. **Local fix**: specific fact, ambiguity, or sentence problem.
2. **Project decision**: term, name, formatting, audience, or voice rule for the current book.
3. **Reusable skill rule**: a general method that improves new material without changing source meaning.

Promote a reusable rule only when:

- it is supported by at least one accepted case and a clear causal explanation;
- its scope and counterexample are stated;
- it does not merely encode one reviewer's preferred wording;
- it improves a fresh unit or catches a real failure during forward testing;
- it does not conflict with a source- or project-specific decision.

Keep project decisions in the project ledger. Keep only transferable procedure in the skill.

## Iteration cycle

After each accepted batch:

1. save exact corrections;
2. assemble and run structural/fidelity audits;
3. extract candidate lessons;
4. distinguish local, project, and reusable lessons;
5. update the project ledger;
6. update this skill only for reusable lessons;
7. validate the skill;
8. test the changed rule on a fresh, withheld source unit before trusting it.

When forward testing, do not reveal the expected answer. Compare the result against source constraints and record whether the rule helped, did nothing, or caused a new error.

## Publication gate

Before writeback or platform import, verify:

- every intended source unit is present exactly once;
- all accepted corrections are applied;
- no unaccepted draft silently replaced accepted text;
- term and name decisions are consistent;
- quotations, emphasis, omission marks, footnotes, and endnotes remain linked;
- the final target passes both native read and source-fidelity review;
- export format matches the collaboration platform's schema.
