"""Scheduled reconciliation: catches anything webhooks missed.

- hourly: pull Jira issues updated in the last 2 hours for all mapped projects
- daily: full sweep of mapped projects (paginated)
"""

import frappe

from jira_sync.api.jira_client import JiraClient
from jira_sync.api.webhook import handle_issue_upsert
from jira_sync.sync import utils


def _mapped_project_keys():
    s = utils.get_settings()
    keys = {row.jira_project_key for row in (s.project_mappings or []) if row.jira_project_key}
    keys.update(
        frappe.get_all(
            "Project",
            filters={"jira_project_key": ("is", "set")},
            pluck="jira_project_key",
        )
    )
    return sorted(k for k in keys if k)


def pull_recent_updates():
    if not utils.sync_enabled("sync_tasks"):
        return
    keys = _mapped_project_keys()
    if not keys:
        return
    jql = f"project in ({', '.join(keys)}) AND updated >= -2h ORDER BY updated ASC"
    _pull(jql)


def full_reconcile():
    if not utils.sync_enabled("sync_tasks"):
        return
    keys = _mapped_project_keys()
    if not keys:
        return
    jql = f"project in ({', '.join(keys)}) ORDER BY updated ASC"
    _pull(jql)


def _pull(jql):
    client = JiraClient()
    next_page_token = None
    while True:
        result = client.search_issues(jql, next_page_token=next_page_token)
        issues = result.get("issues", [])
        if issues:
            with utils.inbound_sync():
                for issue in issues:
                    try:
                        handle_issue_upsert({"issue": issue})
                    except Exception:
                        frappe.log_error(
                            title=f"Reconcile failed for {issue.get('key')}"
                        )
            frappe.db.commit()
        next_page_token = result.get("nextPageToken")
        if not next_page_token:
            break
