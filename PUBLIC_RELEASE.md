# Public source snapshot

This repository publishes the current source tree as a fresh initial commit. Previous Git history, developer email metadata, local `.env` files, runtime state, usage ledgers, logs, temporary directories, and credential files are excluded.

The Bailian workspace-specific hostname, Windows username, and machine-specific absolute paths in this public copy use example values. Replace them with your own values before deployment. The published qualification records describe historical observations; they do not qualify a new installation.

Provider and gateway client credentials belong only in an untracked local `.env` or a secret manager. Copy `.env.example` to `.env` and provide your own credentials. Never expose the local gateway directly to the public internet.

Before publishing changes, run Gitleaks against both the working tree and Git history, and run the offline acceptance suite. Secret scanning reduces risk but does not guarantee the absence of every possible secret.
