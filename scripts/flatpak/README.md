# Shared Flatpak source inventory generator

Run from the app checkout:

```sh
python3 /path/to/craft-common-actions/scripts/flatpak/generate-cargo-sources.py Cargo.lock -o packaging/linux/flatpak/cargo-sources.json
```

An absolute Cargo.lock path also works: the child generator runs from its parent directory. The default output is relative to that workspace, not this shared repository. Explicit output paths are relative to the invoking directory.

## Regeneration wrapper

`generate-cargo-sources.py` uses the upstream
[`flatpak-cargo-generator.py`](https://github.com/flatpak/flatpak-builder-tools/blob/9d86d336a39b9af38b86f114b6ce5820fe260f91/cargo/flatpak-cargo-generator.py)
pinned to commit `9d86d336a39b9af38b86f114b6ce5820fe260f91` with SHA-256
`0a2db6be87d75910facef28ab46d4d6460802e8419ab850d0caa6a364d26b380`. It downloads
the script, verifies that hash, applies the two reviewed patches below, and
invokes it with the current Python interpreter. The upstream script is not
vendored; it is MIT-licensed
([upstream licence](https://github.com/flatpak/flatpak-builder-tools/blob/9d86d336a39b9af38b86f114b6ce5820fe260f91/LICENSE)).
The patched script's hash is also checked before execution.

Requirements: Python 3.9+ with `aiohttp` and `tomlkit` (`PyYAML` is optional and
only needed for upstream's YAML output), plus `git` and network access to fetch
the generator and git dependencies. Crate archive URLs and checksums are read
from Cargo.lock; Flatpak downloads those archives when materializing the sources. The generator clones git
repositories into `$XDG_CACHE_HOME/craft-flatpak-cargo/<workspace-hash>` by default, or the
directory passed with `--cache-dir`; the wrapper refuses a cache inside the
lockfile workspace or `CARGO_TARGET_DIR`. `cargo-sources.json` is written to a temporary
file and atomically renamed only after the generator succeeds, so a failed run
leaves the previous inventory untouched.

The regenerated inventory keeps the temporary file’s owner-only permissions (0600 on POSIX).
Run the generator and Flatpak builder as the same user; a cross-user packaging workflow
should explicitly install the inventory with its required ownership and permissions.

Generation is capped at 30 minutes and the generator's stdin is `/dev/null`, so
an interactive credential prompt fails instead of hanging. On POSIX systems
(Linux is the supported Flatpak packaging platform) the generator starts in its
own session; at the cap, the wrapper kills that generator's process group,
including git clone/fetch descendants, and reaps its leader, so no orphan keeps
writing into the cache. On Windows only the direct generator process is
terminated; its git descendants are not tree-killed, and the timeout is
otherwise the same. The timeout path runs before the atomic rename, so the
existing inventory and the temporary-file cleanup behave exactly like any other
generation failure.

Because the generator no longer shares the wrapper's terminal session, the
wrapper also handles Ctrl-C (`SIGINT`) and `SIGTERM` while it runs: the signal
is recorded, the generator's process group is killed and reaped like the timeout
path, the temporary file and script directory are removed, and only then is the
signal replayed with the handler that was installed before generation started
(`KeyboardInterrupt` for `SIGINT`, the previous or default action for
`SIGTERM`). A cancelled `flatpak-builder` run or an interactive Ctrl-C therefore
leaves neither a detached generator or git clone nor a modified inventory. The
temporary handlers are installed before the generator is spawned, so a signal
that arrives while the child is being created cannot strand the new session
either. A cancellation delivered while those handlers are installed is recorded
once (the first signal wins) and replayed after cleanup with the caller's
restored handler. Signals that cannot be handled by this process at all
(`SIGKILL`, an OOM kill, or a wrapper crash) cannot be cleaned up: the generator
runs in its own session, so it may survive the wrapper, and any temporary output
or script directory it had created remains. A hard termination between creating
the temporary directory or output file and installing the handlers, or after
`run_generator` returns while the output is being validated and renamed, can
likewise leave those temporary paths behind (the complete temporary output in
the latter case), because the default signal action performs no cleanup.

## Why two patches

1. **Keep pinned revisions distinct.** Upstream keys `[source]` replacement
   entries by the canonical repository URL and then adds `rev`/`tag`/`branch`.
   Two different revisions of the same repository therefore overwrite each
   other. The patch keys the entry by the full source URL without its `#`
   fragment, so `https://github.com/storytold/craft-libs?rev=...` remains a
   separate source for each craft-libs revision.
2. **Include `[patch.unused]`.** `Cargo.lock` records patched packages that are
   not in the resolved graph under `patch.unused`. Upstream iterates only
   `package`, which omits crates such as `egui_demo_lib`; `--locked` offline
   dependency resolution then fails because Cargo still loads those patched manifests. The
   patch also walks `patch.unused` entries.

No third patch is needed: upstream's `git_repo_name()` already appends the
first seven characters of the commit, so the two craft-libs checkouts get
different destinations.


Run shared regression tests with `python3 -m unittest discover -s tests -p test_flatpak_sources.py -v`. The tests cover checksum rejection, atomic replacement, timeout, cancellation and descendant cleanup. Set `CRAFT_FLATPAK_TEST_BREAK_KILL=1` for the deliberate broken-cleanup check. Full Flatpak SDK builds remain app-specific.

The lockfile’s parent directory defines the source workspace and cache-exclusion boundary; use its root Cargo.lock. The default packaging path follows the Craft suite convention; use `-o` for another layout. Defaults use a workspace-specific cache, leaving previous LightCraft caches untouched. Do not run two generators concurrently against the same explicit cache. Process-group tests run on Linux and are skipped elsewhere.
