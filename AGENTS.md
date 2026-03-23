# Workspace Operating Rules

1. For OCI monitoring work in this repository, run all OCI API/CLI operations through the remote OCI Mon MCP server tools.
2. Do not execute OCI API/CLI calls from the local computer/workspace environment.
3. If a task appears to require local OCI calls, stop and ask the user before proceeding.
4. For monitoring query results, render inline visualizations whenever chart artifacts are returned.
5. If an inline chart fails to render, immediately regenerate fresh artifacts and provide both:
   - the inline image embed, and
   - a direct clickable artifact URL as fallback.
6. Show up to 10 result rows in responses by default.
7. When total results exceed 10, add a clear note that the full result set can be downloaded as CSV.
8. Only when repo files are changed, provide a commit-ready Title and Summary for the user.
9. Do not provide a commit-ready Title and Summary for read-only tasks, including monitoring queries, investigations, explanations, or other requests that do not modify repository files.
