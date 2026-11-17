"""v3 runner lockfile derivation (agent/runner/bun2npm.py): bun's nesting, resolution and the npm lock entries."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("v3_bun2npm", ROOT / "agent" / "runner" / "bun2npm.py")
b2n = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b2n)

PACKAGES = {
    "@cline/sdk": ["@cline/sdk@workspace:sdk/packages/sdk"],
    "@cline/core": ["@cline/core@workspace:sdk/packages/core"],
    "zod": ["zod@4.4.3", "", {}, "sha512-top"],
    "@cline/core/zod": ["zod@3.25.76", "", {}, "sha512-nested"],
    "left-pad": ["left-pad@1.3.0", "", {"dependencies": {"zod": "^4"}, "peerDependencies": {"react": "*"},
                                         "optionalPeers": ["react"]}, "sha512-lp"],
    "fsevents": ["fsevents@2.3.3", "", {"os": "darwin"}, "sha512-fs"],
    "string-width-cjs": ["string-width@4.2.3", "", {}, "sha512-sw"],
}
PUBLISHED = {
    "@cline/sdk": {"name": "@cline/sdk", "version": "0.2.0", "dependencies": {"@cline/core": "0.2.0"}},
    "@cline/core": {"name": "@cline/core", "version": "0.2.0",
                    "dependencies": {"zod": "^3.25", "left-pad": "^1.3.0", "string-width-cjs": "npm:string-width@^4"},
                    "optionalDependencies": {"fsevents": "~2.3.3"},
                    "peerDependencies": {"posthog-node": "^5"}, "peerDependenciesMeta": {"posthog-node": {"optional": True}}},
}


def test_split_key_keeps_scoped_names_together():
    assert b2n.split_key("@a/b/c/@d/e") == ["@a/b", "c", "@d/e"]
    assert b2n.split_key("zod") == ["zod"]


def test_resolve_prefers_the_innermost_nesting_level():
    assert b2n.resolve(PACKAGES, ["@cline/core"], "zod") == "@cline/core/zod"
    assert b2n.resolve(PACKAGES, ["left-pad"], "zod") == "zod"
    assert b2n.resolve(PACKAGES, ["left-pad"], "missing") is None


def test_closure_follows_deps_skips_optional_peers_and_marks_optional_only():
    required, missing = b2n.closure(PACKAGES, PUBLISHED, ["@cline/sdk"], include_optional=False)
    everything, _ = b2n.closure(PACKAGES, PUBLISHED, ["@cline/sdk"], include_optional=True)
    assert required == {"@cline/sdk", "@cline/core", "@cline/core/zod", "left-pad", "zod", "string-width-cjs"}
    assert everything - required == {"fsevents"} and missing == []


def test_lock_entries_keep_bun_versions_hashes_and_aliases():
    registry = {"packages": {"node_modules/@cline/core": {"resolved": "https://r/core-0.2.0.tgz", "integrity": "sha512-c"}}}
    nested = b2n.lock_entry(PACKAGES, "@cline/core/zod", PUBLISHED, registry)
    assert nested == {"version": "3.25.76", "resolved": "https://registry.npmjs.org/zod/-/zod-3.25.76.tgz",
                      "integrity": "sha512-nested"}
    alias = b2n.lock_entry(PACKAGES, "string-width-cjs", PUBLISHED, registry)
    assert alias["name"] == "string-width" and alias["resolved"].endswith("/string-width/-/string-width-4.2.3.tgz")
    lp = b2n.lock_entry(PACKAGES, "left-pad", PUBLISHED, registry)
    assert lp["peerDependenciesMeta"] == {"react": {"optional": True}}
    core = b2n.lock_entry(PACKAGES, "@cline/core", PUBLISHED, registry)
    assert core["integrity"] == "sha512-c" and core["peerDependenciesMeta"] == {"posthog-node": {"optional": True}}


def test_runner_lockfile_is_the_derived_one():
    """The committed lockfile: every package has a pinned tarball and hash; the SDK packages are 0.2.0."""
    lock = json.loads((ROOT / "agent" / "runner" / "package-lock.json").read_text(encoding="utf-8"))
    entries = {k: v for k, v in lock["packages"].items() if k}
    assert all(v.get("integrity", "").startswith("sha512-") and v.get("resolved") for v in entries.values())
    for n in ("sdk", "core", "agents", "llms", "shared"):
        assert entries[f"node_modules/@cline/{n}"]["version"] == "0.2.0"
    assert entries["node_modules/@ai-sdk/openai-compatible"]["version"] == "3.0.67"
    assert entries["node_modules/ai"]["version"] == "7.0.136"
