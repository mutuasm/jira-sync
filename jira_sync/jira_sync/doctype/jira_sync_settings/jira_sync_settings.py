import frappe
from frappe import _
from frappe.model.document import Document


class JiraSyncSettings(Document):
    def validate(self):
        if self.enabled:
            self.jira_url = (self.jira_url or "").rstrip("/")
            self.detect_account_id()

    def detect_account_id(self):
        """Fetch the account id of the integration user (used for loop prevention)."""
        from jira_sync.api.jira_client import JiraClient

        try:
            client = JiraClient(settings=self)
            me = client.get("/rest/api/3/myself")
            self.jira_account_id = me.get("accountId")
        except Exception:
            frappe.msgprint(
                _("Could not connect to Jira. Check URL, email and API token."),
                indicator="red",
            )
            raise


@frappe.whitelist()
def test_connection():
    from jira_sync.api.jira_client import JiraClient

    client = JiraClient()
    me = client.get("/rest/api/3/myself")
    return {"ok": True, "user": me.get("displayName"), "account_id": me.get("accountId")}
