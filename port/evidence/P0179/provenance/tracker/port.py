"""Pick and reserve porting work (see PORTING.md).

  python3 tracker/port.py next [--limit 10]   # shallowest packets without a port reservation
  python3 tracker/port.py next --include-partial  # also show packets with some ready modules
  python3 tracker/port.py show P0123          # modules, depth, and port status of one packet
  python3 tracker/port.py show P0123 --ready-only  # unported modules whose imports are ready
  python3 tracker/port.py claim P0123 P0124   # reserve independent packets with one issue scan

A packet is done when every module has port/patches/<ID>.json. Ports go in import-depth
order. By default, `next` offers packets whose project imports are already ported;
`--include-partial` also identifies ready members of otherwise blocked packets.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

import tasks

ROOT = Path(__file__).resolve().parent.parent
PATCHES = ROOT / 'port' / 'patches'
TRIAGE = ROOT / 'port' / 'triage' / 'results.json'
PRUNED = ROOT / 'port' / 'pruned.txt'


def ported():
    return {p.stem for p in PATCHES.glob('M*.json')}


def pruned():
    if not PRUNED.exists():
        return set()
    return {line.strip() for line in PRUNED.read_text().splitlines() if line.strip() and not line.startswith('#')}


def check_unported(target, members):
    """Check the published target and the requested records before opening an issue."""
    owner, repo = tasks.repo.split('/')
    fields = ['pins:object(expression:"main:port/target.json"){... on Blob{text}}']
    for i, row in enumerate(members):
        path = json.dumps(f"main:port/patches/{row['id']}.json")
        fields.append(f'm{i}:object(expression:{path}){{oid}}')
    query = ('query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){'
             + ' '.join(fields) + '}}')
    output = subprocess.check_output(['gh', 'api', 'graphql', '-f', 'query=' + query,
                                      '-f', 'owner=' + owner, '-f', 'repo=' + repo], text=True)
    records = json.loads(output)['data']['repository']
    published = json.loads(records['pins']['text'])
    local = json.loads((ROOT / 'port' / 'target.json').read_text())
    if any(local[key] != published[key] for key in ('lean', 'mathlib')):
        raise SystemExit('The port target changed on main. Update your checkout before claiming.')
    if all(records[f'm{i}'] is not None for i in range(len(members))):
        raise SystemExit(f'{target} is already fully ported on main.')


def triage():
    if not TRIAGE.exists():
        return {}
    return {r['id']: r['status'] for r in json.loads(TRIAGE.read_text())['results']}


def port_issues():
    """Reservations from open claim issues and pending port pull requests."""
    taken = set()
    for issue in tasks.open_issues():
        if 'pull_request' in issue:
            labels = {label if isinstance(label, str) else label['name']
                      for label in issue.get('labels', [])}
            if ('port' in labels or re.search(r'\bport(?:s|ing)?\b', issue['title'], re.I)):
                taken.update(match.upper() for match in
                             re.findall(r'\b(?:P\d{4}|M\d{6})\b', issue['title'], re.I))
            continue
        match = re.fullmatch(r'Port (P\d{4}|M\d{6})', issue['title'])
        if match:
            taken.add(match[1])
    return taken


def packets(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row['packet'], []).append(row)
    return groups


def reservation_conflict(target, groups, by_id, taken):
    """Return the open reservation overlapping target, if any."""
    if target in taken:
        return target
    row = by_id.get(target)
    if row is not None:
        packet = row['packet']
        return packet if packet in taken else None
    return next((row['id'] for row in groups[target] if row['id'] in taken), None)


def ready(packet_rows, done):
    """Every project import outside the packet must already be ported."""
    inside = {r['id'] for r in packet_rows}
    for row in packet_rows:
        for dep in row['imports']:
            if dep not in inside and dep not in done:
                return False
    return True


def ready_members(packet_rows, done):
    """Unported members buildable from landed imports and other ready packet members."""
    known = set(done)
    remaining = {r['id']: r for r in packet_rows if r['id'] not in done}
    selected = set()
    # At least one member leaves remaining on each successful pass.
    while remaining:
        available = {mid for mid, row in remaining.items()
                     if all(dep in known for dep in row['imports'])}
        if not available:
            break
        selected.update(available)
        known.update(available)
        for mid in available:
            del remaining[mid]
    return [r for r in packet_rows if r['id'] in selected]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    nxt = sub.add_parser('next'); nxt.add_argument('--limit', type=int, default=10)
    nxt.add_argument('--include-partial', action='store_true',
                     help='also list packets with some unported modules ready to build')
    show = sub.add_parser('show'); show.add_argument('packet')
    show.add_argument('--ready-only', action='store_true',
                      help='show only unported modules whose imports can be satisfied')
    claim = sub.add_parser('claim'); claim.add_argument('packet', nargs='+', metavar='TASK')
    args = parser.parse_args()

    rows = list(tasks.module_rows())
    by_id = {r['id']: r for r in rows}
    done, skipped, status = ported(), pruned(), triage()
    resolved = done | skipped
    groups = packets(rows)

    if args.command == 'show':
        selected = groups.get(args.packet)
        if selected is None:
            selected = [by_id[args.packet]] if args.packet in by_id else []
        if args.ready_only:
            selected = ready_members([r for r in selected if r['id'] not in skipped], done)
        for r in sorted(selected, key=lambda r: r['id']):
            if r['id'] in done:
                state = 'ported'
            elif r['id'] in skipped:
                state = 'pruned'
            else:
                state = status.get(r['id'], 'untriaged')
            print(r['id'], r['depth'], state, r['module'])
        return
    if args.command == 'claim':
        requested = args.packet
        if any(not re.fullmatch(r'P\d{4}|M\d{6}', target)
               or (target not in groups and target not in by_id) for target in requested):
            raise SystemExit('Unknown packet or module ID')
        checked = set()
        for target in requested:
            conflict = reservation_conflict(target, groups, by_id, checked)
            if conflict is not None:
                raise SystemExit(f'Requested tasks overlap: {target} and {conflict}')
            checked.add(target)
        for target in requested:
            members = groups[target] if target in groups else [by_id[target]]
            live_members = [r for r in members if r['id'] not in skipped]
            if not live_members:
                raise SystemExit(f'{target} is pruned on upstream main and does not need porting.')
            check_unported(target, live_members)
        taken = port_issues()
        for target in requested:
            conflict = reservation_conflict(target, groups, by_id, taken)
            if conflict is not None:
                raise SystemExit(f'Already reserved by an open port issue or pull request: Port {conflict}')
        # Validate every requested reservation before creating the first issue.
        for target in requested:
            subprocess.run(['gh', 'issue', 'create', '--repo', tasks.repo, '--title', f'Port {target}',
                            '--label', 'port', '--body', 'Porting this packet to the target in port/target.json. See PORTING.md.'],
                           check=True)
        return
    taken = port_issues()
    choices = []
    for packet, members in groups.items():
        if (reservation_conflict(packet, groups, by_id, taken) is not None
                or all(r['id'] in resolved for r in members)):
            continue
        live_members = [r for r in members if r['id'] not in skipped]
        partial = not ready(live_members, done)
        if partial and not args.include_partial:
            continue
        available = ready_members(live_members, done) if partial else [r for r in live_members if r['id'] not in done]
        if not available:
            continue
        depth = max(r['depth'] for r in live_members)
        failing = sum(status.get(r['id']) == 'fail' for r in (available if partial else live_members))
        remaining = sum(r['id'] not in done for r in live_members)
        choices.append((partial, depth, -len(available) if partial else 0,
                        -failing, packet, len(live_members), len(available), remaining))
    for partial, depth, _, negative_failing, packet, size, count, remaining in sorted(choices)[:args.limit]:
        if partial:
            print(f'{packet}\tdepth {depth}\t{count}/{remaining} unported modules ready '
                  f'({size} total)\t{remaining - count} waiting for imports')
        else:
            print(f'{packet}\tdepth {depth}\t{size} modules\t{-negative_failing} known failing on the target')


if __name__ == '__main__':
    main()
