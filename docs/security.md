# Security model and deployment checklist

## Trust boundary

MVP1 is a self-hosted, single-node data plane. The REST API is a control plane and never receives
vectors or resolved database credentials. The worker is the only component that resolves secrets
and reaches vector databases. Workspace administrators are trusted to define migrations, but their
choices are still constrained to configured adapters, filesystem roots, and endpoint allowlists.

This release is not certified as an Internet-facing public SaaS or a hostile multi-tenant service.
Put it on a private network behind a TLS reverse proxy or ingress. Use an egress firewall in
addition to the application allowlist; application URL validation alone cannot completely remove
DNS rebinding and dependency-level request risks.

## Implemented controls

- Authentication is mandatory off loopback. Static bearer tokens require at least 32 characters;
  OIDC validates an HTTPS JWKS endpoint, signature, fixed asymmetric algorithms, issuer, audience,
  expiry, issued-at time, subject, workspace, and role claims.
- Viewer/operator/admin authorization is applied at every resource or action route, and every
  database lookup includes the authenticated workspace.
- OpenAPI documentation is disabled by default. Host headers are allowlisted and API responses use
  no-store, anti-framing, MIME-sniffing, CSP, and referrer security headers.
- Request bodies are limited to 1 MiB by default, mutating request bodies require JSON, list routes
  are bounded, and the bundled server limits concurrent requests and keep-alive duration.
- Only `chroma` and `qdrant` are enabled by default. Network endpoints must appear as an exact
  `host:port` or `CIDR:port` entry in `VME_ENDPOINT_ALLOWLIST`. TLS is required unless an operator
  explicitly enables insecure endpoints. Embedded database paths must remain under
  `VME_DATA_ROOTS`.
- Connection dictionaries use strict adapter-specific field allowlists. Credential-looking fields,
  credential-bearing URLs, unapproved environment references, plaintext Chroma headers, and secret
  material in migration/resource dictionaries are rejected before persistence.
- Durable configuration stores only `env:` or `file:` references. Environment references must be
  named in `VME_SECRET_ENV_ALLOWLIST`; file references must remain below
  `VME_SECRET_FILE_ROOTS` and are limited to regular files no larger than 64 KiB.
- Resolved secret values are tracked only for the active operation and removed from provider error
  messages before checkpoint, event, job, or plan persistence. API responses recursively redact
  secret references, and idempotency keys are SHA-256 hashed before storage.
- SQLite state files use mode `0600` on POSIX. The container runs as UID/GID 10001 with a read-only
  root filesystem, all Linux capabilities dropped, `no-new-privileges`, PID/memory limits, and
  separate API and worker processes.

These controls follow the defense-in-depth guidance in the
[OWASP SSRF Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html),
[OWASP REST Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html),
and [OWASP Secrets Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html).

## Required production configuration

```text
VME_AUTH_MODE=oidc
VME_OIDC_ISSUER=https://identity.example/tenant
VME_OIDC_AUDIENCE=vector-migration-engine
VME_OIDC_JWKS_URL=https://identity.example/tenant/keys
VME_ALLOWED_HOSTS=vme.internal.example
VME_ENDPOINT_ALLOWLIST=chroma.internal.example:443,qdrant.internal.example:6333
VME_DATA_ROOTS=/data
VME_SECRET_ENV_ALLOWLIST=QDRANT_API_KEY,CHROMA_TOKEN
VME_SECRET_FILE_ROOTS=/run/secrets
VME_HSTS=true
```

For local token mode, generate at least 32 random bytes, keep the token out of shell history, inject
it through the deployment secret mechanism, and rotate it if it may have been exposed. Never place
credentials in an idempotency key, URL, migration resource, command line, image, source file, or
log message.

## Infrastructure controls still required

- Terminate TLS with a managed certificate and restrict ingress to operator networks. Do not enable
  `VME_ALLOW_INSECURE_ENDPOINTS` outside an isolated development environment.
- Enforce the same destination allowlist with firewall/Kubernetes NetworkPolicy rules, explicitly
  denying cloud metadata, loopback, link-local, cluster management, and container-runtime APIs.
- Apply rate limits at the reverse proxy/ingress. The bundled server has body and concurrency limits
  but intentionally does not treat per-process in-memory rate limiting as an HA security boundary.
- Inject provider credentials only into the worker, use separate least-privilege source and
  destination identities, prefer short-lived credentials, and rotate/revoke them through the
  external secret manager.
- Encrypt the state volume and backups at rest, restrict backup readers, and test restore and secret
  rotation procedures. SQLite does not provide application-level encryption.
- Pin and scan the final image and Python dependency lock in the release pipeline. The development
  `pyproject.toml` uses compatible version ranges and is not a reproducible supply-chain lockfile.
- Forward audit events and authentication failures to tamper-resistant monitoring. Alert on unusual
  profile creation, repeated authentication failures, queue growth, and migration volume.

## Chroma dependency advisory

The full `chromadb` package through 1.5.9 is affected by critical
[CVE-2026-45829 / GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c),
and no patched full-package version was listed when this review was completed. The production VME
image therefore installs Chroma's official HTTP-only `chromadb-client` package and disables
embedded Chroma paths by default. The `chroma` optional dependency remains solely for isolated
embedded integration testing; it must not be used to launch a Chroma server or be included in a
production image until a patched release is available. Keep any remote Chroma server private,
authenticated, and independently assessed before migration.

## Incident response

If a secret may have leaked, stop the affected worker, revoke or rotate the upstream credential,
replace the mounted/environment value, review job and ingress audit history, and resume only after
confirming the destination allowlist and migration definition. Deleting VME state does not revoke a
provider credential.
