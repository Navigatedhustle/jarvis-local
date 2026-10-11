#!/usr/bin/env python3
import json,subprocess,sys
from urllib.parse import urlsplit
from pathlib import Path
root=Path(__file__).resolve().parents[1]
meta=json.loads((root/'.publication.json').read_text())
remote=sys.argv[1] if len(sys.argv)>1 else 'origin'
url=sys.argv[2] if len(sys.argv)>2 else subprocess.check_output(['git','remote','get-url','--push',remote],text=True).strip()
expected='https://github.com/'+meta['owner']+'/'+meta['repository']+'.git'
if url!=expected:
 print('Push blocked: destination differs from the reviewed HTTPS repository.',file=sys.stderr);sys.exit(1)
r=subprocess.run(['gh','repo','view',meta['owner']+'/'+meta['repository'],'--json','isPrivate,nameWithOwner'],capture_output=True,text=True)
if r.returncode:
 print('Push blocked: GitHub authentication or repository visibility could not be verified.',file=sys.stderr);sys.exit(1)
d=json.loads(r.stdout)
if not d['isPrivate'] or d['nameWithOwner'].lower()!=(meta['owner']+'/'+meta['repository']).lower():
 print('Push blocked: repository must match the reviewed owner and remain private.',file=sys.stderr);sys.exit(1)
print('Private GitHub destination verified.')
