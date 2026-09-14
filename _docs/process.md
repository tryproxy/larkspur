- Tasks are GitHub issues, one at a time
- Read the acceptance criteria before starting and before closing
- Commit regularly

Labels

- `mvp` - needed for the current minimum viable product
- `post-mvp` - real work, deliberately deferred
- Every issue carries exactly one of the two above labels

Roles

- Orchestrator - the main session; follows this lifecycle, coordinates the
  `pm`, `engineer`, and `qa` agents, and does not perform their work
- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer - implements one groomed task, follows _docs/team/engineer.md
- QA - checks the result, reports PASS or FAIL, and comments on FAIL; follows _docs/team/qa.md

Lifecycle

For issue selection:

- Consider only open `mvp` issues whose listed dependencies are closed.
- If multiple issues are eligible, choose the lowest issue number.
- If the selected issue is blocked or conflicts with `_docs/decisions.md`, stop
  and report it.
- Stop when no eligible open `mvp` issues remain.

1. Pick the next eligible issue using the rule above
2. PM grooms it
3. Engineer implements it
4. QA verifies it
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, close the issue
7. Repeat until no eligible open `mvp` issues remain

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code, only outputs PASS or FAIL
- The orchestrator closes the issue only after QA outputs PASS
