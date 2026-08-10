app_name = "jira_sync"
app_title = "Jira Sync"
app_publisher = "Your Company"
app_description = "Two-way sync between ERPNext Projects and Jira"
app_email = "dev@example.com"
app_license = "MIT"

# Create custom fields (jira keys/ids on Project, Task, Timesheet Detail, Comment)
after_install = "jira_sync.install.after_install"
after_migrate = "jira_sync.install.after_install"

# ---------------------------------------------------------------------------
# Outbound: ERPNext -> Jira
# ---------------------------------------------------------------------------
doc_events = {
    "Project": {
        "after_insert": "jira_sync.sync.outbound.project_after_insert",
        "on_update": "jira_sync.sync.outbound.project_on_update",
    },
    "Task": {
        "after_insert": "jira_sync.sync.outbound.task_after_insert",
        "on_update": "jira_sync.sync.outbound.task_on_update",
        "on_trash": "jira_sync.sync.outbound.task_on_trash",
    },
    "Timesheet": {
        "on_submit": "jira_sync.sync.outbound.timesheet_on_submit",
        "on_cancel": "jira_sync.sync.outbound.timesheet_on_cancel",
    },
    "Comment": {
        "after_insert": "jira_sync.sync.outbound.comment_after_insert",
    },
}

# ---------------------------------------------------------------------------
# Inbound safety net: hourly reconciliation (webhooks are the primary channel)
# ---------------------------------------------------------------------------
scheduler_events = {
    "hourly": [
        "jira_sync.sync.reconcile.pull_recent_updates",
    ],
    "daily": [
        "jira_sync.sync.reconcile.full_reconcile",
    ],
}
