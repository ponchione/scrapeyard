"""TD-71 diagnostic (throwaway): print one probe's result and add it to the summary."""

import json
import sys

path, summary, variant, kind, stealth, pyfix, status = sys.argv[1:8]
result = None
try:
    for line in open(path):
        if line.startswith("{"):
            result = json.loads(line)
except OSError:
    pass
if result is None:
    print(f"   NO RESULT (exit status {status})")
    with open(summary, "a") as stream:
        stream.write(f"{variant}\t{kind}\t{stealth}\t{pyfix}\tNO RESULT (exit {status})\n")
    sys.exit(0)

env = result.get("environment", {})
keys = ("crash_event_seconds", "command", "set_offline", "set_offline_seconds", "connected_after",
        "fetch_raised", "fetch_seconds", "aclose_seconds", "events", "probe", "signal_sent")
print("   result:", {key: result[key] for key in keys if result.get(key) is not None})
print("   env:", {key: env.get(key) for key in ("pyfix", "core_pattern", "core_pipe_limit", "rlimit_core",
                                                "coredump_filter", "cwd", "cwd_writable", "uname")})
before = result.get("before", [])
for entry in before:
    if entry["type"] in ("renderer", "zygote"):
        print("   before:", {key: entry.get(key) for key in (
            "pid", "type", "nspid", "uid", "proc_dir_owner", "core_limit", "coredump_filter", "cwd", "capeff",
            "nonewprivs", "seccomp", "seccomp_filters", "sigcgt", "vmsize", "rss", "oom_score_adj")})
browser = next((entry for entry in before if entry.get("cmdline")), None)
if browser:
    flags = [flag for flag in browser["cmdline"].split() if any(
        word in flag for word in ("sandbox", "breakpad", "crash", "zygote", "headless", "chrome"))]
    print("   browser flags of interest:", flags[:14])
print("   handlers:", result.get("handlers"))
for sample in result.get("samples", [])[:2] + result.get("samples", [])[-1:]:
    print("   sample", sample["t"], [
        (entry["pid"], entry["type"], entry["state"], "coredumping=" + str(entry["coredumping"]), entry["threads"])
        for entry in sample["procs"] if entry["type"] == "renderer"])
print("   left over:", [(entry["pid"], entry["type"], entry["state"]) for entry in result.get("left_over", [])])

crash = result.get("crash_event_seconds")
note = result.get("probe") or ""
with open(summary, "a") as stream:
    stream.write("\t".join([
        variant, kind, stealth, pyfix,
        "none" if crash is None else f"{crash:.3f}",
        str(result.get("set_offline", "-"))[:48],
        str(result.get("fetch_seconds", "-")),
        str(note)[:80],
    ]) + "\n")
