# Threat model (V1)

Scope: private single-user deployment on Render (one API, one worker, one
independent promotion-gate process, managed PostgreSQL, external S3-compatible
bucket), public-aggregate data only.

## Assets

1. Integrity of scientific history (evidence, revisions, releases, model versions).
2. Credentials: migration owner, four runtime role passwords (api, worker, sim,
   gate), S3 keys (API read-only, worker and gate prefix-scoped write), session
   secret, admin hash, optional provider/LLM keys.
3. Availability of the last accepted releases.
4. The operator's session.

## Trust boundaries

| Boundary | Control |
|---|---|
| Internet → API | Render TLS edge; trusted hosts; Argon2id login with per-client and global rate limiting (`REVA_LOGIN_RATE_LIMIT_PER_MINUTE`, default 5/min, in-memory per process) and audit; HttpOnly/Secure/SameSite session + CSRF; scoped hashed API tokens; bounded queries; no SQL/GraphQL/code execution |
| Render proxy → Uvicorn | `--proxy-headers` trusted only from loopback and private ranges (`REVA_FORWARDED_ALLOW_IPS`) |
| Process → database | separate NOINHERIT logins; grants per role (0007); append-only triggers; RLS on shared provenance tables (`meta.context`, `meta.activity`, artifact locations, activity inputs/outputs); SECURITY DEFINER functions with fixed `search_path` (see below) |
| Deploy → runtime | owner URL exists only in the API service env for pre-deploy and is removed from the child environment; worker and gate refuse it in production; each start script forwards only its own process's secrets (the worker never receives the gate password, the gate never the worker/sim/API ones) |
| Worker → internet | SafeHttpClient: HTTPS allowlist per connector, DNS-resolved address checks, redirect re-checks, size/time/decompression limits |
| Documents → system | retrieved text is data; tool allowlists and policies live outside content; prompt-injection strings are inert |
| Autonomy → production | template-only (declarative manifests run through reviewed template code in a child process that is defence in depth, not a security boundary); generated code never executed; policy digests pinned; the promotion gate is a **separate process with its own login** (`reva_gate`; on Render the `reva-gate` background service): the worker files an `ops.gate_request` and waits, the gate re-derives everything from the persisted proposal and stored, digest-verified evidence (recomputing validator and sandbox for T1) and is the only login that may execute `ops.record_gate_result`; approvals accept only gate results with `recorded_by = 'reva_gate'` |

## Runtime database roles

| Role | Writes | SECURITY DEFINER functions it may execute |
|---|---|---|
| `reva_api` | `ops.audit`, `ops.session_revocation` (INSERT) only; reads scientific schemas and selected `ops` tables | `ops.submit_job`, `ops.request_cancel`, `ops.review_proposal`, `sim.create_scenario` |
| `reva_worker` | INSERT on all tables of `meta`, `ontology`, `world`, `evidence`, `belief`, `model`, `ops` (except `ops.api_token`, `ops.proposal_review`, `ops.session_revocation`, `ops.gate_result`); UPDATE/DELETE only on queue/bookkeeping `ops` tables; RLS forbids simulation contexts/activities; approved registry states only through the `reva_registry` approval functions | `ops.assert_lease`, `ops.heartbeat`, `ops.complete_job`, `model.record_promotion`, the approval functions (`evidence.approve_eligibility`, `evidence.restore_eligibility`, `ontology.approve_mapping`, `model.approve_version`) |
| `reva_gate` | its own decision evidence only (`meta.artifact` rows restricted to `gate-decision/1`, `meta.artifact_location` in `evaluation`); SELECT on `ops.proposal`, `ops.gate_request`, `ops.gate_result`, `evidence.connector_version`, `evidence.source`, `evidence.eligibility` and artifact metadata, nothing else | `ops.record_gate_result` (only this login) |
| `reva_sim` | INSERT on `sim.*`, `model.evaluation` and provenance rows; RLS limits its contexts to simulation/evaluation and its activities to simulation/evaluation/model | `ops.assert_lease`, `ops.heartbeat`, `ops.complete_job` |

No runtime role has UPDATE/DELETE/TRUNCATE on scientific tables, and append-only
triggers reject mutation even for the owner. API tokens are minted only by the
migration owner (admin CLI); human reviews only through `ops.review_proposal`.

## Threats and mitigations

