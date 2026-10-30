# Security and accidental disclosure

Do not open a public issue containing credentials, identifying host details, private logs or conversation transcripts.

Use GitHub's [private vulnerability reporting](https://github.com/calebbrinkley/roost/security/advisories/new). If it is unavailable, contact the maintainer through an already established private channel. Do not invent or publish a personal contact address.

For a suspected disclosure:

1. Stop further publication and preserve a private record of affected files/commits.
2. Revoke or rotate affected credentials through the relevant service.
3. Remove the exposed content from current files and investigate published history, release assets and copies.
4. Coordinate any history rewrite with the maintainer; deleting the current file does not remove old commits or third-party copies.
5. Verify the replacement is sanitized before publishing. Do not repeat the secret in a report, commit message or remediation test.

Roost holds model and speech API keys, account sessions, passkeys, chat data, saved memory,
job results and browser push subscriptions. Report authentication/origin bypasses, credential
exposure, private-data leaks, unsafe page fetching or weakening of the public-web address guard.
Provide a minimal synthetic reproduction and the affected version, without private state or logs.

Deploy one owner-facing process behind trusted HTTPS. Keep its writable state directory private.
Provider keys stay in separate server-side files and never appear in API responses or exports.
A configured remote primary or fallback receives selected prompt data, including memory and
attachments. Web pages and attachments are untrusted input; model output is not authority to
execute arbitrary code. Roost exposes only its allowlisted tools. Push destinations are restricted
to supported browser services. Search, model and speech bases are owner-configured trusted services;
model-selected page URLs are independently restricted to public HTTP(S) destinations.

For branch rules, CI permissions and dependency updates, see [repository security](docs/repository-security.md). These controls help prevent unsafe publication; they do not replace code review, credential rotation or live deployment validation.
