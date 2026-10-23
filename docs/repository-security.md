# Repository security and merge policy

Roost uses pull requests, mandatory CI, publication checks and restricted automation to protect its public source. The application remains private to its owner; publishing the source does not authorize a public Roost deployment.

## Branch rules

| Scope | Rules |
| --- | --- |
| `main` | Pull request required; zero required approvals; review conversations resolved; required `check` from GitHub Actions; branch current with the base; linear history; squash merges; force pushes and deletion blocked. |
| All branches | Force pushes blocked, including work branches. |
| Work branches | Short-lived `feat/`, `fix/`, `docs/` and `security/` branches from current `main`; deleted after merge. |

Both rulesets are active and contain no bypass actors. Reviewer and code-owner approvals are optional so the solo maintainer can merge a passing pull request. `CODEOWNERS` requests maintainer review for contributions; it is not an approval requirement. Repository administrators can still edit the settings themselves, so repository rules do not replace security of the owner account.

The integration branch is `main`. New changes follow the [contribution workflow](../CONTRIBUTING.md). There is no deployment workflow attached to a merge.

## Checks and automation

The required `check` job runs the public-content working-tree and history scans, Markdown link checks, unit tests and demo smoke check. Its name is part of the ruleset: a rename requires updating the required context before merging that workflow change.

GitHub Actions defaults to read-only token permissions and cannot approve pull requests. The workflow explicitly requests only `contents: read`, removes persisted checkout credentials, has a ten-minute timeout and cancels superseded runs for the same event and branch or pull request.

External actions are restricted to `actions/checkout`, and GitHub requires full-length commit SHA references. Repository-local actions and workflows remain permitted by GitHub's selected-actions policy. The checked-in checkout SHA was resolved from the upstream action repository. Dependabot can propose a new SHA; it cannot merge its own change. Adding another external action requires reviewing it and updating the allowlist.

The checks run proposed code on GitHub-hosted runners. Do not add deployment secrets, live agent credentials or self-hosted access to this workflow. Keep pull-request execution unprivileged; do not run proposed code through `pull_request_target` with write access or secrets.

## Disclosure and dependency protection

GitHub secret scanning and push protection are enabled. The local pre-push guard also scans what each push would publish and blocks credential-shaped content, private identifiers and unreviewed binaries. Both are pattern-based controls, not a guarantee that every possible secret will be detected.

Dependabot alerts and security update PRs are enabled. Weekly version update PRs cover Python dependencies and GitHub Actions. Updates pass the same required CI and squash-merge workflow as other changes. Review dependency changes before merging; alerts and updates are not automatically applied to a running host.

Vulnerabilities can be reported through [private vulnerability reporting](https://github.com/calebbrinkley/roost/security/advisories/new). Follow [SECURITY.md](../SECURITY.md) for disclosure response, and never paste a secret into a public issue or pull request.

## Configuration records

| File | GitHub setting |
| --- | --- |
| [main.json](../.github/rulesets/main.json) | Ruleset for `main`. |
| [all-branches.json](../.github/rulesets/all-branches.json) | Force-push protection across branches. |
| [repository-settings.json](../.github/repository-settings.json) | Merge methods and work-branch cleanup. |
| [actions-permissions.json](../.github/actions-permissions.json) | Selected actions and mandatory SHA pinning. |
| [allowed-actions.json](../.github/allowed-actions.json) | External action allowlist. |
| [workflow-permissions.json](../.github/workflow-permissions.json) | Default token access and approval prohibition. |
| [dependabot.yml](../.github/dependabot.yml) | Weekly dependency update PRs. |

These files record the intended settings; GitHub does not automatically apply the JSON files. Administrators apply changes through GitHub and verify the returned settings. Live settings can drift, so inspect them before claiming that a rule is enforced.

Read the live rules and automation settings with GitHub CLI:

```bash
gh api repos/calebbrinkley/roost/rulesets
gh api repos/calebbrinkley/roost/rules/branches/main
gh api repos/calebbrinkley/roost/actions/permissions
gh api repos/calebbrinkley/roost/actions/permissions/selected-actions
gh api repos/calebbrinkley/roost/actions/permissions/workflow
```

When updating an existing ruleset, obtain its current ID first and use an update request. Do not create a duplicate ruleset or weaken protection to make a check pass. Document the final policy and read it back after a change.

## References

- [GitHub repository rules](https://docs.github.com/en/rest/repos/rules)
- [GitHub Actions security guidance](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub Actions permissions](https://docs.github.com/en/rest/actions/permissions)
