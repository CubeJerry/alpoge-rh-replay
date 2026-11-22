#!/usr/bin/env python3
"""Continue the initial fresh-tree check after the M027599 proof-only repair.

The complete original four-target history is retained. This invocation builds
only the failed target, using the exact initial driver and frozen selection.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

base = Path('/workspace/scratch/1fdb83412789')
driver_path = Path(__file__).with_name('initial_driver.py')
spec = importlib.util.spec_from_file_location('p0179_initial_driver', driver_path)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

initial = json.loads((base / 'initial-check/P0179-receipt.json').read_text())
failed = [r['id'] for r in initial['modules'] if not r['successful']]
assert failed == ['M027599'], failed
assert 'TFAE indices start at 1.' in (base / 'initial-check/logs/M027599.log').read_text()

args = SimpleNamespace(
    repo=base / 'replay', work=base / 'port-work', packet='P0179',
    patches=None, out=base / 'repair-check', lean_threads=1,
    issue='https://github.com/rohanarun/alpoge-rh-replay/issues/32377',
)
helpers = driver.helpers(args.repo)
selection = driver.load_selection(args, helpers)
selection['selected'] = [r for r in selection['selected'] if r['id'] == 'M027599']
assert len(selection['selected']) == 1
raise SystemExit(driver.check(args, helpers, selection))
