# Triage existing vulnerabilities

Given a list of reported findings, verify each one by:

1. Read the exact file + line mentioned in the finding.
2. Confirm the vulnerability still exists (not already patched).
3. Assess exploitability: is this reachable from untrusted input?
4. Assign CVSS-like severity.
5. Suggest concrete remediation.
