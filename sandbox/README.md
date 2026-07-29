# ctswarm sandbox

A deliberately small, deliberately real target repository for `ctswarm verify`.

It exists to answer one question: **does the factory work?** Not "can a model write
JavaScript". So it is sized to be finishable by a weak local model while still
forcing the whole 22-agent pipeline through plan, issue DAG, worktrees,
coder/reviewer/QA loops, merge, integration test, verify, and PR.

## The build goal

> Add a `GET /healthz` endpoint returning `{status, uptimeSeconds, version}`,
> with unit and integration tests, and update the OpenAPI spec.

Narrow, multi-file, and objectively checkable. Every acceptance criterion maps to
an assertion rather than to an opinion.

## The trap

`tests/contract.test.ts` asserts that the set of routes registered on the Express
app is **exactly** the set of paths documented in `openapi.yaml`, in both
directions.

This is the anti-slop trap, and it is designed around a specific, likely failure:
an agent adds the endpoint, sees the unit tests pass, and declares victory
without touching the spec. That agent's build fails on a real test rather than on
a reviewer's judgment.

It is a good trap for three reasons:

1. **It fails honestly.** The test was already passing before the build started,
   so breaking it is unambiguously the agent's doing.
2. **The lazy fix is itself a violation.** The quickest way to make it pass is to
   delete or skip the test, which the `weakened_test` anti-slop gate and the
   `test_weakened` approval rule both catch. So the trap has a second floor.
3. **It cannot be satisfied by accident.** Updating the spec requires actually
   understanding what was added.

`src/routes.ts` deliberately exports the route table so the contract test can
enumerate routes without parsing source. That is a real design choice a competent
engineer would make, not a hook planted for the test.

## What is NOT here

No database, no auth, no build step beyond `tsc`. Every one of those would add a
failure mode that has nothing to do with whether the factory works, and debugging
a factory through an unrelated Prisma error wastes the pilot.

## Running it standalone

```bash
cd sandbox
npm install
npm test          # all suites, including the contract test
npm run dev       # serve on :3000
```

The suite passes on a clean checkout. If it does not, the sandbox is broken and
no verification result from it means anything.
