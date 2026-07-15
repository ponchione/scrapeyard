#!/usr/bin/env python3
"""Render the application address policy for the host firewall installer."""

from __future__ import annotations

import os
import sys
from pathlib import Path

repository_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.fspath(repository_root / "src"))

from scrapeyard.engine.ip_policy import firewall_policy_entries  # noqa: E402


for family, action, cidr in firewall_policy_entries():
    print(f"{family}\t{action}\t{cidr}")
