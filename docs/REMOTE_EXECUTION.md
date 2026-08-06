# Remote execution and full routing control

Status: product requirement; not yet implemented.

## Requested outcome

Mission Control must let an operator choose where agents execute and how every
role is served. A build can run on the local machine, on a configured VPS, or on
a deliberate mix of execution targets. Agent placement is independent from model
placement: agents running on a VPS may still use models hosted on the local
machine, or local models may be disabled entirely.

The default remains the current safe path: local execution, capacity-aware
harness selection, local OpenCode models for implementation, and subscription
capacity for planning and independent acceptance when available.

## Configuration model

### Execution targets

An execution target is a named, pre-validated profile rather than an arbitrary
hostname entered at launch time.

- `local`: the existing Docker and AgentField stack.
- `vps`: an enrolled remote runner with a stable identity, labels, capacity,
  health, workspace root, and concurrency limit.
- `hybrid`: a per-work-category assignment across enrolled targets.

Each target reports operating system, architecture, CPU, memory, disk, optional
accelerator, active executions, queue depth, installed harnesses, and last health
check. Credentials, private keys, tokens, raw environment variables, and command
arguments never appear in the browser response.

### Work placement

The launch flow and a durable defaults page must support assignments for:

| Work category | Example roles |
|---|---|
| Planning | product manager, architect, tech lead, sprint planner, issue writer |
| Implementation | coder, QA, integration tester, CI fixer |
| Review and acceptance | code reviewer, QA synthesizer, final verifier |
| Repository operations | git initialization, merger, finalizer, PR publisher |

Every category can select:

1. execution target (`local`, a named VPS, or `auto`);
2. harness (`OpenCode`, `Claude Code`, `Codex`, or `auto`);
3. provider/backend and concrete model, or a ctswarm virtual tier;
4. fallback order;
5. concurrency, timeout, and spend/quota ceiling.

The resolved assignment must be shown before launch and stored with the build so
history always explains where a role ran and which harness/model served it.

### Local-model control

Local model use is an explicit policy:

- **Preferred:** use eligible local models first and fall back according to the
  selected policy.
- **Allowed:** use local models only when the chosen hosted/subscription route is
  unavailable.
- **Disabled:** exclude Ollama, MLX, and LM Studio from every route and reject the
  launch if the remaining assignments cannot serve all required roles.

When a VPS-hosted agent uses a model on the local machine, traffic must traverse
an authenticated private network or tunnel to the ctswarm router. Ollama and
other raw model servers must not be exposed directly to the public internet.
The router continues to enforce model eligibility, circuit breakers, context
limits, cancellation checks, and audit logging.

## Mission Control surfaces

### New swarm

- Add an **Execution** section after repository selection.
- Start with a concise mode choice: `Use defaults`, `Local`, `VPS`, or `Custom`.
- Selecting `VPS` reveals enrolled healthy targets and their current capacity.
- Selecting `Custom` opens the four-category placement matrix.
- Provide one prominent local-model policy control with a plain-language impact
  summary.
- Show a read-only resolved-plan summary before the Start button, including
  target, harness, model route, fallback, and any unavailable assignment.

### Infrastructure

- List enrolled runners, health, capabilities, versions, active work, and last
  contact.
- Support enrollment, drain, disable, test connection, and removal behind an
  explicit owner approval boundary.
- Never return enrollment secrets after creation.

### Models and routing

- Extend the existing work-category policy with execution target and harness.
- Keep execution placement visually separate from model placement so operators
  do not mistake “agent runs on VPS” for “model runs on VPS.”
- Preview the exact resolved provider/model and any network hop to a local router.

### Build detail

- Every execution lane shows runner, harness, provider/backend, and model.
- Build summary records the immutable launch configuration and subsequent
  operator-approved changes.
- Loss of a runner or model route appears as a specific blocking or failover
  event, not a generic stalled state.

## Security and operational constraints

- Enroll remote runners with short-lived, scoped credentials and an auditable
  owner approval.
- Prefer outbound runner registration over inbound public administration ports.
- Isolate each build checkout and its credentials on the selected runner.
- Scope repository and MCP credentials to the roles and target that require them.
- Encrypt control, artifact, and model traffic in transit.
- Validate host keys or runner identities; never offer “accept any host key.”
- Draining a target prevents new work and allows current work to finish.
- Stopping a build cancels its complete cross-target AgentField workflow tree.
- A disconnected target cannot be shown as available or silently selected by
  `auto` placement.

## Acceptance criteria

1. An operator can enroll and health-check a VPS without exposing secrets in UI
   responses or logs.
2. New Swarm can select local, a named VPS, or custom per-category placement.
3. Harness and model routing remain independently configurable for every work
   category.
4. Local models can be preferred, fallback-only, or fully disabled.
5. A VPS agent can use the authenticated local ctswarm router without exposing a
   raw local model server publicly.
6. Invalid or unavailable combinations are rejected before submission with an
   actionable explanation.
7. Build history and live traces show the resolved runner, harness, backend, and
   concrete model for every execution.
8. Target drain, disconnect, timeout, and cancellation behavior is deterministic
   and covered by integration tests.
9. Browser verification covers desktop and mobile launch configuration without
   hidden controls, overflow, ambiguous defaults, or inaccessible inputs.
10. Existing local-only builds remain compatible and require no new setup.

## Delivery sequence

1. Define and persist execution-target profiles, health, and immutable build
   placement policy.
2. Enroll a remote AgentField runner and prove isolated repository execution.
3. Add secure VPS-to-local-router connectivity and local-model policy modes.
4. Extend the scheduler submission contract and observability trace schema.
5. Build the Impeccable-shaped Mission Control configuration surfaces.
6. Verify failover, drain, disconnect, cancellation, and a complete remote
   branch-and-PR proof before making remote execution generally selectable.
