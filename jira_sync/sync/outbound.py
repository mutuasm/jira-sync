"""Outbound sync: ERPNext -> Jira.

Doc-event handlers enqueue background jobs so API latency never blocks saves.
Every handler is a no-op while an inbound (Jira -> ERPNext) change is being
applied, which prevents infinite echo loops.
"""

import frappe
from frappe.utils import get_datetime

from jira_sync.api.jira_client import JiraClient
from jira_sync.sync import utils


def _enqueue(method, **kwargs):
    frappe.enqueue(
        method, queue="short", enqueue_after_commit=True, **kwargs
    )


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
def project_after_insert(doc, method=None):
    # Projects are usually mapped, not created, since Jira project creation
    # needs admin scopes and key allocation. Creation is opt-in via jira_project_key.
    pass


def project_on_update(doc, method=None):
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_projects"):
        return
    if not doc.jira_project_key:
        return
    _enqueue(
        "jira_sync.sync.outbound.push_project_update",
        project=doc.name,
    )


def push_project_update(project):
    doc = frappe.get_doc("Project", project)
    if not doc.jira_project_key:
        return
    client = JiraClient()
    try:
        client.put(
            f"/rest/api/3/project/{doc.jira_project_key}",
            {"name": doc.project_name},
        )
    except Exception:
        frappe.log_error(title=f"Jira project push failed: {project}")


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------
def task_after_insert(doc, method=None):
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_tasks"):
        return
    if doc.jira_issue_key:  # already linked (e.g. created by inbound sync)
        return
    if not doc.project:
        return
    if not utils.jira_key_for_project(doc.project):
        return  # project not mapped to Jira -> stay local
    _enqueue("jira_sync.sync.outbound.push_new_task", task=doc.name)


def push_new_task(task):
    doc = frappe.get_doc("Task", task)
    if doc.jira_issue_key:
        return
    project_key = utils.jira_key_for_project(doc.project)
    if not project_key:
        return
    client = JiraClient()
    issue = client.create_issue(
        project_key=project_key,
        summary=doc.subject,
        description=frappe.utils.strip_html(doc.description or ""),
    )
    frappe.db.set_value(
        "Task",
        doc.name,
        {
            "jira_issue_key": issue.get("key"),
            "jira_issue_id": issue.get("id"),
            "jira_last_synced": frappe.utils.now(),
        },
        update_modified=False,
    )
    # push status too if the task didn't start as Open
    if doc.status and doc.status != "Open":
        target = utils.jira_status_for_task_status(doc.status)
        if target:
            client.transition_issue(issue.get("key"), target)
    # push anyone already assigned (their ToDo events predate the Jira link)
    if utils.task_assignees(doc.name):
        push_task_assignee(doc.name)


def task_on_update(doc, method=None):
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_tasks"):
        return
    if not doc.jira_issue_key:
        return
    old = doc.get_doc_before_save()
    changed = {}
    if not old or old.subject != doc.subject:
        changed["summary"] = doc.subject
    if not old or (old.description or "") != (doc.description or ""):
        changed["description"] = frappe.utils.strip_html(doc.description or "")
    status_changed = not old or old.status != doc.status
    if not changed and not status_changed:
        return
    _enqueue(
        "jira_sync.sync.outbound.push_task_update",
        task=doc.name,
        changed=changed,
        status=doc.status if status_changed else None,
    )


def push_task_update(task, changed=None, status=None):
    doc = frappe.get_doc("Task", task)
    if not doc.jira_issue_key:
        return
    client = JiraClient()
    fields = {}
    if changed:
        if "summary" in changed:
            fields["summary"] = changed["summary"]
        if "description" in changed:
            from jira_sync.api.jira_client import text_to_adf

            fields["description"] = text_to_adf(changed["description"])
    if fields:
        client.update_issue(doc.jira_issue_key, fields)
    if status:
        target = utils.jira_status_for_task_status(status)
        if target:
            client.transition_issue(doc.jira_issue_key, target)
    frappe.db.set_value(
        "Task", doc.name, "jira_last_synced", frappe.utils.now(), update_modified=False
    )


# ---------------------------------------------------------------------------
# Assignments (ToDo) -> Jira assignee
# ---------------------------------------------------------------------------
def todo_after_insert(doc, method=None):
    _todo_changed(doc)


def todo_on_update(doc, method=None):
    _todo_changed(doc)


def todo_on_trash(doc, method=None):
    _todo_changed(doc)


def _todo_changed(doc):
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_tasks"):
        return
    if doc.reference_type != "Task" or not doc.reference_name:
        return
    if not frappe.db.get_value("Task", doc.reference_name, "jira_issue_key"):
        return
    _enqueue("jira_sync.sync.outbound.push_task_assignee", task=doc.reference_name)


