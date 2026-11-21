# Plan deviations: Muninn first release

A chronological record of human-directed implementation changes that depart from the approved plans.


## Source plans

- Design plan: `.artifacts/20260804--muninn-conversation-context/design-plan.md`
- Implementation plan: `.artifacts/20260804--muninn-conversation-context/implementation-plan.md`


## Deviations

### 1. Use AssemblyAI instead of Zen

#### Stage

Execution


#### Human feedback

“Let's use AssemblyAI. I've set up an account, and have an API key for it.”


#### Approved plan reference

The approved design names Zen as the first-release external AI provider. Implementation
plan Task 05 specifies a Zen v1 adapter and Task 10 specifies Zen deployment values.


#### Change made

The worker now uses AssemblyAI for transcription and its LLM Gateway for summaries. Transcription and summary model IDs
are configurable. The API exposes the current transcript to its owner, and the web review screen displays timestamped
speaker segments. The key is injected through an external Kubernetes Secret rather than chart values.

Files changed include the API provider, configuration, worker, transcript route, relevant tests, Helm
values/templates, DevSpace profile, API/release contracts, and web transcript client/view/tests.


#### Reason

The human selected AssemblyAI after discussing providers that offer both transcription and chat, and stated
that an account and API key were available. No additional rationale was provided.


#### Verification

- API unit tests: 177 passed.
- API type and lint checks: passed.
- Web unit tests: 11 passed; typecheck and lint passed.
- Playwright E2E: 11 passed.
- Helm lint and homelab template rendering: passed.
- The live AssemblyAI API was not called, and no deployment was run. The API key still
  needs to be added to the local Kubernetes Secret before a live transcription test.
