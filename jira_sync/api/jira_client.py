"""Thin client for the Jira Cloud REST API v3 (basic auth: email + API token)."""

import frappe
import requests
from requests.auth import HTTPBasicAuth


class JiraClient:
    def __init__(self, settings=None):
        self.settings = settings or frappe.get_cached_doc("Jira Sync Settings")
        self.base_url = (self.settings.jira_url or "").rstrip("/")
        token = self.settings.get_password("api_token")
        self.auth = HTTPBasicAuth(self.settings.jira_user_email, token)
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # -- low level -----------------------------------------------------------
    def request(self, method, path, **kwargs):
        url = f"{self.base_url}{path}"
        resp = requests.request(
            method, url, auth=self.auth, headers=self.headers, timeout=30, **kwargs
        )
        if not resp.ok:
            frappe.log_error(
                title=f"Jira API {method} {path} -> {resp.status_code}",
                message=resp.text[:4000],
            )
            resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, payload):
        return self.request("POST", path, json=payload)

    def put(self, path, payload):
        return self.request("PUT", path, json=payload)

    def delete(self, path):
        return self.request("DELETE", path)

    # -- projects --------------------------------------------------------------
    def get_projects(self):
        """All live Jira projects visible to the integration account."""
        projects, start = [], 0
        while True:
            page = self.get(
                "/rest/api/3/project/search",
                params={"startAt": start, "maxResults": 50, "status": "live"},
            )
            values = page.get("values", [])
            projects.extend(values)
            if page.get("isLast") or not values:
                break
            start += len(values)
        return projects

    # -- issues --------------------------------------------------------------
    def create_issue(self, project_key, summary, description=None, issue_type=None):
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "issuetype": {"name": issue_type or self.settings.default_issue_type or "Task"},
            }
        }
        if description:
            payload["fields"]["description"] = text_to_adf(description)
        return self.post("/rest/api/3/issue", payload)

    def update_issue(self, issue_key, fields):
        return self.put(f"/rest/api/3/issue/{issue_key}", {"fields": fields})

    def get_issue(self, issue_key):
        return self.get(f"/rest/api/3/issue/{issue_key}")

    def transition_issue(self, issue_key, status_name):
        """Move an issue to the transition whose target status matches status_name."""
        transitions = self.get(f"/rest/api/3/issue/{issue_key}/transitions").get(
            "transitions", []
        )
        for t in transitions:
            if t.get("to", {}).get("name", "").lower() == status_name.lower():
                self.post(
                    f"/rest/api/3/issue/{issue_key}/transitions", {"transition": {"id": t["id"]}}
                )
                return True
        return False

    def search_issues(self, jql, fields=None, next_page_token=None, max_results=100):
        payload = {
            "jql": jql,
            "fields": fields
            or ["summary", "description", "status", "updated", "project", "assignee"],
            "maxResults": max_results,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token
        return self.post("/rest/api/3/search/jql", payload)

    def assign_issue(self, issue_key, account_id):
        """Set (or clear, with account_id=None) the assignee of an issue."""
        return self.put(f"/rest/api/3/issue/{issue_key}/assignee", {"accountId": account_id})

    # -- users -----------------------------------------------------------------
    def get_account_id_for_email(self, email):
        """Resolve a Jira accountId from an email address (cached for a day).

        User search matches the email even when the profile hides it, so this
        works regardless of the user's email visibility setting.
        """
        if not email:
            return None
        cache_key = f"jira_sync::account_id::{email.lower()}"
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return cached
        users = self.get("/rest/api/3/user/search", params={"query": email}) or []
        candidates = [
            u
            for u in users
            if u.get("active", True) and u.get("accountType", "atlassian") == "atlassian"
        ]
        account_id = None
        for u in candidates:
            if (u.get("emailAddress") or "").lower() == email.lower():
                account_id = u.get("accountId")
                break
        if not account_id and len(candidates) == 1:
            # email hidden by privacy settings, but the query matched exactly one user
            account_id = candidates[0].get("accountId")
        if account_id:
            frappe.cache().set_value(cache_key, account_id, expires_in_sec=24 * 60 * 60)
        return account_id

    # -- comments --------------------------------------------------------------
    def add_comment(self, issue_key, text):
        return self.post(
            f"/rest/api/3/issue/{issue_key}/comment", {"body": text_to_adf(text)}
        )

    # -- worklogs --------------------------------------------------------------
    def add_worklog(self, issue_key, seconds, started_iso, comment=None):
        payload = {"timeSpentSeconds": max(60, int(seconds)), "started": started_iso}
        if comment:
            payload["comment"] = text_to_adf(comment)
        return self.post(f"/rest/api/3/issue/{issue_key}/worklog", payload)

    def delete_worklog(self, issue_key, worklog_id):
        return self.delete(f"/rest/api/3/issue/{issue_key}/worklog/{worklog_id}")


def text_to_adf(text):
    """Convert plain text into Atlassian Document Format."""
    paragraphs = [p for p in (text or "").split("\n") if p.strip()] or [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            for p in paragraphs
        ],
    }


def adf_to_text(adf):
    """Best-effort flatten of ADF back to plain text."""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""

    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                out.append(node.get("text", ""))
            for child in node.get("content", []) or []:
                walk(child)
            if node.get("type") == "paragraph":
                out.append("\n")
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(adf)
    return "".join(out).strip()
