- Tasks are GitHub issues, one at a time
- Read the acceptance criteria before starting and before closing
- Commit regularly

Labels

- `mvp` - needed for the current minimum viable product
- `showcase` - current work to make the completed MVP presentable and publicly demoable
- `future` - real product work deliberately deferred beyond the current showcase
- `draft` - not yet groomed; excluded from issue selection
- Every issue carries exactly one stage label: `mvp`, `showcase`, or `future`.
  A draft may also carry `draft`.

Roles

- Orchestrator - the main session; follows this lifecycle, coordinates the
  `pm`, Cursor/Grok, and `qa`, and does not perform their work
- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer (Cursor/Grok) - implements one groomed task; follows _docs/team/engineer.md
- QA - verifies the issue; follows _docs/team/qa.md

Lifecycle

Active backlog label: `showcase`.

For issue selection:

- Consider only open issues with the active backlog label, without `draft`, whose
  listed dependencies are closed.
- If multiple issues are eligible, choose the lowest issue number.
- If the selected issue is blocked or conflicts with `_docs/decisions.md`, stop
  and report it.
- Stop when no eligible open issues remain.

1. Pick the next eligible issue using the rule above
2. PM grooms it
3. Orchestrator sends the groomed issue (and any QA `FAIL` on retries) via
   `.tools/cursor-grok --prompt-file`. The prompt directs Cursor/Grok to follow
   `_docs/team/engineer.md`. Orchestrator reviews the changes and runs verification
4. QA verifies it
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, QA marks every acceptance criterion as checked in the issue body,
   then closes the issue
7. Repeat until no eligible open `showcase` issues remain

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code. On PASS, QA updates only the acceptance-criteria
  checkboxes and closes the issue
