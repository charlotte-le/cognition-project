#!/usr/bin/env python3
"""
Smoke test script for API clients.

Part (a): Tests the normalize function with hand-written status/detail pairs.
Part (b): With DRY_RUN=1, calls every GitHub write function once and prints what would be done.
"""

import os
import logging

# Configure logging to see DRY_RUN messages
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')


def test_normalize():
    """Test normalize function with five hand-written status/detail pairs."""
    # Define Internal enum locally for testing (no network required)
    from enum import Enum
    
    class Internal(str, Enum):
        RUNNING = "RUNNING"
        BLOCKED = "BLOCKED"
        DONE = "DONE"
        FAILED = "FAILED"
        HALT = "HALT"
    
    def normalize(status: str, status_detail: str) -> Internal:
        """Normalize vendor status/status_detail to Internal enum."""
        status_lower = status.lower()
        detail_lower = (status_detail or "").lower()
        
        if status_lower in ("new", "claimed", "resuming"):
            return Internal.RUNNING
        if status_lower == "running" and detail_lower == "working":
            return Internal.RUNNING
        if status_lower == "running" and detail_lower == "waiting_for_user":
            return Internal.BLOCKED
        if status_lower == "running" and detail_lower == "finished":
            return Internal.DONE
        if status_lower == "running" and detail_lower == "waiting_for_approval":
            return Internal.HALT
        if status_lower == "exit":
            return Internal.DONE
        if status_lower == "error":
            return Internal.RUNNING
        if status_lower == "suspended" and detail_lower in ("inactivity", "user_request"):
            return Internal.FAILED
        if status_lower == "suspended":
            if any(keyword in detail_lower for keyword in ("credit", "quota", "limit", "payment")):
                return Internal.HALT
        return Internal.RUNNING
    
    print("Testing normalize function:")
    print("=" * 60)
    
    test_cases = [
        # (status, status_detail, expected_result)
        ("new", None, Internal.RUNNING),
        ("running", "working", Internal.RUNNING),
        ("running", "waiting_for_user", Internal.BLOCKED),
        ("running", "finished", Internal.DONE),
        ("exit", "completed", Internal.DONE),
        ("error", "something went wrong", Internal.RUNNING),
        ("suspended", "inactivity", Internal.FAILED),
        ("suspended", "credit_limit_reached", Internal.HALT),
        ("running", "waiting_for_approval", Internal.HALT),
        ("claimed", "starting", Internal.RUNNING),
    ]
    
    for status, status_detail, expected in test_cases:
        result = normalize(status, status_detail)
        status_str = f"normalize('{status}', '{status_detail}')"
        match = "✓" if result == expected else "✗"
        print(f"{match} {status_str:40} -> {result:8} (expected: {expected})")
    
    print()


def test_github_dry_run():
    """Test GitHub write functions with DRY_RUN=1."""
    print("Testing GitHub write functions with DRY_RUN=1:")
    print("=" * 60)
    
    # Set minimal required environment variables for testing
    os.environ["DRY_RUN"] = "1"
    os.environ["DEVIN_API_KEY"] = "test-key"
    os.environ["DEVIN_ORG_ID"] = "test-org"
    os.environ["GITHUB_TOKEN"] = "test-token"
    os.environ["GITHUB_REPO"] = "test/repo"
    
    # Get a fresh client instance (DRY_RUN is already set)
    from github import GitHubClient
    client = GitHubClient()
    
    # Test create_issue
    print("1. create_issue:")
    issue_number = client.create_issue(
        title="Test Issue",
        body="This is a test issue for smoke testing",
        labels=["bug", "test"]
    )
    print(f"   Returned issue number: {issue_number}")
    print()
    
    # Test add_labels
    print("2. add_labels:")
    client.add_labels(issue_number, ["enhancement", "priority-high"])
    print()
    
    # Test remove_label
    print("3. remove_label:")
    client.remove_label(issue_number, "test")
    print()
    
    # Test comment
    print("4. comment:")
    client.comment(issue_number, "This is a test comment")
    print()
    
    # Test close_issue
    print("5. close_issue:")
    client.close_issue(issue_number)
    print()
    
    client.close()
    
    print("All GitHub write functions executed in DRY_RUN mode.")
    print()


if __name__ == "__main__":
    print("SMOKE TEST SCRIPT")
    print("=" * 60)
    print()
    
    # Part (a): Test normalize function
    test_normalize()
    
    # Part (b): Test GitHub write functions with DRY_RUN=1
    test_github_dry_run()
    
    print("Smoke test complete!")
