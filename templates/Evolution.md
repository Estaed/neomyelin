---
title: Evolution
type: memory
---

# Evolution

Repeated friction becomes a candidate; the user accepts or rejects each candidate.
For an implemented fix, record `- Decision: rule|tool|patch|promote` and
`- Decision note: YYYY-MM-DD what changed`. The gardener counts later cases from that date.

Where a fix goes (the candidate's type):
- `rule`: a one-sentence behaviour fix, written into the Rules file (`Rules.md`).
- `tool`: a problem a script, hook or setting removes. A rule that failed twice becomes a tool.
- `patch`: an existing skill missed a step or misled; patch it and name the skill in the note.
- `promote`: only a repeated multi-step workflow becomes a new skill, after one run of the same
  task without it and one with it shows a difference (one line under the candidate).
- `placed`: an idea that is not a skill went where it belongs (a project's notes, a task, a
  note); the decision note names the file.
- `reject`: no difference, or not a repeat. Rejected blocks stay; a deleted one comes back.

A skill that misleads during a session is patched in the same turn, without waiting for a
candidate. A candidate still pending after 30 days is closed as `reject`.

## Friction → candidate → decision

Examples:
- Friction: a command silently used the wrong working folder.
- Candidate: check the active folder before running the command.
- Decision: pending (accept or reject).

## Candidates
