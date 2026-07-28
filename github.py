import httpx
from typing import Optional, Dict, Any, List, Union
import logging

import config

logger = logging.getLogger(__name__)


class GitHubClient:
    """Thin API client for GitHub API - dumb client with no business logic or state."""
    
    def __init__(self):
        self.base_url = "https://api.github.com"
        self.token = config.GITHUB_TOKEN
        self.repo = config.GITHUB_REPO
        self.timeout = 30.0
        self._client = httpx.Client(timeout=self.timeout)
    
    def _request(self, method: str, path: str, **kwargs) -> Union[Dict[str, Any], str]:
        """Make HTTP request to GitHub API."""
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        
        # Set default Accept header if not provided
        if "Accept" not in headers:
            headers["Accept"] = "application/vnd.github.v3+json"
        
        response = self._client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        
        # Return JSON for most requests, but handle text responses for diff
        if "application/vnd.github.v3.diff" in headers.get("Accept", ""):
            return response.text
        return response.json()
    
    def _log_dry_run(self, operation: str, details: Dict[str, Any]) -> None:
        """Log what would be done in DRY_RUN mode."""
        logger.info(f"[DRY_RUN] Would {operation}: {details}")
    
    def create_issue(self, title: str, body: str, labels: Optional[List[str]] = None) -> int:
        """Create a GitHub issue. Returns issue number."""
        if config.DRY_RUN:
            self._log_dry_run("create_issue", {
                "title": title,
                "body": body,
                "labels": labels,
                "repo": self.repo
            })
            return 1  # Plausible fake value
        
        path = f"/repos/{self.repo}/issues"
        payload = {
            "title": title,
            "body": body,
        }
        if labels:
            payload["labels"] = labels
        
        response = self._request("POST", path, json=payload)
        return response["number"]
    
    def add_labels(self, issue_number: int, labels: List[str]) -> None:
        """Add labels to an issue."""
        if config.DRY_RUN:
            self._log_dry_run("add_labels", {
                "issue_number": issue_number,
                "labels": labels,
                "repo": self.repo
            })
            return
        
        path = f"/repos/{self.repo}/issues/{issue_number}/labels"
        self._request("POST", path, json={"labels": labels})
    
    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue."""
        if config.DRY_RUN:
            self._log_dry_run("remove_label", {
                "issue_number": issue_number,
                "label": label,
                "repo": self.repo
            })
            return
        
        path = f"/repos/{self.repo}/issues/{issue_number}/labels/{label}"
        self._request("DELETE", path)
    
    def comment(self, issue_number: int, body: str) -> None:
        """Add a comment to an issue."""
        if config.DRY_RUN:
            self._log_dry_run("comment", {
                "issue_number": issue_number,
                "body": body,
                "repo": self.repo
            })
            return
        
        path = f"/repos/{self.repo}/issues/{issue_number}/comments"
        self._request("POST", path, json={"body": body})
    
    def list_open_issues(self, label: Optional[str] = None) -> List[Dict[str, Any]]:
        """List open issues, optionally filtered by label. Read function - always executes."""
        path = f"/repos/{self.repo}/issues"
        params = {"state": "open"}
        if label:
            params["labels"] = label
        
        response = self._request("GET", path, params=params)
        return response
    
    def close_issue(self, issue_number: int) -> None:
        """Close an issue."""
        if config.DRY_RUN:
            self._log_dry_run("close_issue", {
                "issue_number": issue_number,
                "repo": self.repo
            })
            return
        
        path = f"/repos/{self.repo}/issues/{issue_number}"
        self._request("PATCH", path, json={"state": "closed"})
    
    def find_pr_for_branch(self, branch: str) -> Optional[Dict[str, Any]]:
        """
        Find a PR for a specific branch.
        Returns dict with: number, head_sha, body, state, merged or None if not found.
        Read function - always executes.
        """
        path = f"/repos/{self.repo}/pulls"
        params = {"state": "all", "head": f"{self.repo.split('/')[0]}:{branch}"}
        
        try:
            response = self._request("GET", path, params=params)
            if response and len(response) > 0:
                pr = response[0]
                return {
                    "number": pr.get("number"),
                    "head_sha": pr.get("head", {}).get("sha"),
                    "body": pr.get("body"),
                    "state": pr.get("state"),
                    "merged": pr.get("merged", False),
                }
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        
        return None
    
    def get_pr_diff(self, number: int) -> str:
        """
        Get the unified diff for a PR.
        Read function - always executes.
        """
        path = f"/repos/{self.repo}/pulls/{number}"
        # Override the default Accept header for diff format
        return self._request("GET", path, headers={"Accept": "application/vnd.github.v3.diff"})
    
    def get_pr_files(self, number: int) -> List[str]:
        """
        Get list of files changed in a PR.
        Read function - always executes.
        """
        path = f"/repos/{self.repo}/pulls/{number}/files"
        response = self._request("GET", path)
        return [file.get("filename") for file in response]
    
    def close(self):
        """Close the HTTP client."""
        self._client.close()


# Global client instance
_client: Optional[GitHubClient] = None


def get_client() -> GitHubClient:
    """Get or create the global GitHub client instance."""
    global _client
    if _client is None:
        _client = GitHubClient()
    return _client
