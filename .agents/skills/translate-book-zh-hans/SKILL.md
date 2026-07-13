---
name: translate-book-zh-hans
description: Translate, rewrite, compare, and audit English book prose into publication-quality Simplified Chinese with source-content alignment, terminology control, native-Chinese reconstruction, paragraph traceability, human review, and a separate source-fidelity backcheck. Use for book chapters, quotations, footnotes/endnotes, competing translation versions, or guided paragraph review. Do not use for casual sentence translation or localization that replaces the author's examples, people, places, or claims.
---

# Translate English Books into Simplified Chinese

Produce native Simplified Chinese without trading away the source's propositions, logic, voice, or traceability. Treat human feedback as evidence for an evolving translation standard, not as permission to agree blindly.

## Choose the operation

- **Translate new material**: build a source brief, draft, run both review tracks, then present it.
- **Revise a translation**: diagnose the exact readability or fidelity failure before rewriting.
- **Compare versions**: align every version to the same source content, anonymize identities, judge independently, then reveal and aggregate.
- **Guide human review**: show target text in small batches by default; when proposing a change, show the exact source clause, current text, reason, and revised text.
- **Consolidate a chapter**: apply only accepted corrections, preserve IDs and markup, then assemble and audit.

Read [references/quality-rubric.md](references/quality-rubric.md) before translating or judging. Read [references/zh-hans-prose.md](references/zh-hans-prose.md) before drafting or rewriting Chinese. Read [references/audit-and-learning.md](references/audit-and-learning.md) when recording decisions, handling human feedback, or updating this skill.

## Core workflow

### 1. Establish the contract

Identify the source text, target as Simplified Chinese, genre, audience, authorial voice, paragraph policy, approved terms, name policy, note/quotation handling, and localization boundary. Preserve the author's examples, people, places, and claims unless the user explicitly authorizes adaptation.

Keep source paragraph boundaries for audit and import unless the project specifies another stable unit. Reorder information and split or merge sentences inside a unit when Chinese requires it.

### 2. Align by source content

Use the same English source span as the comparison unit. Do not assume target paragraph counts match: one source paragraph may become several Chinese paragraphs or vice versa. Group rewritten target spans back to their source unit before comparing versions.

### 3. Build a source brief

For each unit, record:

- propositions and referents;
- causality, contrast, negation, modality, quantities, and scope;
- names, terms, quotations, and cross-references;
- tone, rhetorical purpose, and links to adjacent units;
- meaning-bearing formatting such as emphasis, omission marks, and note anchors.

Do not draft until the unit's argument and dependencies are understood.

### 4. Draft by reconstruction

Preserve meaning, not English syntax or information order. Rebuild the unit around a Chinese reader's path: establish the subject, state the point, then add background, qualification, contrast, or consequence.

Use the approved termbase as evidence, not as a blind substitution table. Resolve contextual variants explicitly. Flag ambiguity instead of inventing certainty.

### 5. Run native-Chinese review without consulting the source

Read the draft aloud. Check subject continuity, pronouns, speaker identity, sentence breath, particles in dialogue, punctuation, rhythm, and translation-like phrasing. Require a one-read paraphrase: if a reader cannot restate the point and its connection to the previous unit, the draft has not passed.

### 6. Run a separate source-fidelity backcheck

Return to the source and verify every proposition, entity, relationship, negation, modality, quantity, rhetorical address, term, note marker, and emphasis. Treat omissions, additions, contradictions, or scope changes as hard failures; fluency cannot compensate for them.

Mark any explicitation. Keep it only when the local source and context already entail it and it adds no new evidence, conclusion, example, or causal claim.

### 7. Reconcile and present

Resolve hard fidelity failures first, then choose the most natural faithful Chinese. Preserve the author's rhetorical posture without copying English sentence architecture.

For ordinary human review, present only the Chinese batch. When a revision needs approval, present:

1. the exact source clause;
2. the current Chinese;
3. the specific problem;
4. the revised Chinese;
5. a recommendation with its tradeoff.

Do not silently replace accepted text.

### 8. Record and learn

Save accepted decisions with stable unit IDs, before/after text, reason, evidence, reviewers, and status. Distinguish a local editorial choice from a reusable rule. Promote a rule into this skill only when it is generalizable, supported by reviewed examples, bounded against overuse, and compatible with fidelity.

### 9. Finalize

Assemble only accepted units. Audit completeness, unit coverage, terminology, names, notes, emphasis, omission marks, and source links. Do not write back to the publication artifact until the project's human approval gate is satisfied.

## Repository integration

When working in `bilingual_book_maker`, reuse the existing source-brief, editorial, comparison, assembly, and audit scripts under `scripts/`; inspect each script's `--help` before running it. Keep review artifacts under the established preview directory and do not add ignored translation artifacts to Git unless the user requests it.

Use the project decision ledger and correction overlay as the current source of truth. Human-approved project decisions override generic skill guidance.
