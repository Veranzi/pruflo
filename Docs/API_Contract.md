# PruFlo API contract (endpoints 1–33)

The request and response shapes for the core API. Endpoints 34–50 (API keys, webhooks, document sources, rules, providers and the findings export) are in [`Integrations_API_Contract.md`](Integrations_API_Contract.md). Endpoint numbers match `frontend/lib/core/api_endpoints.dart`.

If you change a shape, update this file in the same pull request as the code.

## Conventions

- Base URL: the Cloud Run API, e.g. `https://pruflo-api-xxxx.a.run.app`. All routes are under `/v1`.
- **Auth:** every call sends `Authorization: Bearer <Firebase ID token>` and `X-Firebase-AppCheck: <token>`, or `Authorization: Bearer pf_live_...` for API keys. Local development also accepts `dev:<uid>:<email>` tokens when the server runs with `PRUFLO_ENV=local`.
- **IDs** are opaque strings with a type prefix (`case_`, `evd_`, `clm_`, `fnd_`, `rpt_`, `u_`, `cus_`). Cases also have a `reference` people see, such as `PL-001`.
- **Timestamps** are ISO 8601 UTC. **Decimals** (quantities, money) are strings.
- **Lists** return `{"items": [...], "next_cursor": "..." | null}`. Pass `?cursor=` for the next page and `?limit=` (1–200, default 50).
- **Errors** return `{"error": {"code", "message", "field", "request_id"}}`. Show `message` to people.
- **Enums:** case `status` is `awaiting_evidence`, `in_review` or `completed`. Review `decision` is `approve`, `reject` or `pending`. `role` is `investigator`, `reviewer`, `admin` or `viewer`.

