## What

<!-- One paragraph: what this change adds and which PLAN.md row it completes. -->

## Behaviour (Given / When / Then)

<!-- One scenario per guaranteed behaviour, each linked to the test that proves it.
     Reviewers should be able to understand the change from this section alone. -->

| # | Given | When | Then | Proven by |
|---|---|---|---|---|
| 1 |  |  |  |  |

## Decisions

<!-- Choices a reviewer could reasonably question, with the reason and the alternative. -->

| Decision | Why | Alternative rejected |
|---|---|---|
|  |  |  |

## Safety

<!-- What this change guarantees about leaks, network, licenses, logs and temp files. -->

## Review map

| Priority | File:line | What to check |
|---|---|---|
|  |  |  |

## Screenshots

## Leak report

## Checklist

- [ ] Safe (leak test, offline test, license check, no raw values in logs)
- [ ] Enough (meets the phase's done-when row in docs/PLAN.md §11)
- [ ] Required (every file traces to docs/PLAN.md)
- [ ] Shortest (nothing unnecessary)
- [ ] Documented (every non-obvious rule has a comment saying why)
- [ ] Reviewable (one concern per commit, reason in the body)
