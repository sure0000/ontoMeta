# ontoMeta Project Memory

This file is the project-level working memory for coding agents. It is a
distilled, reviewable version of the Claude Code project memory found under
`~/.claude/projects/-Users-me-Documents-ontoMeta/memory/`. The code, tests, and
current branch win over any historical note here.

## Current Repository State

- At the time this memory was written, the active branch was `v3`; treat the
  current checkout and explicit user direction as authoritative for branch
  selection.
- The latest MCP commits are `8a04e46` (Phase 2/3), `febde0c` (Phase 4),
  `fa31f86` (Phase 5 partial), and `83047c8` (MCP management-page styling).
- The working tree may contain user/generated changes. In particular,
  `frontend/tsconfig.tsbuildinfo` and `backend/data/` are local artifacts and
  must not be included in commits unless explicitly requested.
- `backend/ontometa.db` is demo/seed data, not representative production
  data. Real DataHub/ERPNext behavior must be checked separately.

## Project Invariants

### Source of truth and configuration

- The ontology is the primary source of truth. Physical warehouse tables are
  rebuildable projections of ontology objects and relationships, not the
  modeling authority.
- Runtime business and connection configuration is managed in the Web settings
  UI and stored in the database. Do not add new runtime configuration reads
  from environment variables or `settings` fields. Add configurable fields to
  the self-describing dependency/settings schema and expose them through the
  existing `SettingsService` runtime readers.
- Bootstrap-only environment values are the narrow exception: database URL,
  admin token, API-key pepper, debug/CORS, and deployment/bootstrap paths.
- Credentials never enter LLM prompts or MCP tool results. Resolve saved
  connection information through the existing settings/data-source services;
  do not infer hosts, ports, or credentials from unrelated fields.

### Modeling and execution

- Business objects, relations, and properties use the real model names and
  service projections. Do not assume fields from design documents such as
  `ObjectType.role`, `RelationType.source_object_id`, or synthetic task model
  classes.
- Read-side code should reuse existing services (`OntologyQueryService`,
  `AgentPipelineService`, `data_app_executor`, landing/projection helpers) so
  derived fields, published-only rules, and live readiness reconciliation are
  preserved.
- A task-produced physical table is a landing/projection, not a new ontology
  object. Change of layer alone does not create a new object; a changed
  semantic grain may create a derived object.
- Every task follows the six-ring confirmation flow: requirement, ontology,
  data, execution plan, execution, result. Agent/MCP proposals must not bypass
  human confirmation or write directly to execution tables.
- Inferred properties such as semantic type, primary key, or relationship are
  evidence, not unquestionable facts. Do not turn uncertain inference into a
  hard constraint that can make source data unloadable.
- Business relations are gated by object roles. Fact/action/verb tables are
  generally relationship/bridge evidence, while isolated tables should not be
  promoted to business objects without review.

## MCP Implementation Memory

### Delivered at current HEAD

The MCP service is in `backend/app/mcp/` and currently has 16 tools:

- Query: `query_ontology`, `query_objects`, `query_object_detail`,
  `query_relations`, `list_datasources`
- SQL: `execute_sql`, `validate_sql`
- Tasks: `list_tasks`, `get_task_status`
- Proposals: `propose_sync`, `propose_transform`, `propose_materialize`,
  `propose_metric`
- Operations: `server_info`, `get_mcp_stats`, `list_audit_logs`

Phase 1 through Phase 4 are complete. Phase 5 is partial: remote Streamable
HTTP transport and the frontend MCP management tab are implemented. The
remaining work is listed below; do not describe Phase 5 as fully complete.

### MCP rules that must not regress

- The MCP dependency lower bound is `mcp>=2.1.0`; the server uses the SDK's
  callback-style API. Keep the dependency and SDK API aligned.
- Every tool is registered by import side effect through
  `backend/app/mcp/tools/__init__.py`. Adding a tool requires adding the module
  import and the expected-tool coverage.
- Tool failures must set MCP `CallToolResult.is_error=True`; a JSON body with
  `success: false` alone is still reported as a successful MCP call.
- Read-only SQL validation must reuse
  `app.services.data_app_executor.is_read_only`, and SQL execution must use
  the existing fail-closed warehouse resolver/executor. Do not create a
  second keyword blacklist or a fictional `QueryGateway`.
- Proposals are read-only previews. They must call the real drafter/registry
  path and governance validation, return `draft_payload`, and not insert a
  `GovernanceArtifact`. The frontend/REST confirmation flow owns persistence.