Who may call what is in [`backend_plan.md`](backend_plan.md#roles-and-permissions). A case someone can't see returns `404`, not `403`.

## Profile and session

| # | Method and path | Body | Response |
| --- | --- | --- | --- |
| 1 | `GET /v1/me` | | `Me` |
| 21 | `PATCH /v1/me` | any of `name`, `phone`, `job_title` | `Me` |
| 2 | `POST /v1/devices` | `{"token", "platform": "web"\|"android"\|"ios"}` | `201 {"id", "platform"}` |
| 22 | `DELETE /v1/devices/{id}` | | `204` |
| 23 | `GET /v1/me/activity` | | list of `{"title", "case_id", "case_reference", "at", "kind"}`; `kind` is `evidence_uploaded`, `review_completed` or `report_generated` |

```json
Me = { "id": "u_...", "name": "Sarah Johnson", "email": "sarah@acme.com", "role": "reviewer",
       "tenant_id": "ten_...", "phone": "", "job_title": "", "active": true }
```

A signed-in Firebase account that hasn't been invited gets `403` with code `not_invited`.

## Cases

| # | Method and path | Body / query | Response |
| --- | --- | --- | --- |
| 3 | `GET /v1/cases` | `?status=`, `?priority=`, `?q=` (title or reference) | list of `Case`, newest first |
| 4 | `POST /v1/cases` | `{"title", "category"?, "description"?, "priority"?, "assigned_to"?, "customer_id"?}` | `201 Case` |
| 5 | `GET /v1/cases/{caseId}` | | `Case` |
| 6 | `PATCH /v1/cases/{caseId}` | any of the create fields, plus optional `version` (`409` if it changed) | `Case` |
| 16 | `GET /v1/cases/{caseId}/timeline` | | list of `{"id", "action", "at", "actor_type", "actor", "details"}` |

```json
Case = { "id": "case_...", "reference": "PL-001", "title": "Fraud Investigation", "category": "",
         "description": "", "status": "in_review", "priority": "high", "customer_id": null,
         "assigned_to": { "uid": "u_...", "name": "Sarah Johnson" } | null,
         "created_at": "...", "updated_at": "...", "version": 3 }
```

`status` can't be set by hand. It follows the evidence and the review: new evidence → `awaiting_evidence`; reconciliation finds something to review → `in_review`; no finding left `pending` → `completed`.

## Evidence

| # | Method and path | Body / query | Response |
| --- | --- | --- | --- |
| 7 | `POST /v1/cases/{caseId}/evidence/upload-url` | `{"file_name", "size_bytes", "content_type", "evidence_types": ["documents"\|"images"\|"other"]}` | `201 {"evidence_id", "upload_url", "method", "headers", "expires_at"}` |
| 8 | `POST /v1/cases/{caseId}/evidence/{evidenceId}/complete` | | `Evidence` |
| 9 | `GET /v1/cases/{caseId}/evidence` | | list of `Evidence` |
| 10 | `POST /v1/cases/{caseId}/evidence/{evidenceId}/retry` | | `Evidence` |
| 11 | `GET /v1/cases/{caseId}/evidence/{evidenceId}/view-url` | | `{"url", "expires_at"}` |
| 12 | `GET /v1/cases/{caseId}/evidence/{evidenceId}/anchors` | `?page=` | `{"evidence_id", "status", "pages": [PageSummary], "page": PageDetail \| null}` |

Upload in three steps: ask for a URL (7), `PUT` the bytes to `upload_url` sending exactly `headers`, then call `complete` (8). Accepted: PDF, DOCX, XLSX, CSV, TXT, JPG, PNG, TIFF; at most 25 MB and 200 PDF pages.

```json
Evidence = { "id": "evd_...", "case_id": "case_...", "file_name": "po.pdf", "content_type": "application/pdf",
             "size_bytes": 52144, "page_count": 2, "evidence_types": ["documents"],
             "status": "processed", "error_class": null, "sha256": "...", "uploaded_at": "...", "created_at": "..." }
```

Evidence `status`: `uploading`, `queued`, `extracting`, `grounding`, `reconciling`, `processed`, `needs_review`, `failed_retryable` (being retried) or `failed_terminal`. `error_class` says why, e.g. `unsupported_file`, `ocr_not_configured`.

`PageDetail` adds `text` and `blocks: [{"id", "index", "text", "char_start", "char_end", "bbox": {"x0","y0","x1","y1"} | null, "cell_ref", "confidence"}]`. Boxes are fractions of the page, top-left origin.

## Claims, findings and review

| # | Method and path | Body / query | Response |
| --- | --- | --- | --- |
| 13 | `GET /v1/cases/{caseId}/claims` | `?evidence_id=` | list of `Claim` |
| 14 | `GET /v1/cases/{caseId}/findings` | `?review_status=` | list of `Finding` |
| 15 | `POST /v1/cases/{caseId}/findings/{findingId}/decision` | `{"decision", "note", "correction"?: {"field", "previous", "new"}}` | `Finding` |

```json
Claim = { "id": "clm_...", "evidence_id": "evd_...", "file_name": "po.pdf", "field": "ordered_quantity",
          "value": "100", "unit": "units", "page": 1, "quote": "Qty ordered: 100",
          "grounding": "grounded", "bbox": {...} | null, "model": "gemini-2.5-flash", "created_at": "..." }

Finding = { "id": "fnd_...", "rule_id": "rule_...", "rule_version": 3, "type": "quantity_shortfall",
            "severity": "high", "expected": {"value": "100", "unit": "units"},
            "observed": {"value": "80", "unit": "units"}, "difference": {"value": "20", "unit": "units"},
            "explanation": "...", "explanation_source": "template" | "machine_generated",
            "review": { "status": "pending", "by": {"uid", "name"} | null, "at": null, "note": "",
                        "correction": null },
            "evidence": [Claim], "created_at": "..." }
```

Decisions (15) are for signed-in reviewers and admins only, never API keys. `reject` and `pending` need a `note`. Every decision is kept in the audit trail. When no finding in the case is `pending`, the case becomes `completed`; setting one back to `pending` returns the case to `in_review`.

Findings come from the rule engine (plan Step 11). Until it runs, cases have no findings and these lists are empty.

## Reports

| # | Method and path | Body | Response |
| --- | --- | --- | --- |
| 17 | `POST /v1/cases/{caseId}/reports` | `{"type": "summary"\|"detailed"\|"executive", "sections": ["evidence", "timeline", "findings", "recommendations"], "format": "pdf"\|"docx"}` (format defaults to `pdf`) | `202 Report` with `status: "queued"` |
| 18 | `GET /v1/cases/{caseId}/reports` | | list of `Report` |
| 19 | `GET /v1/cases/{caseId}/reports/{reportId}/download-url?disposition=attachment\|inline` | | `{"url", "expires_at"}`; `409` until `ready`. `attachment` (default) downloads the file; `inline` opens it in the browser, a preview for PDFs |

```json
Report = { "id": "rpt_...", "case_id": "case_...", "title": "PL-001 Detailed report", "type": "detailed",
           "format": "pdf" | "docx", "status": "queued" | "ready" | "failed", "requested_by": {"uid", "name"} | null,
           "created_at": "...", "completed_at": null }
```

Reports are generated by the worker from stored data only and fire the `report.ready` webhook. Exporting needs the `investigator`, `reviewer` or `admin` role; viewers get `403`.

## Admin

All under `/v1/admin`, admin role only, never API keys. Everything is scoped to the admin's organisation and audited.

| # | Method and path | Body / query | Response |
| --- | --- | --- | --- |
| 24 | `GET /v1/admin/users` | `?q=` | list of `User` |
| 25 | `POST /v1/admin/users` | `{"email", "name"?, "role"}` | `201 User` |
| 26 | `PATCH /v1/admin/users/{userId}` | any of `role`, `active`, `name` | `User` |
| 27 | `DELETE /v1/admin/users/{userId}` | | `204`: removes them from this organisation |
| 28 | `GET /v1/admin/customers` | | list of `Customer` |
| 29 | `POST /v1/admin/customers` | `{"name", "contact_email"?}` | `201 Customer` |
| 30 | `PATCH /v1/admin/customers/{customerId}` | any of `name`, `contact_email` | `Customer` |
| 31 | `GET /v1/admin/cases/{caseId}/members` | | `{"items": [{"uid", "name", "email", "role"}]}` |
| 32 | `PUT /v1/admin/cases/{caseId}/members` | `{"user_ids": [...]}` | same as 31 |
| 33 | `GET /v1/admin/audit` | `?action=`, `?case_id=` | list of `{"id", "action", "at", "actor_type", "actor", "target_type", "target_id", "case_id", "details"}` |

```json
User = { "id": "u_...", "name": "Sarah Johnson", "email": "sarah@acme.com", "role": "reviewer",
         "active": true, "signed_in": true, "created_at": "..." }
Customer = { "id": "cus_...", "name": "Acme Supplies", "contact_email": "", "created_at": "..." }
```

- Inviting (25) doesn't send an email. The person signs in with that email address, and their Firebase account is linked on first sign-in. `signed_in` is `false` until then.
- There must always be at least one active admin: demoting, disabling or removing the last one returns `409`.

## Notifications, profile photo and customer picker (51–57)

| # | Method and path | Body / query | Response |
| --- | --- | --- | --- |
| 51 | `GET /v1/me/notifications` | `?unread_only=true`, `?cursor=`, `?limit=` | `{"items": [Notification], "next_cursor", "unread_count"}`, newest first |
| 52 | `POST /v1/me/notifications/{notificationId}/read` | | `Notification` |
| 53 | `POST /v1/me/notifications/read-all` | | `{"updated": 3}` |
| 54 | `POST /v1/me/avatar/upload-url` | `{"content_type": "image/png"\|"image/jpeg", "size_bytes"}` (at most 5 MB) | `201 {"upload_url", "method", "headers", "expires_at"}` |
| 55 | `POST /v1/me/avatar/complete` | | `Me` with the new `avatar_url` |
| 56 | `DELETE /v1/me/avatar` | | `204` |
| 57 | `GET /v1/customers` | | list of `{"id", "name"}`; any signed-in member, or an API key with `cases:read`, for pickers |

```json
Notification = { "id": "ntf_...", "kind": "case_assigned", "title": "PL-004 was assigned to you",
                 "body": "Supplier shortfall", "case_id": "case_..." | null, "case_reference": "PL-004" | null,
                 "link": "/cases/case_...", "read": false, "created_at": "..." }
```

- `kind` is one of:
  - `evidence_processed`: your upload finished processing, failed, or needs review.
  - `case_assigned`: a case was assigned to you.
  - `case_in_review`: a case you can review has findings waiting.
  - `report_ready`: a report you asked for is ready.
  - `weekly_summary`: Monday summary of your open cases.
- `link` is an app route. Open it when the notification is tapped, then mark it read (52).
- Poll 51 with `?limit=1` (about once a minute) to update the bell's `unread_count`.
- Notifications are kept for 90 days.

**Profile photo.** Upload exactly like evidence: ask for a URL (54), `PUT` the bytes with `headers`, then `complete` (55). The server checks the file really is a PNG or JPEG, then re-encodes it as a JPEG of at most 512 × 512 px. Re-encoding strips metadata such as the GPS location in phone photos.

**Changes to `Me` (1, 21):**

- `avatar_url`: a short-lived link to the photo, or `null`. It expires after about 10 minutes, so fetch `/v1/me` again to get a fresh one.
- `notification_preferences`: `{"evidence_processed": true, "case_assigned": true, "case_in_review": true, "report_ready": true, "weekly_summary": false, "push": true}`. Each kind switches that notification on or off. `push` controls whether it is also sent to registered devices (2). Update any subset with `PATCH /v1/me {"notification_preferences": {...}}`.

**Change to `Case` (3–6):** a new `customer_name` field (string or `null`) sits next to `customer_id`.
