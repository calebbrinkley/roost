# Security and accidental disclosure

Do not open a public issue containing credentials, identifying host details, private logs or conversation transcripts.

Use the repository's private vulnerability reporting feature if it is enabled. If it is unavailable, contact the maintainer through an already established private channel. Do not invent or publish a personal contact address.

For a suspected disclosure:

1. Stop further publication and preserve a private record of affected files/commits.
2. Revoke or rotate affected credentials through the relevant service.
3. Remove the exposed content from current files and investigate published history, release assets and copies.
4. Coordinate any history rewrite with the maintainer; deleting the current file does not remove old commits or third-party copies.
5. Verify the replacement is sanitized before publishing. Do not repeat the secret in a report, commit message or remediation test.

Roost holds the Hermes dashboard password, which administers the agent. Report anything that lets a browser reach a Hermes route Roost does not deliberately forward, or that weakens sign-in, with a minimal generic reproduction and the affected version.