- All task kinds share `GovernanceArtifact` with `kind` distinguishing them;
  do not introduce `SyncTask`/`TransformTask`/`MaterializeTask`/`MetricTask`
  ORM classes merely to satisfy a design document.
- Authentication reuses `app.auth.resolve_principal_token` and the existing
  four-level Principal RBAC (`reader < editor < reviewer < publisher`). Do not
  implement the five-role `rbac.py`/`User` design from the old security draft;
  those types do not exist in this repository.
- stdio is one process/one identity. `ONTOMETA_MCP_TOKEN` is resolved at
  startup; no token falls back to `mcp_default_role` (normally `reader`).
  Authorization is centralized in the MCP server and fails closed. `execute_sql`
  uses the same minimum role as Data Agent `run_sql`.
- MCP audit is append-only, arguments are redacted, and audit failures must
  never break the primary tool call.
- Rate limiting is an in-process sliding window. It runs before authorization,
  counts only allowed calls, gives `rate_limited` a distinct meaning from
  `denied`, and deduplicates rate-limit audit rows per tool/window.
- Remote HTTP is disabled by default (`mcp_http_enabled=False`). When enabled,
  use `/mcp/` with the trailing slash, parse Bearer auth per request, and keep
  anonymous HTTP access disabled unless explicitly configured. Do not reuse a
  stdio session identity for HTTP.
- The shared MCP introspection layer is the source for both MCP monitoring
  tools and `/api/mcp/*`; avoid duplicating tool catalogs or audit aggregation.

### MCP work still outstanding

1. Resource-level permissions for allowed domains/data sources.
2. Modeling tools (`infer_ontology_from_datahub`,
   `classify_business_objects`, `infer_relationships`, `validate_ontology`),
   with an explicit asynchronous and human-confirmation design.
3. Read-only lineage, landing, and operational-record tools:
   `get_lineage`, `get_landing`, `get_ops_record`.
4. Governance tools: `validate_against_policy`, `lint_task_spec`,
   `get_active_governance_standard`.
5. Remote HTTP production hardening: source/origin checks, TLS termination,
   and deployment-level concurrency/rate controls.

The design documents under `docs/MCP_*.md` are proposals and historical
context. They reference capabilities and models that may not exist. Validate
every proposed API against the current model/service layer before coding.

## Agent and UI Lessons

- Put behavioral constraints in architecture (intent gates, narrowed tools,
  pipeline/status gates), not only in prompts.
- Keep grounding checks intent-aware: analytical/structural answers require
  evidence; general conversation should not be rejected merely because it
  contains incidental numbers.
- The Data Agent must not receive the full ontology payload. Large ontology
  prompts caused HTTP 413 failures; retrieve and send only the relevant subset.
- Do not silently fall back to technical names when LLM business naming fails;
  fail with a reviewable error and resume from checkpoints where supported.
- The ontology UI is review-first. Optimize for fast grouped decisions, keep
  exception evidence visible, and do not auto-promote uncertain objects to
  `business_object`.
- Derived fields are contract fields. When changing a read model, trace every
  consumer and test that values such as provenance, landing, primary-key
  confidence, and readiness are actually populated.

## Safe Working Procedure

1. Confirm `git branch --show-current` and `git status --short` before editing.
2. Read the relevant model, service, schema, API, and tests before adding a
   wrapper or field. Search with `rg`; do not trust a plan's names blindly.
3. Preserve unrelated user changes. Use `apply_patch` for manual edits.
4. Add focused regression tests for changed contracts, then run the narrow test
   set followed by broader tests when the change crosses module boundaries.
5. Verify imports and protocol boundaries, not only syntax. For MCP changes,
   check registration, schema, `is_error`, authorization, audit behavior, and
   real SDK handshake where possible.
6. Report unverified external facts honestly. Airflow/Flink/Doris topology and
   production deployment claims require live-instance evidence, not just local
   demo data.

## Useful Verification Entrypoints

- Backend tests: `cd backend && .venv/bin/pytest -q`
- MCP tests: `cd backend && .venv/bin/pytest -q tests/test_mcp_tools.py tests/test_mcp_auth.py tests/test_mcp_monitoring.py tests/test_mcp_http.py`
- MCP module import: `cd backend && .venv/bin/python -c "import app.mcp.server; print('IMPORT OK')"`
- Frontend checks: `cd frontend && npm run lint` and `npm run build`
- Primary cross-module principles: `docs/DEVELOPMENT_PRINCIPLES.md`
- MCP implementation status: `backend/app/mcp/STATUS.md`
