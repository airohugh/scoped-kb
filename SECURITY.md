# Security

This is a personal, unpaid side project. There is **no guaranteed response time** — please don't expect SLA-style handling.

## Reporting a vulnerability

Please use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) on this repository instead of opening a public issue. This lets us discuss and fix the issue before it's public.

If private reporting isn't enabled or doesn't work for you, open a regular issue describing the problem in general terms (no exploit details, no working proof-of-concept) and ask for a private channel.

## Scope notes

- This project talks to a Postgres/pgvector instance and an Ollama server, both expected to run on `127.0.0.1` under your control. It is not designed to be exposed to untrusted networks or multi-tenant hosting as-is.
- The access-scoping layer (`scope-policy.json` / collections / profiles) assumes the machine itself and the people with write access to your notes directories are trusted. See `docs/design.md` for the exact threat model it does and doesn't cover.
