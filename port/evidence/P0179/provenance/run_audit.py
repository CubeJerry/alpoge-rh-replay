#!/usr/bin/env python3
"""Run one unchanged repository semantic audit over the 12-module closure."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('name', choices=['islattice', 'reused'])
name = parser.parse_args().name
base = Path('/workspace/scratch/1fdb83412789')
files = Path(__file__).resolve().parent.parent
driver_path = files / 'provenance/initial_driver.py'
spec = importlib.util.spec_from_file_location('p0179_initial_driver', driver_path)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

initial = json.loads((base / 'initial-check/P0179-receipt.json').read_text())
repair = json.loads((base / 'repair-check/P0179-receipt.json').read_text())
assert [r['id'] for r in initial['modules'] if r['successful']] == ['M022910', 'M023055', 'M026102']
assert repair['all_successful'] and [r['id'] for r in repair['modules']] == ['M027599']

args = SimpleNamespace(repo=base / 'replay', work=base / 'port-work', packet='P0179', lean_threads=1)
helpers = driver.helpers(args.repo)
selection = driver.load_selection(args, helpers)
closure = sorted(selection['selected'] + selection['imports'], key=lambda row: row['id'])
modules = (files / 'closure-modules.txt').read_text().splitlines()
assert len(modules) == 12 and modules == [row['module'] for row in closure]
log = files / f'{name}-audit.log'
assert not log.exists(), f'Refusing to overwrite an audit transcript: {log}'
command = ['elan', 'run', selection['pins']['lean'], 'lake', '--no-ansi',
           'env', 'lean', '--run', str(files / f'{name}_audit.lean'), *modules]
result = driver.run_log(command, args.work / 'lean', driver.environment(args, helpers), log)
print(json.dumps(result, indent=2), flush=True)
print(log.read_text(), flush=True)
raise SystemExit(result['exit_code'])
