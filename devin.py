import httpx
from enum import Enum
from typing import Optional, Dict, Any, List
import time
import logging
import uuid

import config

logger = logging.getLogger(__name__)


class Internal(str, Enum):
    """Internal status enum - raw vendor status strings must never leave this module."""
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    FAILED = "FAILED"
    HALT = "HALT"


def normalize(status: str, status_detail: Optional[str]) -> Internal:
    """
    Normalize vendor status/status_detail to Internal enum.
    
    IMPORTANT: completion is running + finished, not exit.
    IMPORTANT: error is transient — it must not count against the attempt cap.
    """
    status_lower = status.lower()
    detail_lower = (status_detail or "").lower()
    
    # new, claimed, resuming -> any detail -> RUNNING
    if status_lower in ("new", "claimed", "resuming"):
        return Internal.RUNNING
    
    # running -> working -> RUNNING
    if status_lower == "running" and detail_lower == "working":
        return Internal.RUNNING
    
    # running -> waiting_for_user -> BLOCKED
    if status_lower == "running" and detail_lower == "waiting_for_user":
        return Internal.BLOCKED
    
    # running -> finished -> DONE
    # IMPORTANT: completion is running + finished, not exit
    if status_lower == "running" and detail_lower == "finished":
        return Internal.DONE
    
    # running -> waiting_for_approval -> HALT
    if status_lower == "running" and detail_lower == "waiting_for_approval":
        return Internal.HALT
    
    # exit -> any detail -> DONE
    if status_lower == "exit":
        return Internal.DONE
    
    # error -> any detail -> RUNNING
    # IMPORTANT: error is transient — it must not count against the attempt cap
    if status_lower == "error":
        return Internal.RUNNING
    
    # suspended -> inactivity, user_request -> FAILED
    if status_lower == "suspended" and detail_lower in ("inactivity", "user_request"):
        return Internal.FAILED
    
    # suspended -> anything containing credit, quota, limit, payment -> HALT
    if status_lower == "suspended":
        if any(keyword in detail_lower for keyword in ("credit", "quota", "limit", "payment")):
            return Internal.HALT
    
    # anything else -> RUNNING
    return Internal.RUNNING


class DevinClient:
    """Thin API client for Devin API - dumb client with no business logic or state."""
    
    def __init__(self):
        self.base_url = config.DEVIN_API_BASE
        self.api_key = config.DEVIN_API_KEY
        self.org_id = config.DEVIN_ORG_ID
        self.timeout = 30.0
        self._client = httpx.Client(timeout=self.timeout)
    
    def _log_dry_run(self, operation: str, details: Dict[str, Any]) -> None:
        """Log what would be done in DRY_RUN mode."""
        logger.info(f"[DRY_RUN] Would {operation}: {details}")

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """Make HTTP request with simple backoff on 5xx errors."""
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.api_key}"
        
        max_retries = 3
        for attempt in range(max_retries):
            response = self._client.request(method, url, headers=headers, **kwargs)
            
            # Simple backoff on 5xx errors
            if response.status_code >= 500:
                if attempt < max_retries - 1:
                    backoff = 2 ** attempt
                    logger.warning(f"5xx error on {method} {path}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(backoff)
                    continue
            
            response.raise_for_status()
            return response.json()
        
        raise Exception(f"Max retries exceeded for {method} {path}")
    
    def create_session(
        self,
        prompt: str,
        title: Optional[str] = None,
        repo: Optional[str] = None,
        max_acu: Optional[int] = None,
        session_secrets: Optional[Dict[str, Any]] = None,
        schema: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a new Devin session.
        
        Returns SessionResponse with fields: session_id, url, status, acus_consumed.
        IMPORTANT: structured_output and status_detail are never populated on create response.
        """
        if config.DRY_RUN:
            self._log_dry_run("create_session", {
                "title": title,
                "repo": repo or config.GITHUB_REPO,
                "max_acu": max_acu or config.MAX_ACU_PER_SESSION,
                "prompt": prompt,
            })
            # Plausible fake values, shaped like a real create response.
            return {
                "session_id": f"dry-run-{uuid.uuid4().hex}",
                "url": "https://app.devin.ai/sessions/dry-run",
                "status": "new",
                "acus_consumed": 0,
            }

        body = {
            "prompt": prompt,
            "tags": [config.SESSION_TAG],
            "repos": [repo or config.GITHUB_REPO],
            "max_acu_limit": max_acu or config.MAX_ACU_PER_SESSION,
            "bypass_approval": True,
            "structured_output_required": True,
            "structured_output_schema": schema or {},
        }
        
        if title:
            body["title"] = title
        
        response = self._request(
            "POST",
            f"/v3/organizations/{self.org_id}/sessions",
            json=body
        )
        
        # Only return the fields we care about
        return {
            "session_id": response.get("session_id"),
            "url": response.get("url"),
            "status": response.get("status"),
            "acus_consumed": response.get("acus_consumed"),
        }
    
    def list_tagged_sessions(self) -> List[Dict[str, Any]]:
        """
        List sessions tagged with cfg.SESSION_TAG.
        
        Returns list of sessions with: session_id, status, status_detail, acus_consumed,
        pull_requests[{pr_url, pr_state}], structured_output, title, url.
        """
        response = self._request(
            "GET",
            f"/v3/organizations/{self.org_id}/sessions/insights"
        )
        
        # Filter to sessions whose tags contain config.SESSION_TAG and are not archived
        tagged_sessions = []
        for session in response.get("items", []):
            tags = session.get("tags", [])
            is_archived = session.get("is_archived", False)
            if config.SESSION_TAG in tags and not is_archived:
                tagged_sessions.append({
                    "session_id": session.get("session_id"),
                    "status": session.get("status"),
                    "status_detail": session.get("status_detail"),
                    "acus_consumed": session.get("acus_consumed"),
                    "pull_requests": session.get("pull_requests", []),
                    "structured_output": session.get("structured_output"),
                    "title": session.get("title"),
                    "url": session.get("url"),
                })
        
        return tagged_sessions
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific session by ID.
        
        Returns session dict with: session_id, status, status_detail, acus_consumed,
        pull_requests[{pr_url, pr_state}], structured_output, title, url.
        Returns None if session not found.
        """
        try:
            response = self._request(
                "GET",
                f"/v3/organizations/{self.org_id}/sessions/{session_id}"
            )
            
            # Return the fields we care about
            return {
                "session_id": response.get("session_id"),
                "status": response.get("status"),
                "status_detail": response.get("status_detail"),
                "acus_consumed": response.get("acus_consumed"),
                "pull_requests": response.get("pull_requests", []),
                "structured_output": response.get("structured_output"),
                "title": response.get("title"),
                "url": response.get("url"),
            }
        except Exception as e:
            logger.warning(f"Failed to get session {session_id}: {e}")
            return None
    
    def send_message(self, session_id: str, message: str) -> None:
        """
        Send a message to a session.
        Sending a message auto-resumes a suspended session.
        """
        if config.DRY_RUN:
            self._log_dry_run("send_message", {
                "session_id": session_id,
                "message": message,
            })
            return

        self._request(
            "POST",
            f"/v3/organizations/{self.org_id}/sessions/{session_id}/messages",
            json={"message": message}
        )
    
    def close(self):
        """Close the HTTP client."""
        self._client.close()


# Global client instance
_client: Optional[DevinClient] = None


def get_client() -> DevinClient:
    """Get or create the global Devin client instance."""
    global _client
    if _client is None:
        _client = DevinClient()
    return _client
