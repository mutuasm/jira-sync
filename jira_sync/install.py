import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def after_install():
    """Create the custom fields that anchor ERPNext docs to Jira entities."""
    create_custom_fields(
        {
            "Project": [
                dict(
                    fieldname="jira_project_key",
                    label="Jira Project Key",
                    fieldtype="Data",
                    insert_after="project_name",
                    unique=1,
                    read_only=0,
                    no_copy=1,
                ),
            ],
            "Task": [
                dict(
                    fieldname="jira_issue_key",
                    label="Jira Issue Key",
                    fieldtype="Data",
                    insert_after="subject",
                    unique=1,
                    no_copy=1,
                    in_standard_filter=1,
                ),
                dict(
                    fieldname="jira_issue_id",
                    label="Jira Issue ID",
                    fieldtype="Data",
                    insert_after="jira_issue_key",
                    hidden=1,
                    no_copy=1,
                ),
                dict(
                    fieldname="jira_last_synced",
                    label="Jira Last Synced",
                    fieldtype="Datetime",
                    insert_after="jira_issue_id",
                    read_only=1,
                    hidden=1,
                    no_copy=1,
                ),
            ],
            "Timesheet Detail": [
                dict(
                    fieldname="jira_worklog_id",
                    label="Jira Worklog ID",
                    fieldtype="Data",
                    insert_after="description",
                    read_only=1,
                    no_copy=1,
                ),
            ],
            "Comment": [
                dict(
                    fieldname="jira_comment_id",
                    label="Jira Comment ID",
                    fieldtype="Data",
                    insert_after="content",
                    read_only=1,
                    no_copy=1,
                ),
            ],
        },
        ignore_validate=True,
    )
    frappe.db.commit()
