"""Ghost Data Still Exists - deletion audit engine.

Pipeline (per case): request_text -> intent -> NOT_APPLICABLE or
identity -> evidence observations -> resource linkage -> state at audit_at ->
policy (deletion_rules.csv) -> ordered findings / remediation / verification.
"""
