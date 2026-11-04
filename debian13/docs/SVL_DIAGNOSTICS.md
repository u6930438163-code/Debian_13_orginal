# SVL Kernel → Debian13_orginal: local report handoff

Original source: `u6930438163-code/Aplikacja_SVL/scripts/svl_topological_kernel.py`.

The original standalone Python diagnostic module has been run locally. It is not a bootable Debian OS or evidence of autonomous AI workers.

On a trusted development machine, run the existing SVL script without privileged permissions and pass its JSON stdout to this Debian 13 adapter:

```bash
python3 /path/to/Aplikacja_SVL/scripts/svl_topological_kernel.py > svl-report.json
python3 debian13/system/svl_report_adapter.py --input svl-report.json
```

The importer accepts only `System`, `Compact_Metric_C` and `Actual_SI_Updates_Count`. It discards `Operator`, `Config_Snapshot` and `Performance`, and never loads or executes SVL Python source. It makes no network calls and cannot launch agents, execute commands, write to GitHub or grant root privileges. The +1456% number in the original status output is a hardcoded string, not a measured speedup.

Offline validation:

```bash
python3 -m unittest debian13/system/tests/test_svl_report_adapter.py -v
```

Do not feed this imported status into deployment or authorization decisions without independent live verification.
