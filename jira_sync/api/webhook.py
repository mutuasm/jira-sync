"""Inbound sync: Jira -> ERPNext, driven by Jira webhooks.

Register a webhook in Jira (Settings > System > Webhooks) pointing to:

    https://<your-erpnext-site>/api/method/jira_sync.api.webhook.handle?secret=<webhook_secret>

Events to enable: issue created/updated/deleted, comment created, worklog
created/deleted, project updated.
"""

import json

import frappe

from jira_sync.api.jira_client import adf_to_text
from jira_sync.sync import utils


@frappe.whitelist(allow_guest=True, methods=["POST"])
def handle(secret=None):
    settings = utils.get_settings()
    if not settings.enabled:
        return {"status": "disabled"}

    expected = settings.get_password("webhook_secret", raise_exception=False)
    if not expected or secret != expected:
        frappe.throw("Invalid webhook secret", frappe.PermissionError)

    payload = json.loads(frappe.request.data or "{}")

    # Loop prevention: ignore events triggered by the integration account itself
    actor = (payload.get("user") or payload.get("comment", {}).get("author") or {}).get(
        "accountId"
    )
    if actor and actor == settings.jira_account_id:
        return {"status": "ignored", "reason": "self-triggered"}

    event = payload.get("webhookEvent", "")
    # Process asynchronously so Jira gets a fast 200
    frappe.enqueue(
        "jira_sync.api.webhook.process_event",
        queue="short",
        event=event,
        payload=payload,
    )
    return {"status": "queued", "event": event}


def process_event(event, payload):
    handlers = {
        "jira:issue_created": handle_issue_upsert,
        "jira:issue_updated": handle_issue_upsert,
        "jira:issue_deleted": handle_issue_deleted,
        "comment_created": handle_comment_created,
        "worklog_created": handle_worklog_created,
        "worklog_deleted": handle_worklog_deleted,
    }
    fn = handlers.get(event)
    if not fn:
        return
    with utils.inbound_sync():
        fn(payload)
    frappe.db.commit()


# ---------------------------------------------------------------------------
# Issues -> Tasks
# ---------------------------------------------------------------------------
def handle_issue_upsert(payload):
    if not utils.sync_enabled("sync_tasks"):
        return
    issue = payload.get("issue") or {}
    fields = issue.get("fields") or {}
    issue_key = issue.get("key")
    if not issue_key:
        return

    project_key = (fields.get("project") or {}).get("key")
    erp_project = utils.project_for_jira_key(project_key)
    task_name = utils.task_for_issue_key(issue_key)

    subject = fields.get("summary") or issue_key
    description = adf_to_text(fields.get("description"))
    jira_status = (fields.get("status") or {}).get("name")
    task_status = utils.task_status_for_jira_status(jira_status)

    if task_name:
        doc = frappe.get_doc("Task", task_name)
        doc.subject = subject
        if description:
            doc.description = description
        if task_status and doc.status != task_status:
            doc.status = task_status
        doc.jira_last_synced = frappe.utils.now()
        doc.flags.ignore_permissions = True
        doc.save()
    else:
        if not erp_project:
            return  # issue belongs to an unmapped Jira project
        doc = frappe.get_doc(
            {
                "doctype": "Task",
                "subject": subject,
                "description": description,
                "project": erp_project,
                "status": task_status or "Open",
                "jira_issue_key": issue_key,
                "jira_issue_id": issue.get("id"),
                "jira_last_synced": frappe.utils.now(),
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert()


def handle_issue_deleted(payload):
    if not utils.sync_enabled("sync_tasks"):
        return
    issue_key = (payload.get("issue") or {}).get("key")
    task_name = utils.task_for_issue_key(issue_key)
    if not task_name:
        return
    # Mirror the deletion softly: cancel the task rather than deleting it.
    doc = frappe.get_doc("Task", task_name)
    doc.status = "Cancelled"
    doc.flags.ignore_permissions = True
    doc.save()
    doc.add_comment("Comment", f"Linked Jira issue {issue_key} was deleted in Jira.")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
def handle_comment_created(payload):
    if not utils.sync_enabled("sync_comments"):
        return
    issue_key = (payload.get("issue") or {}).get("key")
    comment = payload.get("comment") or {}
    task_name = utils.task_for_issue_key(issue_key)
    if not task_name or not comment:
        return
    if frappe.db.exists("Comment", {"jira_comment_id": comment.get("id")}):
        return
    body = adf_to_text(comment.get("body"))
    if body.startswith("[") and "via ERPNext]" in body.split("\n", 1)[0]:
        return  # our own mirrored comment
    author = (comment.get("author") or {}).get("displayName", "Jira user")
    doc = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": "Task",
            "reference_name": task_name,
            "content": f"<b>{frappe.utils.escape_html(author)} (Jira):</b><br>"
            + frappe.utils.escape_html(body).replace("\n", "<br>"),
            "jira_comment_id": comment.get("id"),
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()


# ---------------------------------------------------------------------------
# Worklogs -> Timesheets
# ---------------------------------------------------------------------------
def handle_worklog_created(payload):
    if not utils.sync_enabled("sync_timesheets"):
        return
    worklog = payload.get("worklog") or {}
    worklog_id = worklog.get("id")
    issue_id = worklog.get("issueId")
    if not worklog_id or not issue_id:
        return
    if frappe.db.exists("Timesheet Detail", {"jira_worklog_id": worklog_id}):
        return
    task = frappe.db.get_value("Task", {"jira_issue_id": issue_id}, ["name", "project"], as_dict=True)
    if not task:
        return

    seconds = worklog.get("timeSpentSeconds") or 0
    hours = round(seconds / 3600.0, 3)
    started = frappe.utils.get_datetime(
        (worklog.get("started") or "").split("+")[0].replace("T", " ").split(".")[0]
    )
    author = (worklog.get("author") or {}).get("displayName", "Jira user")

    ts = frappe.get_doc(
        {
            "doctype": "Timesheet",
            "note": f"Jira worklog by {author}",
            "time_logs": [
                {
                    "activity_type": _default_activity_type(),
                    "from_time": started,
                    "hours": hours,
                    "task": task.name,
                    "project": task.project,
                    "description": adf_to_text(worklog.get("comment"))
                    or f"Jira worklog {worklog_id}",
                    "jira_worklog_id": worklog_id,
                }
            ],
        }
    )
    ts.flags.ignore_permissions = True
    ts.insert()  # left as draft for review; submit manually or automate here


def handle_worklog_deleted(payload):
    if not utils.sync_enabled("sync_timesheets"):
        return
    worklog_id = (payload.get("worklog") or {}).get("id")
    row = frappe.db.get_value(
        "Timesheet Detail", {"jira_worklog_id": worklog_id}, ["name", "parent"], as_dict=True
    )
    if not row:
        return
    ts = frappe.get_doc("Timesheet", row.parent)
    if ts.docstatus == 0:
        ts.time_logs = [r for r in ts.time_logs if r.name != row.name]
        if ts.time_logs:
            ts.flags.ignore_permissions = True
            ts.save()
        else:
            ts.delete(ignore_permissions=True)
    else:
        ts.add_comment(
            "Comment", f"Jira worklog {worklog_id} was deleted in Jira; review this timesheet."
        )


def _default_activity_type():
    name = frappe.db.get_value("Activity Type", {"name": "Execution"}) or frappe.db.get_value(
        "Activity Type", {}, "name"
    )
    if not name:
        doc = frappe.get_doc({"doctype": "Activity Type", "activity_type": "Execution"})
        doc.flags.ignore_permissions = True
        doc.insert()
        name = doc.name
    return name
