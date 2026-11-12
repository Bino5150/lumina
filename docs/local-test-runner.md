# Running Lumina tests locally

Use the Python 3.13 test environment. On Linux, the runner requires a non-root user systemd manager, cgroup v2 memory, swap, PID and CPU controllers, memory PSI and Bubblewrap with user namespaces. Install the core dependencies from the tests workflow. Add Node 22 for Chrome Companion coverage and PySide6 plus its Linux libraries for Qt. Full inventory discovery uses offscreen Qt so it can see every ordinary and evidence ID.

```sh
scripts/lumina-test smoke
scripts/lumina-test list
scripts/lumina-test shard 18
scripts/lumina-test --profile qt shard 8
scripts/lumina-test report
```

Smoke selects the identity, elapsed-decay, owner-data isolation and harness accounting tests. Each invocation tests a private projection of the checkout. Tracked files use their current working bytes, including local edits. New visible source files with supported code or config extensions are included automatically. Use `git add -N -- path` for a new fixture or asset with another extension. Keep credentials and private runtime state outside versioned source. Git history needed for provenance tests is fetched locally into the projection.

Each pytest process gets private HOME, runtime and network namespaces, short disposable scratch paths, read-only source and a fresh resource check. Smoke uses 768 MiB, inventory collection 1,024 MiB and maintained shards 2,000 MiB, with zero workload swap, 64 tasks and a 50% CPU quota. Smoke and collection have a two-minute limit. Each shard has a ten-minute limit. Refused admission or missing containment exits with an error. A run never retries a shard.

## Profiles and results

| Profile | Coverage |
| --- | --- |
| `core` | Ordinary tests without a required Qt or historical relay fixture. Smoke uses this profile by default. |
| `qt` | Headless GUI and Qt-dependent test files. A failed Qt preflight or unexpected skip blocks. |
| `wire` | Historical relay integration, including Qt-dependent Wire tests. Provide a local AIvengers Wire Git checkout containing both pinned source generations. The runner exports the exact package trees and starts disposable local socket fixtures. |
| `evidence` | Frozen Castle Walls reproductions. Run alone with `--profile evidence shard 29`. Their failures and native exit are recorded as evidence, with a nonzero command status. |

`full` reconciles the entire inventory first, then runs the core and Qt shards in order. Use `--profile core full` for discovery scoped to core test files and newly found test files, without a Qt preflight. Supplying `--relay-repo /path/to/aivengers-release` also includes Wire unless a profile is explicitly selected. Report receipts record which profiles were included, the source fingerprint, native status, pytest outcomes, timing, cgroup peak memory and cleanup. They live under `${XDG_STATE_HOME:-$HOME/.local/state}/lumina-test`. Use `--results PATH` before the command to change the location. `report` fails if the most recent receipt belongs to different source bytes. Use each stage's `pytest.log` and `report.jsonl` to investigate failures.

## Updating selectors

`scripts/lumina_test/inventory.tsv` lists every maintained node ID, shard and profile. Existing assignments were seeded from W1. Historical manifests and evidence have separate archival custody.

1. Give parametrized cases stable IDs when inputs include machine-specific paths or environment values.
2. Use `lumina_qt` or `lumina_wire` markers for new dependency-bound tests. The profile file also records existing file and selector rules. The Castle Walls evidence marker remains authoritative.
3. Run `scripts/lumina-test list --write-inventory /tmp/lumina-inventory.tsv` when tests are added, removed, renamed or reclassified. Drift exits nonzero and writes a proposed inventory if collection completed. Compare the exact changes in the receipt before replacing the maintained file. A rename appears as a removed ID and a new ID.
4. New IDs first use space in their file's existing shards, then fill new numbered shards in stable ID order. Each shard holds at most 550 IDs. Duplicate IDs block inventory generation. Re-run `list` and the affected shard after reviewing the change.

Smoke and single-shard runs reconcile the entire inventory of the selected files. Use `list` to check newly added files across the checkout. Required security tests must execute. An explicitly approved skip needs an exact ID and reason in the profile file. Unexpected skips and xfails block ordinary runs.

## CI qualification

The manual `test-harness-candidate` workflow runs inventory discovery, smoke and one Qt shard with Castle Walls regression coverage. It uploads logs and receipts even after failure. The existing required core and Qt security jobs and the separate non-blocking evidence step are still authoritative.

Before switching CI, qualify an owner-approved full run on the maintained tree. Compare every blocking core and Qt selector in the active workflow with the new inventory and execution receipts. Exercise Linux containment on the intended CI runner. Then gate on a single aggregate result that requires a complete native-zero receipt for each planned ordinary shard. Keep the frozen evidence step separate. Reuse a result only when its source fingerprint, selector inventory, profile, runtime dependencies and containment settings match the required run. The current runner executes fresh stages.