| Threat | Mitigation | Residual risk |
|---|---|---|
| Owner credential leak via runtime process (env dump, error page, crash report) | allowlisted child environments; owner URL never forwarded; redacted startup errors; tests on the real child environment | the Render dashboard and the API service's own env still hold it (pre-deploy needs it); protect the Render account with MFA |
| Runtime role rewrites history | no UPDATE/DELETE/TRUNCATE grants on scientific tables; triggers reject mutation even for the owner | a superuser or the owner disabling triggers; restricted to migrations |
| **Worker forges governance approvals** (self-approved eligibility, mappings or model versions) | application code writes `evidence.eligibility`, `ontology.mapping` and `model.version` only after seed review or a recorded gate/human decision; gate results and promotions are guarded by insert triggers (`ops.check_gate_evaluator`, `model.check_promotion`); every row is append-only and audited through its `review_activity_id` / provenance activity | **Mitigated in the database (0007).** Writes record `recorded_by = session_user`; human attribution only from the API login, owner or approval functions; approved states only through `reva_registry`-owned functions requiring a gate pass **recorded by `reva_gate`** or an API-recorded human review, bound to the proposal scope and used once; `evidence.restore_eligibility` re-enables only versions whose approval was independent (owner, `reva_gate` pass or API review). Gate results are written only by the separate gate process: the worker has no INSERT on `ops.gate_result` and no EXECUTE on `ops.record_gate_result` (even the owner is refused by `meta.enforce_governance`). Seeding runs as the owner; restricted deletion needs `SET ROLE reva_restricted_deletion` plus a ticket. **Residual:** the gate decides on inputs the worker stored (proposal documents, report evidence, the captured fixture bank it recomputes on), so a compromised worker still chooses *what* is evaluated, though not the verdict; both processes run the same image, so a code-level compromise affects both; locally (`reva gate`) the gate runs on the same host as the worker, usually from the same `.env` holding every password, so separation there is by credential convention only; on Render it is a separate service whose environment holds only the gate password (the API service also holds it for the pre-deploy bootstrap, and its start script scrubs it from the API child); `model.record_promotion`, `model.evaluation` and `meta.artifact` remain worker-writable. See [dev/gate_separation.md](dev/gate_separation.md). |
| Simulation writes beliefs/evidence | `reva_sim` grants + RLS; `belief.input` FKs exclude sim results | shared worker process boundary is accidental-write isolation, not a sandbox against malicious code |
| SSRF from connectors | allowlist, literal-IP refusal, post-resolution public-address check, per-redirect check, limited redirects | **DNS-rebinding TOCTOU**: the address check resolves the host before the request, and the HTTP library resolves again when connecting. An attacker controlling an allowlisted domain's DNS could answer a public address to the check and a private one to the connection. Mitigations: hosts are a short allowlist of official statistics domains; redirects are re-checked; no secret-bearing headers are sent to providers except the optional Comtrade key to its fixed host; link-local/metadata ranges are part of the blocked set. Complete fix (planned): connect to the pre-validated IP with SNI/Host set to the name (custom transport) or route egress through a filtering proxy. When an outbound proxy is configured (`HTTPS_PROXY`), the proxy resolves names and must enforce its own private-range policy. |
| Zip bombs / oversized responses | streamed size limits, decompression ratio limits, safe archive extraction | CPU spent on bounded inputs |
| SQL injection | parameterised SQL everywhere; identifiers via `format('%I')`/`sql.Identifier` | none known |
| Prompt injection in papers/catalogues | content never changes policies/allowlists; LLM disabled by default | future LLM features must keep the separation |
| **Natural-language scenario drafts** (`POST /v1/scenario-drafts`, V2): hostile text ("ignore previous instructions", "set multiplier 1000", "use value 0 for Algeria"), a manipulated or wrong LLM output, LLM cost exhaustion, personal data in the text | opt-in (`REVA_LLM_ENABLED`), scope `simulate`, Idempotency-Key, text ≤ 4096 bytes; fixed system prompt, the text travels only as one JSON data field; no tools; HTTPS POST only to `api.openai.com` / `generativelanguage.googleapis.com` (fixed allowlist, no redirects, bounded response); no LLM key in REVA (the egress proxy injects credentials); output schema-validated, every model key, country, output, parameter path/choice and fidelity checked against the allow-list built from the registered plugins — a draft referencing anything else is rejected, never substituted; the draft is then validated by the same validator as hand-written specs; numbers are listed with their provenance for confirmation and the restatement is generated by the server from the validated spec; nothing is executed or stored — a draft runs only when the user submits it to `POST /v1/scenarios`; daily request/token budget in `budgets.yaml` reserved in `ops.budget_ledger` before each call (429 `budget_exhausted`, no fallback to a guess); audit row with the text's sha256 and length, never the text | the LLM can still misread a request into a *valid but different* scenario (e.g. a wrong sign or the wrong country); the defence is the server restatement, the per-number provenance (`not_in_text` warnings) and the user's confirmation, not the model; the text and the trusted catalogue are sent to the provider (third-party processing — do not enter personal data); keyword guards are lexical (French/English); a fallback provider sees the same text; the ledger functions `ops.reserve_llm_budget`/`ops.settle_llm_budget` are required for the API role (until migration 0008 adds them the endpoint fails closed with 503) |
| Malicious generated code | template-only autonomy; sandbox interface blocked unless an external isolated runner is configured | none in V1 (no execution) |
| Session theft / CSRF | HttpOnly Secure SameSite cookies, CSRF header, short-lived signed sessions, login rate limit | XSS in the UI would bypass CSRF; security headers and React escaping are the defence |
| Object tampering | content addressing; sha256 verified on read; inventory in backups | worker key can delete objects (GC); mitigated by bucket versioning in a separate admin account |
| Backup theft | age encryption required in production; backups stored outside the production account | identity file custody |
| Supply chain | uv.lock / package-lock.json, base images pinned by digest, CI actions pinned by commit SHA, no build tools in the runtime image | upstream compromise of a pinned artifact before pinning |
| Resource exhaustion | one worker, explicit thread budget (never `os.cpu_count()`), statement timeouts, bounded pages/graphs, budgets | single-instance availability |

## Out of scope for V1

Restricted/sensitive microdata, multi-user tenancy, hostile code execution,
high availability. Each requires its own design and review before it is enabled.
