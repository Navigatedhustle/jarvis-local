#!/usr/bin/env python3
"""Fail closed on unsafe staged files/history; never display secret values."""
import argparse,os,re,shutil,subprocess,sys,tempfile,json,hashlib,struct
from pathlib import Path

def git(*args):
    result=subprocess.run(['git',*args],capture_output=True)
    if result.returncode:raise RuntimeError('Git check failed')
    return result.stdout

def allowed(name,mode='100644'):
    if name=='docs/images/synthetic-preview.png':return mode in {'100644','100755'}
    parts=Path(name).parts
    if mode not in {'100644','100755'}:return False
    if parts and parts[0] in {'models','data','var','logs','runtime','artifacts','out','run','vendor','dist'}:return False
    if any(p in {'.venv','venv','env','node_modules','__pycache__','site-packages','.cache','.pytest_cache','.ruff_cache'} for p in parts):return False
    if name.endswith('.env.example') or name.endswith('booth.env.example'):return True
    if re.search(r'(?i)(?:^|/)(?:\.env(?:\.|$)|secrets?|credentials?|id_rsa|id_ed25519|cookies|sessions?|auth(?:entication)?\.db)|\.bak$|\.log$|backup',name):return False
    return not name.lower().endswith(('.sqlite','.sqlite3','.db','.pem','.key','.p12','.pfx','.gguf','.safetensors','.onnx','.pt','.pth','.bin','.trt','.zip','.tar','.gz','.wav','.mp4','.webm','.pdf','.docx','.xlsx','.pptx','.ipynb','.png','.jpg','.jpeg'))

def screenshot_verified(content,receipt):
    if receipt.get('sha256')!=hashlib.sha256(content).hexdigest() or receipt.get('classification')!='reviewed synthetic UI':return False
    if not content.startswith(b'\x89PNG\r\n\x1a\n'):return False
    position=8
    while position<len(content):
        if position+12>len(content):return False
        length=struct.unpack('>I',content[position:position+4])[0]
        kind=content[position+4:position+8]
        if kind not in {b'IHDR',b'IDAT',b'IEND'}:return False
        position+=12+length
    return position==len(content)

def scanner_run(scanner,args):
    # Scanner diagnostics stay captured; redacted output may still contain sensitive metadata.
    r=subprocess.run([scanner,*args,'--redact=100','--no-banner'],capture_output=True)
    if r.returncode:
        print('Secret scan failed or found a potential credential. Review locally; no secret values are displayed.',file=sys.stderr)
        return False
    return True

def main():
    p=argparse.ArgumentParser();p.add_argument('--history',action='store_true');a=p.parse_args()
    scanner=os.environ.get('GITLEAKS_BIN') or shutil.which('gitleaks')
    if not scanner or not Path(scanner).is_file():print('Install Gitleaks or set GITLEAKS_BIN; scan cannot be bypassed by a missing tool.',file=sys.stderr);return 2
    try:
        entries=git('ls-files','--stage','-z').split(b'\0');issues=[]
        all_names={row.split(b'\t',1)[1].decode() for row in entries if row}
        with tempfile.TemporaryDirectory(prefix='publication-index-') as d:
            for row in entries:
                if not row:continue
                meta,name=row.split(b'\t',1);mode,blob,stage=meta.decode().split();name=name.decode()
                if stage!='0' or not allowed(name,mode):issues.append(name);continue
                content=git('cat-file','blob',blob)
                if name=='docs/images/synthetic-preview.png':
                    receipt=json.loads(git('show',':docs/SCREENSHOT-REVIEW.json'))
                    if not screenshot_verified(content,receipt):issues.append(name);continue
                if len(content)>2*1024*1024:issues.append(name);continue
                # Binary fonts require a retained adjacent license; other binaries fail closed.
                if b'\0' in content[:8192] and name!='docs/images/synthetic-preview.png':
                    is_font='fonts' in Path(name).parts and name.endswith(('.woff','.woff2','.ttf','.otf'))
                    has_notice=any(Path(x).parent==Path(name).parent and ('LICENSE' in Path(x).name.upper() or 'NOTICE' in Path(x).name.upper()) for x in all_names)
                    if not (is_font and has_notice):issues.append(name);continue
                target=Path(d)/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)
            if issues:
                print('Publication blocked: unsafe artifact paths or unresolved index entries:',file=sys.stderr)
                for name in issues:print('  '+name,file=sys.stderr)
                return 1
            if not scanner_run(scanner,['dir',d]):return 1
        if a.history:
            for commit in git('rev-list','--all').decode().splitlines():
                for row in git('ls-tree','-r','-z',commit).split(b'\0'):
                    if not row:continue
                    meta,name=row.split(b'\t',1);mode=meta.decode().split()[0]
                    if name.decode()=='docs/images/synthetic-preview.png':
                        receipt=json.loads(git('show',commit+':docs/SCREENSHOT-REVIEW.json'))
                        if not screenshot_verified(git('show',commit+':'+name.decode()),receipt):
                            print('Publication blocked: screenshot history differs from its reviewed receipt.',file=sys.stderr);return 1
                    if not allowed(name.decode(),mode):
                        print('Publication blocked: excluded artifact exists in local history.',file=sys.stderr);return 1
            if not scanner_run(scanner,['git','.','--log-opts=--all']):return 1
        print('Publication checks passed: staged blobs'+(' and all local commit history' if a.history else '')+'. Ownership and asset rights still require review.')
        return 0
    except Exception:
        print('Publication check could not complete; no upload is allowed.',file=sys.stderr);return 2
if __name__=='__main__':sys.exit(main())
