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
| `wire` | Historical relay integration, including Qt-dependent Wire tests. Provide a local AIvengers Wire Git checkout containing every pinned source generation (two historical relays and the attention-capable relay that Lumina's autonomous Wire conversations are tested against). The runner exports the exact package trees and starts disposable local socket fixtures. |
| `evidence` | Frozen Castle Walls reproductions. Run alone with `--profile evidence shard 29`. Their failures and native exit are recorded as evidence, with a nonzero command status. |

`full` reconciles the entire inventory first, then runs the core and Qt shards in order. Use `--profile core full` for discovery scoped to core test files and newly found test files, without a Qt preflight. Supplying `--relay-repo /path/to/aivengers-release` also includes Wire unless a profile is explicitly selected. Report receipts record which profiles were included, the source fingerprint, native status, pytest outcomes, timing, cgroup peak memory and cleanup. They live under `${XDG_STATE_HOME:-$HOME/.local/state}/lumina-test`. Use `--results PATH` before the command to change the location. `report` fails if the most recent receipt belongs to different source bytes. Use each stage's `pytest.log` and `report.jsonl` to investigate failures.

## Temporary dependency exception: the attention relay

The `attention` Wire generation is pinned to a commit that so far exists only in the local AIvengers Wire checkout (it is not on GitHub until the owner publishes it), so CI and other machines cannot stage it yet. Every Wire-profile receipt prints this as `TEMPORARY DEPENDENCY EXCEPTION` and records it under `temporary_dependency_exceptions`; the text lives in `TEMPORARY_DEPENDENCY_EXCEPTIONS` in `scripts/lumina_test/source.py`. Resolve it by publishing the commit (a merge commit keeps its SHA; if it is squashed, repin to the new commit, whose `src/aivengers_wire` tree must equal the pinned tree) and then delete the entry. Tests that need that relay are not skipped: they run in the Wire profile against the pinned tree.

## Known failing units on release main (recorded, not waived)

Six shard units do not pass on pristine release `main` `a2f38c1` (the parent of the WIRE-AUTONOMY-01 commits), measured with this runner. They fail identically with and without those commits (same test IDs, same counts), they have no entry in `allowed_skips`, and they are still in the inventory, so they keep failing runs until someone fixes them. Their causes have not been investigated here.

| Unit | Result on `a2f38c1` | IDs |
| --- | --- | --- |
| core 005 | native exit 1, 1 failure | `tests/test_agent_runaway_suspend_01_c1b.py::test_the_eval_harness_never_initializes_outside_its_scratch_root` |
| core 013 | native exit 1, 1 failure | `tests/test_eval_scratch_dir.py::test_run_eval_wipes_stale_scratch_dir_before_any_project_import` |
| core 019 | native exit 1, 3 failures | `tests/test_run_eval_persona.py::test_run_task_resolves_and_threads_named_persona`, `::test_run_task_unknown_persona_name_falls_back_gracefully`, `::test_run_task_without_persona_key_passes_none_unchanged` |
| core 020 | native exit 1, 7 failures | `tests/test_sandbox.py::test_exception_captured_not_raised`, `::test_memory_rlimit_blocks_large_allocation`, `::test_no_output_case`, `::test_normal_execution`, `::test_secrets_not_inherited_from_app_env`, `::test_timeout_clamped_to_max`, `::test_timeout_is_actually_enforced` |
| core 021 | native exit 1, 1 failure and a "missing or repeated structured pytest collection" error | `tests/test_site_action_claims.py::test_spawned_processes_have_one_durable_winner` |
| qt 028 | native exit 0, runner status FAIL: 3 unexpected skips | `tests/test_wire_wake_adapter_01.py::TestReleaseLifecycleR3::test_release_records_failed_with_note`, `::TestSafetyGates::test_production_isolation`, `::TestSafetyGates::test_token_never_logged` |

The qt 028 skips all report "aivengers release source not found; relay-bound W1 tests require it": they are relay-bound tests listed under the `qt` profile, where no relay is staged.

A run can also end with `host resource admission refused` (available memory below the runner's floor, or swap pressure). That ran no test and is not a result: free memory and rerun the unit.

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
