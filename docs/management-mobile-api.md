# Management Mobile API Contract

Date: 2026-07-08

Status: v1 server contract implemented for APP management-mode integration.

This document is for the APP thread. It describes the first service-side contract for management-mode screens in the existing APP.

Next workflow design:

- `docs/management-workflow-design.md`

Do not implement retake, clarification, correction follow-up, or daily closeout as APP-local state. Those need the server task/session model described in the workflow design.

## Scope

This is not a new employee upload API. Employee upload remains on the legacy mobile API with `X-API-Key`.

Management-mode APIs use `/api/v2/management/...` and require a logged-in portal/API v2 user session. The first APP version can use `/api/v2/auth/login` and keep the session cookie. A mobile token/device-session layer can be added later without changing the management response shapes.

Allowed roles:

- `platform_super_admin`
- `super_admin`
- `owner`
- `admin`
- `manager`
- `project_manager`

Denied roles:

- `client`
- `client_viewer`
- `worker`
- `employee`
- anonymous requests
- employee `X-API-Key` without a user session

## Guardrails

The management API:

- returns database-derived facts, not raw AI output.
- does not expose `file_path`, `storage_path`, `raw_model_output`, or prompt text.
- separates upload time from capture time.
- separates upload success from AI/queue processing success.
- respects company and project membership scoping.
- writes management review actions to audit logs.

## Authentication

Login:

```http
POST /api/v2/auth/login
Content-Type: application/json

{
  "email": "manager@example.com",
  "password": "..."
}
```

The current response is:

```json
{
  "message": "ok",
  "tenant_slug": "default",
  "future_token_mode": "not_enabled"
}
```

APP should retain the returned session cookie for management calls.

## 1. Project Overview

```http
GET /api/v2/management/projects/overview?window_days=1
GET /api/v2/management/projects/overview?start_date=2026-07-08&end_date=2026-07-08
```

Purpose: management home screen project list.

Important fields:

- `totals.uploaded_count`: photos uploaded in the selected window, based on `photos.created_at`.
- `totals.captured_count`: photos captured in the selected window, based on `photos.captured_at_utc`.
- `totals.pending_internal_count`: all current project photos still `pending + internal`.
- `totals.pending_internal_window_count`: photos uploaded in the selected window and still `pending + internal`.
- `totals.client_visible_count`: all current `approved + client_visible` project photos.
- `totals.late_upload_count`: photos uploaded more than 12 hours after capture.
- `projects[].needs_review`: true when the project has pending internal photos.
- `projects[].has_recent_uploads`: true when selected window has uploads.

APP should display both upload and capture counts. Do not label captured count as uploaded count.

## 2. Project Review Summary

```http
GET /api/v2/management/projects/{project_id}/review-summary?window_days=1
```

Purpose: project status card for the manager.

Includes:

- `project`
- `window`
- `counts`
- `status_flags`
- `processing.photo_ai_queue`
- `processing.progress_report_status_counts`
- `recent_photos`

`recent_photos[]` is safe for APP display and intentionally excludes internal paths.

Photo fields include:

- `id`
- `employee_id`
- `project_id`
- `image_url`
- `thumb_url`
- `media_url`
- `media_kind`
- `captured_at_utc`
- `created_at`
- `visibility`
- `approval_status`
- `client_visible_ready`
- `labeling_status`
- `note`
- `location`
- `gps_lat`
- `gps_lon`
- `device_model`
- `app_version`

## 3. Review Inbox

```http
GET /api/v2/management/projects/{project_id}/review-inbox?status_filter=pending&limit=50
```

`status_filter` values:

- `pending`: `approval_status=pending` and `visibility=internal`.
- `failed_processing`: photos whose AI labeling status is `failed`.
- `client_visible`: `approved + client_visible`.
- `all`: all accessible non-deleted project photos.

Purpose: photo review queue.

APP should use this screen for the manager's daily review flow.

## 4. Upload Health

```http
GET /api/v2/management/projects/{project_id}/upload-health?window_days=1
```

Purpose: distinguish upload health from AI/queue health.

Includes:

- `upload_counts`: same count structure as overview.
- `photo_ai_queue_company_snapshot`: company-level AI queue snapshot.
- `project_photo_task_status_counts`: task status counts for project photos uploaded in the selected window.
- `diagnostics.company_mobile_diagnostic_logs_in_window`: company diagnostic log count.
- `diagnostics.project_attribution`: currently `not_available_in_current_diagnostic_log_schema`.
- `health_flags.has_recent_uploads`
- `health_flags.has_late_uploads`
- `health_flags.has_processing_dead_letters`

Important: dead-letter AI tasks do not mean photos failed to upload.

## 5. Review Actions

```http
POST /api/v2/management/review-actions
Content-Type: application/json

{
  "photo_ids": [1320],
  "action": "approve_client_visible",
  "comment": "Ready for client review."
}
```

Supported `action` values:

- `approve_client_visible`: sets `approval_status=approved` and `visibility=client_visible`.
- `approve_internal`: sets `approval_status=approved` and `visibility=internal`.
- `keep_internal`: keeps/sets `visibility=internal`.
- `reject`: sets `approval_status=rejected` and `visibility=internal`.
- `comment`: writes a photo comment; `comment` is required.

Any action may include a `comment`; the server writes the comment to `photo_comments`.

Response:

```json
{
  "message": "ok",
  "action": "approve_client_visible",
  "updated_photo_ids": [1320],
  "comment_ids": [1],
  "processed_count": 1
}
```

## Not In v1

The following are intentionally not implemented in v1:

- retake request task status
- clarification task status
- daily closeout session
- mobile device token/session table
- push notification routing

Those need a real task model such as `review_tasks`, `review_sessions`, and `daily_project_closeouts`. Do not fake them in the APP by storing local-only state.

## APP Implementation Notes

Recommended first screens:

1. Management home: call project overview.
2. Project card: call review summary.
3. Pending review: call review inbox with `status_filter=pending`.
4. Upload health: call upload health.
5. Review action buttons:
   - approve for client
   - approve internal
   - keep internal
   - reject
   - comment

APP should not compute project totals from raw photo lists. Use the server counts.