def push_task_assignee(task):
    row = frappe.db.get_value(
        "Task", task, ["jira_issue_key", "_assign"], as_dict=True
    )
    if not row or not row.jira_issue_key:
        return
    assigned = frappe.parse_json(row._assign or "[]")
    client = JiraClient()
    account_id = None
    if assigned:
        # Jira holds a single assignee; mirror the most recent assignment
        for user in reversed(assigned):
            email = frappe.db.get_value("User", user, "email") or user
            account_id = client.get_account_id_for_email(email)
            if account_id:
                break
        if not account_id:
            return  # nobody assigned locally has a Jira account; don't unassign
    try:
        client.assign_issue(row.jira_issue_key, account_id)
        frappe.db.set_value(
            "Task", task, "jira_last_synced", frappe.utils.now(), update_modified=False
        )
    except Exception:
        frappe.log_error(title=f"Jira assignee push failed: {task}")


def task_on_trash(doc, method=None):
    # Deliberately do NOT delete the Jira issue — deleting remote data on a
    # local trash is dangerous. Instead leave a comment on the issue.
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_tasks"):
        return
    if not doc.jira_issue_key:
        return
    _enqueue(
        "jira_sync.sync.outbound.note_task_deleted",
        issue_key=doc.jira_issue_key,
        task=doc.name,
    )


def note_task_deleted(issue_key, task):
    try:
        JiraClient().add_comment(
            issue_key, f"Linked ERPNext task {task} was deleted in ERPNext."
        )
    except Exception:
        frappe.log_error(title=f"Jira comment failed for deleted task {task}")


# ---------------------------------------------------------------------------
# Timesheet -> Worklogs
# ---------------------------------------------------------------------------
def timesheet_on_submit(doc, method=None):
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_timesheets"):
        return
    _enqueue("jira_sync.sync.outbound.push_worklogs", timesheet=doc.name)


def push_worklogs(timesheet):
    doc = frappe.get_doc("Timesheet", timesheet)
    client = JiraClient()
    for row in doc.time_logs:
        if row.jira_worklog_id or not row.task:
            continue
        issue_key = frappe.db.get_value("Task", row.task, "jira_issue_key")
        if not issue_key:
            continue
        seconds = int((row.hours or 0) * 3600)
        if seconds < 60:
            continue
        started = get_datetime(row.from_time).strftime("%Y-%m-%dT%H:%M:%S.000%z")
        if not started.endswith(("+0000", "-0000")) and "+" not in started and started.count("-") <= 2:
            started += "+0000"
        try:
            wl = client.add_worklog(
                issue_key, seconds, started, comment=row.description or doc.name
            )
            frappe.db.set_value(
                "Timesheet Detail",
                row.name,
                "jira_worklog_id",
                wl.get("id"),
                update_modified=False,
            )
        except Exception:
            frappe.log_error(title=f"Jira worklog push failed: {timesheet}/{row.name}")


def timesheet_on_cancel(doc, method=None):
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_timesheets"):
        return
    _enqueue("jira_sync.sync.outbound.remove_worklogs", timesheet=doc.name)


def remove_worklogs(timesheet):
    doc = frappe.get_doc("Timesheet", timesheet)
    client = JiraClient()
    for row in doc.time_logs:
        if not row.jira_worklog_id or not row.task:
            continue
        issue_key = frappe.db.get_value("Task", row.task, "jira_issue_key")
        if not issue_key:
            continue
        try:
            client.delete_worklog(issue_key, row.jira_worklog_id)
            frappe.db.set_value(
                "Timesheet Detail", row.name, "jira_worklog_id", None, update_modified=False
            )
        except Exception:
            frappe.log_error(title=f"Jira worklog delete failed: {timesheet}/{row.name}")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
def comment_after_insert(doc, method=None):
    if utils.in_inbound_sync() or not utils.sync_enabled("sync_comments"):
        return
    if doc.comment_type != "Comment" or doc.reference_doctype != "Task":
        return
    if doc.get("jira_comment_id"):  # came from Jira
        return
    issue_key = frappe.db.get_value("Task", doc.reference_name, "jira_issue_key")
    if not issue_key:
        return
    _enqueue(
        "jira_sync.sync.outbound.push_comment",
        comment=doc.name,
        issue_key=issue_key,
    )


def push_comment(comment, issue_key):
    doc = frappe.get_doc("Comment", comment)
    author = doc.comment_email or doc.owner or "ERPNext"
    text = f"[{author} via ERPNext]\n{frappe.utils.strip_html(doc.content or '')}"
    try:
        result = JiraClient().add_comment(issue_key, text)
        frappe.db.set_value(
            "Comment", doc.name, "jira_comment_id", result.get("id"), update_modified=False
        )
    except Exception:
        frappe.log_error(title=f"Jira comment push failed: {comment}")
