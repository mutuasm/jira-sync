import frappe

IN_JIRA_SYNC_FLAG = "jira_sync_inbound"


def get_settings():
    return frappe.get_cached_doc("Jira Sync Settings")


def sync_enabled(option=None):
    """True if the integration is on (and, optionally, a specific option too)."""
    try:
        s = get_settings()
    except Exception:
        return False
    if not s.enabled:
        return False
    if option and not s.get(option):
        return False
    return True


def in_inbound_sync():
    """True while an inbound (Jira -> ERPNext) change is being applied.

    Outbound hooks check this to avoid echoing the change back to Jira.
    """
    return bool(frappe.flags.get(IN_JIRA_SYNC_FLAG))


class inbound_sync:
    """Context manager marking a block as inbound-sync so outbound hooks skip it."""

    def __enter__(self):
        frappe.flags[IN_JIRA_SYNC_FLAG] = True
        return self

    def __exit__(self, *exc):
        frappe.flags[IN_JIRA_SYNC_FLAG] = False
        return False


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------
def jira_key_for_project(project_name):
    """Jira project key for an ERPNext Project (mapping table first, then field)."""
    s = get_settings()
    for row in s.project_mappings or []:
        if row.erpnext_project == project_name:
            return row.jira_project_key
    return frappe.db.get_value("Project", project_name, "jira_project_key")


def project_for_jira_key(jira_project_key):
    s = get_settings()
    for row in s.project_mappings or []:
        if (row.jira_project_key or "").upper() == (jira_project_key or "").upper():
            return row.erpnext_project
    return frappe.db.get_value("Project", {"jira_project_key": jira_project_key}, "name")


def task_for_issue_key(issue_key):
    return frappe.db.get_value("Task", {"jira_issue_key": issue_key}, "name")


def user_for_jira_assignee(assignee):
    """ERPNext User matching a Jira assignee dict, by email address.

    Returns None when the assignee has no visible email (Jira privacy
    settings) or no enabled ERPNext user shares that email.
    """
    email = ((assignee or {}).get("emailAddress") or "").strip().lower()
    if not email:
        return None
    return frappe.db.get_value("User", {"email": email, "enabled": 1}, "name")


def task_assignees(task_name):
    """Current assignment list (_assign) of a Task, as a list of user names."""
    return frappe.parse_json(frappe.db.get_value("Task", task_name, "_assign") or "[]")


def task_status_for_jira_status(jira_status):
    s = get_settings()
    for row in s.status_mappings or []:
        if (row.jira_status or "").lower() == (jira_status or "").lower():
            return row.task_status
    # sensible defaults if unmapped
    defaults = {"to do": "Open", "in progress": "Working", "done": "Completed"}
    return defaults.get((jira_status or "").lower())


def jira_status_for_task_status(task_status):
    s = get_settings()
    for row in s.status_mappings or []:
        if row.task_status == task_status:
            return row.jira_status
    defaults = {"Open": "To Do", "Working": "In Progress", "Completed": "Done"}
    return defaults.get(task_status)
