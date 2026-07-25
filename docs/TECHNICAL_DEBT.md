# Technical Debt

## SQLite runtime packaging

Status: open. Owner: Scrapeyard maintainers.

The Ubuntu 24.04 snapshot currently provides SQLite 3.45.1, which lacks the
3.51 broken-POSIX-lock defenses and the WAL-reset fix delivered in 3.51.3.
Scrapeyard therefore installs the official SHA3-pinned SQLite 3.51.3 shared
library under `/usr/local/lib`; Python's `_sqlite3` extension dynamically loads
that copy. The older distribution package remains installed because Ubuntu's
Python package depends on it, even though it is not active in the Scrapeyard
process.

This creates two maintenance costs: package scanners inventory an inactive old
library, and maintainers must keep the source archive/hash/build assertion
current. When the pinned Ubuntu snapshot supplies SQLite 3.51.3 or newer,
remove the source build and image override, verify `_sqlite3` resolves the
vendor library, and rerun the cached-WAL regression, full tests, container
security scan, readiness failure injection, and backup/restore qualification.

## Deleted-sidecar diagnostic portability

Status: accepted for the supported Linux container topology.

Readiness detects a cached descriptor ending in deleted `-wal` or `-shm` by
reading `/proc/self/fd`. The core fix does not depend on procfs: raw opens of
live database paths are prohibited and all readiness work uses SQLite's VFS.
However, a future non-Linux deployment would lose this defense-in-depth signal
and must implement an equivalent platform-native check before that topology is
supported.
