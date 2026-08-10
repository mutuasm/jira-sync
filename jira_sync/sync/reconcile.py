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
    pull_projects()
    if not utils.sync_enabled("sync_tasks"):
        return
    keys = _mapped_project_keys()
    if not keys:
        return
    jql = f"project in ({', '.join(keys)}) ORDER BY updated ASC"
    _pull(jql)


def pull_projects():
    """Create ERPNext Projects for Jira projects that aren't mapped yet."""
    if not utils.sync_enabled("sync_projects"):
        return
    client = JiraClient()
    for proj in client.get_projects():
        key = proj.get("key")
        if not key or utils.project_for_jira_key(key):
            continue
        try:
            with utils.inbound_sync():
                _adopt_or_create_project(key, proj.get("name") or key)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title=f"Jira project pull failed: {key}")


def _adopt_or_create_project(key, name):
    """Link an existing same-named unmapped Project, or create a new one."""
    existing = frappe.db.get_value(
        "Project", {"project_name": name}, ["name", "jira_project_key"], as_dict=True
    )
    if existing and not existing.jira_project_key:
        frappe.db.set_value(
            "Project", existing.name, "jira_project_key", key, update_modified=False
        )
        return existing.name
    doc = frappe.get_doc(
        {
            "doctype": "Project",
            # a same-named project already mapped to another Jira key needs a distinct name
            "project_name": name if not existing else f"{name} ({key})",
            "jira_project_key": key,
            "status": "Open",
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


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
