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
  native agents and relay agents, and does not perform their work
- PM - prepares new issues or grooms existing ones before implementation;
  follows _docs/team/pm.md
- Engineer - implements one groomed task; follows _docs/team/engineer.md
- QA - verifies the issue; follows _docs/team/qa.md

Agent profiles

- Routine work uses the visible native relay agents `pm-routine`,
  `engineer-routine`, and `qa-routine`; each launches the corresponding
  Cursor/Grok worker through `.tools/cursor-relay`.
- Complex work uses the direct native agents `pm`, `engineer`, or `qa`.
- `cursor-relay` is the shared external launcher and may also be invoked
  directly when the relay agent itself is explicitly requested.

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
2. Orchestrator delegates PM grooming to `pm-routine` (or `pm` for complex
   work); it reviews the proposed issue update and applies it if the agent's
   GitHub write is denied
3. Orchestrator sends the groomed issue (and any QA `FAIL` on retries) to
   `engineer-routine` (or `engineer` for complex work). It reviews the changes
   and runs verification
4. `qa-routine` (or `qa` for complex work) verifies it
5. On FAIL, back to step 3 with the QA comment as input
6. On PASS, QA marks every acceptance criterion as checked in the issue body,
   then closes the issue
7. Repeat until no eligible open `showcase` issues remain

For new task intake, PM prepares the title, body, and labels through
`pm-routine` (or `pm` for complex work). The orchestrator reviews them and
posts the issue if the agent's GitHub write is denied; it does not re-groom the
PM's work.

Rules

- Do not skip step 2
- The engineer does not close the issue
- QA does not fix the code. On PASS, QA updates only the acceptance-criteria
  checkboxes and closes the issue
