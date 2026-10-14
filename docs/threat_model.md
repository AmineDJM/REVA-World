# Threat model (V1)

Scope: private single-user deployment on Render (one API, one worker, managed
PostgreSQL, external S3-compatible bucket), public-aggregate data only.

## Assets

1. Integrity of scientific history (evidence, revisions, releases, model versions).
2. Credentials: migration owner, three runtime role passwords, S3 keys (API
   read-only, worker prefix-scoped write), session secret, admin hash, optional
   provider/LLM keys.
3. Availability of the last accepted releases.
4. The operator's session.

## Trust boundaries

| Boundary | Control |
|---|---|
| Internet → API | Render TLS edge; trusted hosts; Argon2id login with rate limiting and audit; HttpOnly/Secure/SameSite session + CSRF; scoped hashed API tokens; bounded queries; no SQL/GraphQL/code execution |
| Render proxy → Uvicorn | `--proxy-headers` trusted only from loopback and private ranges (`REVA_FORWARDED_ALLOW_IPS`) |
| Process → database | separate NOINHERIT logins; grants per role (0007); append-only triggers; RLS on shared provenance tables; SECURITY DEFINER functions with fixed `search_path` |
| Deploy → runtime | owner URL exists only in the API service env for pre-deploy and is removed from the child environment; worker refuses it in production |
| Worker → internet | SafeHttpClient: HTTPS allowlist per connector, DNS-resolved address checks, redirect re-checks, size/time/decompression limits |
| Documents → system | retrieved text is data; tool allowlists and policies live outside content; prompt-injection strings are inert |
| Autonomy → production | template-only; generated code never executed in the worker; gates evaluated separately; policy digests pinned |

## Threats and mitigations

| Threat | Mitigation | Residual risk |
|---|---|---|
| Owner credential leak via runtime process (env dump, error page, crash report) | allowlisted child environments; owner URL never forwarded; redacted startup errors; tests on the real child environment | the Render dashboard and the API service's own env still hold it (pre-deploy needs it); protect the Render account with MFA |
| Runtime role rewrites history | no UPDATE/DELETE/TRUNCATE grants on scientific tables; triggers reject mutation even for the owner | a superuser or the owner disabling triggers; restricted to migrations |
| Simulation writes beliefs/evidence | `reva_sim` grants + RLS; `belief.input` FKs exclude sim results | shared worker process boundary is accidental-write isolation, not a sandbox against malicious code |
| SSRF from connectors | allowlist, literal-IP refusal, post-resolution public-address check, per-redirect check, limited redirects | **DNS-rebinding TOCTOU**: the address check resolves the host before the request, and the HTTP library resolves again when connecting. An attacker controlling an allowlisted domain's DNS could answer a public address to the check and a private one to the connection. Mitigations: hosts are a short allowlist of official statistics domains; redirects are re-checked; no secret-bearing headers are sent to providers except the optional Comtrade key to its fixed host; link-local/metadata ranges are part of the blocked set. Complete fix (planned): connect to the pre-validated IP with SNI/Host set to the name (custom transport) or route egress through a filtering proxy. When an outbound proxy is configured (`HTTPS_PROXY`), the proxy resolves names and must enforce its own private-range policy. |
| Zip bombs / oversized responses | streamed size limits, decompression ratio limits, safe archive extraction | CPU spent on bounded inputs |
| SQL injection | parameterised SQL everywhere; identifiers via `format('%I')`/`sql.Identifier` | none known |
| Prompt injection in papers/catalogues | content never changes policies/allowlists; LLM disabled by default | future LLM features must keep the separation |
| Malicious generated code | template-only autonomy; sandbox interface blocked unless an external isolated runner is configured | none in V1 (no execution) |
| Session theft / CSRF | HttpOnly Secure SameSite cookies, CSRF header, short-lived signed sessions, login rate limit | XSS in the UI would bypass CSRF; security headers and React escaping are the defence |
| Object tampering | content addressing; sha256 verified on read; inventory in backups | worker key can delete objects (GC); mitigated by bucket versioning in a separate admin account |
| Backup theft | age encryption required in production; backups stored outside the production account | identity file custody |
| Supply chain | uv.lock / package-lock.json, base images pinned by digest, CI actions pinned by commit SHA, no build tools in the runtime image | upstream compromise of a pinned artifact before pinning |
| Resource exhaustion | one worker, explicit thread budget (never `os.cpu_count()`), statement timeouts, bounded pages/graphs, budgets | single-instance availability |

## Out of scope for V1

Restricted/sensitive microdata, multi-user tenancy, hostile code execution,
high availability. Each requires its own design and review before it is enabled.
