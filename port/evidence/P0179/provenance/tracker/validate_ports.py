"""Check port records against the pinned source ledger and port target."""
import json
import re
import sys

from port import PATCHES, ROOT
from tasks import module_rows

FORBIDDEN_RE = re.compile(r'\b(sorry|admit|axiom|native_decide|unsafe|implemented_by)\b')
BLOCK_COMMENT_RE = re.compile(r'/-.*?-/', re.DOTALL)


def expected_source_relpath(module_name):
    return 'src/' + module_name.replace('.', '/') + '.lean'


def strip_lean_comments(line):
    """Strip inline block comments and line comments from a single Lean line."""
    line = BLOCK_COMMENT_RE.sub(' ', line)
    return line.split('--', 1)[0]


def validate_patch_content(patch, expected_relpath):
    """Verify a unified diff only touches expected_relpath and adds no forbidden tokens."""
    try:
        text = patch.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        yield f'{patch.name}: {exc}'
        return

    lines = text.splitlines()
    old_files = [l[4:].split('\t', 1)[0] for l in lines if l.startswith('--- ')]
    new_files = [l[4:].split('\t', 1)[0] for l in lines if l.startswith('+++ ')]
    has_hunk = any(l.startswith('@@ ') for l in lines)

    expected_old = f'a/{expected_relpath}'
    expected_new = f'b/{expected_relpath}'
    if old_files != [expected_old] or new_files != [expected_new] or not has_hunk:
        yield (
            f'{patch.name}: must be a unified diff modifying only '
            f'{expected_old} -> {expected_new}'
        )

    for line in lines:
        if line.startswith('diff --git '):
            parts = line.split()
            if len(parts) != 4 or parts[2] != expected_old or parts[3] != expected_new:
                yield f'{patch.name}: unexpected diff header {line!r}'
        # GNU patch interprets a zero Git object ID as file creation/deletion,
        # even when both unified headers name the existing pinned source.
        index = re.fullmatch(r'index ([0-9a-fA-F]+)\.\.([0-9a-fA-F]+)(?: [0-7]{6})?', line)
        if ((index and any(int(oid, 16) == 0 for oid in index.groups()))
                or line.startswith(('new file mode ', 'deleted file mode '))):
            yield f'{patch.name}: creation/deletion metadata cannot modify an existing pinned source'

    in_block_comment = False
    for lineno, line in enumerate(lines, 1):
        if not line.startswith('+') or line.startswith('+++'):
            continue
        code = line[1:]
        if in_block_comment:
            if '-/' in code:
                code = code.split('-/', 1)[1]
                in_block_comment = False
            else:
                continue
        code = strip_lean_comments(code)
        if '/-' in code:
            before, after = code.split('/-', 1)
            code = before
            if '-/' not in after:
                in_block_comment = True
        match = FORBIDDEN_RE.search(code)
        if match:
            yield (
                f'{patch.name}:{lineno}: added line introduces forbidden construct '
                f'{match.group(1)!r}'
            )


def validate(folder, by_id, target):
    for patch in sorted(folder.glob('*.patch')):
        if not patch.with_suffix('.json').is_file():
            yield f'{patch.name}: missing module record'

    present_ids = {path.stem for path in folder.glob('*.json')}

    for path in sorted(folder.glob('*.json')):
        row = by_id.get(path.stem)
        if row is None:
            yield f'{path.name}: unknown module ID'
            continue
        try:
            record = json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            yield f'{path.name}: {exc}'
            continue
        if not isinstance(record, dict):
            yield f'{path.name}: expected a JSON object'
            continue

        for key in ('module', 'source_sha256', 'lean', 'mathlib'):
            expected = row[key] if key in ('module', 'source_sha256') else target[key]
            if record.get(key) != expected:
                yield f'{path.name}: {key} must match {expected!r}'

        missing_deps = [dep for dep in row.get('imports', ()) if dep not in present_ids]
        if missing_deps:
            yield f'{path.name}: unported project import(s): {", ".join(missing_deps)}'

        notes = record.get('notes')
        patch = path.with_suffix('.patch')
        if not isinstance(notes, str) or not notes.strip():
            yield f'{path.name}: missing notes'
        elif notes == 'unchanged' and patch.exists():
            yield f'{path.name}: unchanged record has a patch'
        elif notes != 'unchanged' and (not patch.is_file() or not patch.stat().st_size):
            yield f'{path.name}: missing or empty patch'
        elif notes != 'unchanged':
            yield from validate_patch_content(patch, expected_source_relpath(row['module']))


def main():
    target = json.loads((ROOT / 'port' / 'target.json').read_text())
    by_id = {row['id']: row for row in module_rows()}
    errors = list(validate(PATCHES, by_id, target))
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(f'Validated {len(list(PATCHES.glob("*.json")))} port records.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
