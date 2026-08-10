# Jira Sync for ERPNext

Two-way synchronization between ERPNext Projects and Jira Cloud.

## What syncs

| ERPNext | Jira | Direction |
|---|---|---|
| Project | Project | mapped (name updates pushed) |
| Task | Issue | two-way: subject, description, status |
| Timesheet time log | Worklog | two-way |
| Comment on Task | Issue comment | two-way |

**How it works:** ERPNext → Jira uses document event hooks with background jobs (saves never block on the Jira API). Jira → ERPNext uses webhooks for real-time updates, plus an hourly incremental pull and a daily full reconcile as safety nets. Echo loops are prevented on both sides — an inbound-sync flag suppresses outbound hooks, and webhook events triggered by the integration account itself are ignored.

**Deliberately conservative deletions:** deleting a Task in ERPNext only leaves a comment on the Jira issue (never deletes remote data); deleting an issue in Jira sets the Task to Cancelled instead of deleting it. Inbound worklogs create *draft* Timesheets for review.

## Installation

```bash
cd ~/frappe-bench
bench get-app https://github.com/mutuasm/jira-sync.git
bench --site yoursite.local install-app jira_sync
bench --site yoursite.local migrate
bench restart
```

Ensure background workers and the scheduler are running (`bench doctor` to check).

## Setup

### 1. Jira API token
Create one at https://id.atlassian.com/manage-profile/security/api-tokens using a dedicated service account (recommended, so loop-prevention and audit trails are clean).

### 2. Configure ERPNext
Open **Jira Sync Settings** (single doctype):
- Jira URL: `https://yourcompany.atlassian.net`
- Jira User Email + API Token: the service account credentials
- Webhook Secret: any long random string
- Tick **Enabled** and the sync options you want
- Add **Project Mappings**: one row per ERPNext Project ↔ Jira project key (e.g. `PROJ`)
- Add **Status Mappings** to match your Jira workflow, e.g.:
  - To Do → Open
  - In Progress → Working
  - In Review → Pending Review
  - Done → Completed

Saving validates the connection and auto-detects the integration account ID.

### 3. Register the Jira webhook
In Jira: **Settings → System → Webhooks → Create webhook**
- URL: `https://<your-erpnext-site>/api/method/jira_sync.api.webhook.handle?secret=<your-webhook-secret>`
- Events: Issue *created, updated, deleted*; Comment *created*; Worklog *created, deleted*
- Optional JQL filter: `project in (PROJ1, PROJ2)` to limit traffic

Your ERPNext site must be reachable over HTTPS from the internet for webhooks to arrive. If it isn't, the hourly reconcile job still keeps issues in sync (with up to an hour of lag), but comments and worklogs are webhook-only in this version.

### 4. Initial import
Trigger a full pull from the bench console:

```bash
bench --site yoursite.local execute jira_sync.sync.reconcile.full_reconcile
```

## Notes & extension points

- **Jira Server/Data Center:** this targets Jira Cloud REST v3 (basic auth + ADF descriptions). For Server/DC, switch endpoints to `/rest/api/2/` and plain-text bodies in `api/jira_client.py`.
- **Assignee sync** isn't included (needs a User ↔ Jira account mapping table); the structure in `sync/utils.py` makes it easy to add.
- **Auto-submit inbound timesheets:** change `ts.insert()` to also `ts.submit()` in `api/webhook.py` if you don't want a review step.
- Errors are logged to **Error Log** in ERPNext with the failing entity in the title.
