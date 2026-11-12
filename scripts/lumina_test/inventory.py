"""Read and reconcile the maintained pytest inventory."""
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

PROFILES = ("core", "qt", "wire", "evidence")
SHARD_LIMIT = 550
SMOKE_FILES = {
    "tests/test_aaak_identity_safety.py",
    "tests/test_lumina_test_harness.py",
    "tests/test_temporal_decay.py",
    "tests/test_test_data_isolation_01.py",
}


@dataclass(frozen=True)
class Entry:
    shard: int
    profile: str
    node: str

    @property
    def file(self):
        return self.node.split("::", 1)[0]


def check_node(node):
    file, separator, test = node.partition("::")
    path = PurePosixPath(file)
    if (not separator or not test or path.parts[:1] != ("tests",)
            or ".." in path.parts or path.as_posix() != file
            or not path.name.startswith("test_") or path.suffix != ".py"
            or any(ord(char) < 32 for char in node)):
        raise ValueError(f"invalid pytest node ID {node!r}")


def read_inventory(path):
    entries = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if line.startswith("#") or not line:
            continue
        shard, profile, node = line.split("\t")
        check_node(node)
        index = int(shard)
        if (profile not in PROFILES or index < 1 or index > 999 or node in entries):
            raise ValueError(f"invalid or duplicate inventory row {number}: {line!r}")
        entries[node] = Entry(index, profile, node)
    if not entries or max(Counter(row.shard for row in entries.values()).values()) > SHARD_LIMIT:
        raise ValueError("empty inventory or shard exceeds selector limit")
    return entries


def inventory_text(entries):
    rows = sorted(entries.values(), key=lambda row: (row.shard, row.node))
    return "# shard\tprofile\tpytest node ID\n" + "".join(
        f"{row.shard:03d}\t{row.profile}\t{row.node}\n" for row in rows
    )


def compare(expected, observed):
    seen = {}
    duplicate = set()
    for node, profile in observed:
        check_node(node)
        if profile not in PROFILES:
            raise ValueError(f"invalid profile for {node}: {profile}")
        if node in seen:
            duplicate.add(node)
        seen[node] = profile
    return {
        "missing": sorted(expected.keys() - seen.keys()),
        "new": sorted(seen.keys() - expected.keys()),
        "duplicates": sorted(duplicate),
        "profile_changes": {
            node: [expected[node].profile, seen[node]]
            for node in sorted(expected.keys() & seen.keys())
            if expected[node].profile != seen[node]
        },
    }


def update_inventory(previous, observed):
    drift = compare(previous, observed)
    if drift["duplicates"]:
        raise ValueError("duplicate pytest IDs cannot be assigned")
    retained = {
        node: replace(previous[node], profile=profile)
        for node, profile in observed if node in previous
    }
    counts = Counter(row.shard for row in retained.values())
    file_shards = {}
    for row in retained.values():
        file_shards.setdefault(row.file, set()).add(row.shard)
    last_shard = max((row.shard for row in previous.values()), default=0)
    overflow = last_shard + 1
    for node, profile in sorted(observed):
        if node in retained:
            continue
        file = node.split("::", 1)[0]
        candidates = sorted(file_shards.get(file, ()))
        available = next((shard for shard in candidates if counts[shard] < SHARD_LIMIT), None)
        if available is None:
            if counts[overflow] >= SHARD_LIMIT:
                overflow += 1
            available = overflow
        retained[node] = Entry(available, profile, node)
        file_shards.setdefault(file, set()).add(available)
        counts[available] += 1
    if overflow > 999:
        raise ValueError("inventory exceeds shard numbering capacity")
    return retained


def groups(entries, command, number, profiles):
    chosen = {}
    for row in sorted(entries.values(), key=lambda row: (row.shard, row.node)):
        if row.profile not in profiles:
            continue
        if command == "shard" and row.shard != number:
            continue
        if command == "smoke" and row.file not in SMOKE_FILES:
            continue
        key = 0 if command == "smoke" else row.shard
        chosen.setdefault(key, []).append(row)
    if not chosen:
        raise ValueError("no selectors match the command and profiles")
    return chosen
