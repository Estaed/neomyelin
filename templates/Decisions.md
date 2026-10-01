---
title: Decisions
type: memory
---

# Decisions

The expectation and the outcome of important decisions. Personality counts what is done; this
file keeps what worked.

- **What goes in:** a decision that is costly to reverse (a tool, an architecture, a model policy,
  a project priority, dropping a piece of work); a request the assistant objected to on the basis
  of a pattern and the user went ahead with anyway (`Type: objection`, with a `Pattern:` line
  naming it); a reminder the user asked for (`Type: reminder`).
- **Expectation** is written so it can be checked ("X is done within two weeks"), never "it will
  be better".
- **Check** is a date. When it arrives the assistant asks the user what happened and fills the
  `**Outcome:**` line with `held`, `did not hold` or `partly`, plus one sentence.
- **An objection's outcome goes back to its pattern:** if the assistant was right, it is evidence
  for the pattern; if the user was right, evidence against it. An objection that is wrong several
  times in a row makes its pattern a candidate for removal.

New entries go below, newest first, in this shape (the example is quoted so it is never read as a
real entry):

> ## Decision: Keep every note in the vault, none in other apps
> **Date:** 2026-01-15
> **Type:** decision
> **Expectation:** within a month, no note is written outside the vault more than twice.
> **Check:** 2026-02-15
> **Outcome:** held (2026-02-15): one stray note in a month, moved the same day.
