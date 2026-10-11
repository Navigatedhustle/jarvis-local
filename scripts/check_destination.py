#!/usr/bin/env python3
"""Require the reviewed HTTPS destination and explicitly approved visibility."""
import json, subprocess, sys
from pathlib import Path

def block(message):
    print('Push blocked: '+message, file=sys.stderr)
    sys.exit(1)

root = Path(__file__).resolve().parents[1]
meta = json.loads((root/'.publication.json').read_text())
visibility = meta.get('visibility')
if visibility not in {'PRIVATE', 'PUBLIC'}:
    block('publication visibility must be PRIVATE or PUBLIC.')
if visibility == 'PUBLIC' and meta.get('public_approval') is not True:
    block('public publication requires recorded explicit owner approval.')
remote = sys.argv[1] if len(sys.argv) > 1 else 'origin'
url = sys.argv[2] if len(sys.argv) > 2 else subprocess.check_output(
    ['git', 'remote', 'get-url', '--push', remote], text=True).strip()
expected_owner = meta['owner']+'/'+meta['repository']
if url != 'https://github.com/'+expected_owner+'.git':
    block('destination differs from the reviewed HTTPS repository.')
r = subprocess.run(['gh', 'repo', 'view', expected_owner, '--json',
                    'isPrivate,nameWithOwner'], capture_output=True, text=True)
if r.returncode:
    block('GitHub authentication or repository visibility could not be verified.')
try:
    data = json.loads(r.stdout)
except (ValueError, TypeError):
    block('GitHub returned an invalid verification response.')
if data.get('nameWithOwner', '').lower() != expected_owner.lower():
    block('repository owner/name differs from the reviewed destination.')
if data.get('isPrivate') is not (visibility == 'PRIVATE'):
    block('repository visibility differs from the recorded approval.')
print('Reviewed GitHub destination and '+visibility.lower()+' visibility verified.')
