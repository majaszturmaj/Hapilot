#!/usr/bin/env python3
"""Derive the runner's package-lock.json from the bun.lock of the Cline CLI build (cline repo, tag cli-v3.0.74).

The CLI binary that was measured bundles the @cline/* SDK packages with the dependency versions in that bun.lock.
This takes the closure of the five SDK packages as bun laid it out (a key "a/b" is package b nested under a; a
dependency is looked up from the innermost key outwards), keeps bun's version, tarball and integrity hash for every
package, and takes the @cline/* packages themselves from the npm registry (0.2.0, whose manifests equal the workspace
ones with `workspace:*` replaced by 0.2.0). bun's root overrides that touch the closure go into package.json so npm
accepts the same edges. Optional peers are not installed (as npm does).

usage: bun2npm.py <bun.lock> <dir with cline-<pkg>-0.2.0/package/package.json> <current package-lock.json> <out dir>
Check the result with `npm ci --dry-run --offline` against an empty cache: npm must not need to resolve anything.
"""

import json
import re
import sys
from collections import deque
from pathlib import Path

SDK = ["sdk", "core", "agents", "llms", "shared"]
ROOT_DEPS = {"@cline/core": "0.2.0", "@cline/sdk": "0.2.0"}


def load_bun_lock(path: Path) -> dict:
    """bun.lock is JSON with trailing commas."""
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", path.read_text(encoding="utf-8")))


def split_key(key: str) -> list[str]:
    """'@a/b/c/@d/e' -> ['@a/b', 'c', '@d/e'] (package names along the nesting path)."""
    parts, out, i = key.split("/"), [], 0
    while i < len(parts):
        if parts[i].startswith("@"):
            out.append(parts[i] + "/" + parts[i + 1])
            i += 2
        else:
            out.append(parts[i])
            i += 1
    return out


def resolve(packages: dict, path: list[str], dep: str) -> str | None:
    """Key that a dependency `dep` of the package at `path` resolves to: innermost nesting level first."""
    for i in range(len(path), -1, -1):
        key = "/".join(path[:i] + [dep])
        if key in packages:
            return key
    return None


def edges(packages: dict, key: str, published: dict) -> list[tuple[str, str, str]]:
    """(dependency, spec, kind) with kind dep/optional/peer; optional peers left out."""
    name = split_key(key)[-1]
    if name in published:
        info = published[name]
        optional_peers = {k for k, v in (info.get("peerDependenciesMeta") or {}).items() if v.get("optional")}
    else:
        entry = packages[key]
        info = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}
        optional_peers = set(info.get("optionalPeers") or [])
    out = [(d, s, "dep") for d, s in (info.get("dependencies") or {}).items()]
    out += [(d, s, "optional") for d, s in (info.get("optionalDependencies") or {}).items()]
    out += [(d, s, "peer") for d, s in (info.get("peerDependencies") or {}).items() if d not in optional_peers]
    return out


def closure(packages: dict, published: dict, roots, include_optional: bool) -> tuple[set[str], list[str]]:
    seen, missing, queue = set(), [], deque(roots)
    while queue:
        key = queue.popleft()
        if key in seen:
            continue
        seen.add(key)
        for dep, spec, kind in edges(packages, key, published):
            if kind == "optional" and not include_optional:
                continue
            target = resolve(packages, split_key(key), dep)
            if target is None:
                if kind != "optional":
                    missing.append(f"{key} -> {dep}@{spec} ({kind})")
                continue
            queue.append(target)
    return seen, missing


def lock_entry(packages: dict, key: str, published: dict, registry_lock: dict) -> dict:
    name = split_key(key)[-1]
    loc = "node_modules/" + "/node_modules/".join(split_key(key))
    if name in published:
        pub, old = published[name], registry_lock["packages"][loc]
        e = {"version": pub["version"], "resolved": old["resolved"], "integrity": old["integrity"]}
        for f in ("license", "dependencies", "optionalDependencies", "peerDependencies", "peerDependenciesMeta",
                  "bin", "engines"):
            if pub.get(f):
                e[f] = pub[f]
        return e
    entry = packages[key]
    spec_name, _, version = entry[0].rpartition("@")
    info = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}
    e = {"name": spec_name} if spec_name != name else {}
    e["version"] = version
    e["resolved"] = entry[1] or f"https://registry.npmjs.org/{spec_name}/-/{spec_name.split('/')[-1]}-{version}.tgz"
    if len(entry) > 3 and entry[3]:
        e["integrity"] = entry[3]
    for f in ("dependencies", "optionalDependencies", "peerDependencies", "bin", "os", "cpu"):
        if info.get(f):
            e[f] = info[f]
    if info.get("optionalPeers"):
        e["peerDependenciesMeta"] = {p: {"optional": True} for p in info["optionalPeers"]}
    return e


def main(argv: list[str]) -> int:
    bun_path, pub_dir, registry_lock_path, out_dir = (Path(p) for p in argv[:4])
    bun = load_bun_lock(bun_path)
    packages = bun["packages"]
    registry_lock = json.loads(registry_lock_path.read_text(encoding="utf-8"))
    published = {f"@cline/{n}": json.loads((pub_dir / f"cline-{n}-0.2.0" / "package" / "package.json")
                                           .read_text(encoding="utf-8")) for n in SDK}
    required, _ = closure(packages, published, ROOT_DEPS, include_optional=False)
    everything, missing = closure(packages, published, ROOT_DEPS, include_optional=True)
    if missing:
        print("unresolved edges:", *missing, sep="\n  ", file=sys.stderr)
        return 1
    lock_packages = {"": {"name": "hapilot-agent-runner", "version": "0.1.0", "license": "MIT",
                          "dependencies": ROOT_DEPS, "engines": {"node": ">=22"}}}
    for key in sorted(everything, key=split_key):
        e = lock_entry(packages, key, published, registry_lock)
        if key not in required:
            e["optional"] = True
        lock_packages["node_modules/" + "/node_modules/".join(split_key(key))] = e
    names = {split_key(k)[-1] for k in everything}
    pkg = json.loads((registry_lock_path.parent / "package.json").read_text(encoding="utf-8"))
    pkg["dependencies"] = ROOT_DEPS
    pkg["overrides"] = dict(sorted((k, v) for k, v in (bun.get("overrides") or {}).items() if k in names))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    (out_dir / "package-lock.json").write_text(json.dumps(
        {"name": "hapilot-agent-runner", "version": "0.1.0", "lockfileVersion": 3, "requires": True,
         "packages": lock_packages}, indent=2) + "\n", encoding="utf-8")
    patched = sorted(k for k in (bun.get("patchedDependencies") or {}) if k.rpartition("@")[0] in names)
    print(f"{len(everything)} packages ({len(everything) - len(required)} only optional), "
          f"overrides {sorted(pkg['overrides'])}, bun patches not applied by npm: {patched}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
