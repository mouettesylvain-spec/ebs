---
paths:
  - "tests/**"
---
# Rules for tests

- Follow docs/design/testing.md. Put `# R<n>` above each test that covers a task requirement.
- Unit tests: no network, no real `$HOME`, no sleeps (use `FakeClock` / `tests/helpers/wait.py`),
  everything under `tmp_path`.
- Prefer the shared fakes (InMemoryMetadataStore, FakeSlurm, fake EDA tools, tmp CAS) over mocks.
  Mock only process/network boundaries.
- Don't loosen an assertion, add `skip`, or mark `xfail` to get green. If a test is wrong, fix it and
  explain why in the commit body.
- Fixtures must be synthetic: no real project RTL, logs, hostnames, license servers or vendor text.
