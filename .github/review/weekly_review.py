from __future__ import annotations
import datetime as dt, json, os, re, subprocess, sys
from pathlib import Path

ROOT=Path.cwd(); BACKLOG=ROOT/'BACKLOG.md'; STATE=ROOT/'.github/review/state.json'
PREFIX='review/weekly-'; MODEL=os.getenv('REVIEW_MODEL','github-copilot-cli')
REPO=os.getenv('GITHUB_REPOSITORY',''); TODAY=dt.datetime.now(dt.timezone.utc).date().isoformat(); NOW=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
KEEP={'.py','.pyi','.sql','.toml','.yaml','.yml','.json','.md','.sh','.ps1','.ini','.cfg'}; SKIP={'.git','.venv','venv','node_modules','dist','build','artifacts','data','models','__pycache__'}

def cmd(*a,ok=True):
 p=subprocess.run(a,cwd=ROOT,text=True,capture_output=True)
 if ok and p.returncode: raise RuntimeError(f"{' '.join(a)} failed: {p.stderr or p.stdout}")
 return (p.stdout or '').strip()
def gh(*a): return cmd('gh',*a)

def blocked():
 prs=json.loads(gh('pr','list','--state','open','--limit','100','--json','number,headRefName,url') or '[]')
 hit=[p for p in prs if p.get('headRefName','').startswith(PREFIX)]
 if hit: return True,'open review PR: '+', '.join('#'+str(p['number']) for p in hit)
 b=cmd('git','ls-remote','--heads','origin',f'refs/heads/{PREFIX}*')
 return (True,'review branch still exists') if b else (False,'')

def state():
 try: return json.loads(STATE.read_text())
 except Exception: return {}
def exists(sha): return bool(sha) and subprocess.run(['git','cat-file','-e',sha+'^{commit}'],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
def baseline(st):
 s=st.get('last_reviewed_target_sha','')
 if exists(s): return s
 t=cmd('git','rev-parse','-q','--verify','refs/tags/weekly-review-baseline',ok=False)
 return t if exists(t) else None
def interesting(p):
 x=Path(p)
 return not any(q in SKIP for q in x.parts) and x.name not in {'BACKLOG.md','weekly_review.py','weekly-review.yml'} and (x.suffix.lower() in KEEP or x.name.lower().startswith('dockerfile'))
def cut(s,n): return s if len(s)<=n else s[:n//2]+'\n...[truncated]...\n'+s[-n//2:]

def context(base,target):
 files=cmd('git','diff','--name-only',f'{base}..{target}').splitlines() if base else cmd('git','ls-files').splitlines()
 files=[p for p in files if interesting(p)]
 files.sort(key=lambda p:(0 if any(x in Path(p).parts for x in ('src','tests','sql','scripts')) else 1,p))
 out=[]; used=0
 for p in files:
  if used>=180000: break
  if base: text=cmd('git','diff','--unified=40',f'{base}..{target}','--',p,ok=False)
  else:
   try: text=(ROOT/p).read_text(encoding='utf-8',errors='replace')
   except Exception: continue
  if not text: continue
  part=f'\n===== {p} =====\n'+cut(text,24000)+'\n'; part=part[:180000-used]; out.append(part); used+=len(part)
 return ''.join(out),files

def backlog_summary():
 if not BACKLOG.exists(): return '(no BACKLOG.md yet)'
 s=BACKLOG.read_text(encoding='utf-8',errors='replace')
 if len(s)<=35000: return s
 lines=s.splitlines(); out=[]; n=0
 for line in lines:
  if re.match(r'^#{1,4}\s+.*(?:PR[- #]?\d+|Merged|Completed|Backlog|Active)',line,re.I): out.append(line); n=6
  elif n: out.append(line); n-=1
 return cut('\n'.join(out),35000)
def merged_since(ts):
 prs=json.loads(gh('pr','list','--state','merged','--limit','100','--json','number,title,mergedAt,body,url') or '[]')
 return [p for p in prs if not ts or (p.get('mergedAt') or '')>ts][:100]

def ask(ctx,bks,merged,base,target):
 prompt=f'''Repository: {REPO}\nDate: {TODAY}\nBaseline: {base or 'FIRST FULL REVIEW'}\nTarget: {target}\n\nEXISTING BACKLOG (deduplicate against this):\n{bks}\n\nMERGED PRS SINCE LAST REVIEW:\n{json.dumps(merged,ensure_ascii=False)[:35000]}\n\nCODE/DIFF CONTEXT:\n{ctx or '(no selected code changes)'}\n\nReturn JSON only with shape {{"findings":[{{"severity":"HIGH|MEDIUM","type":"Correctness|Data leakage|Reproducibility|Statistical validity|Failure handling|Security|Concurrency|Testing|Architecture|Performance|Maintainability|Refactoring","title":"atomic PR title","problem":"specific evidenced problem","evidence":["path: evidence"],"proposed_change":"bounded implementation","scope":["file/component change"],"acceptance_criteria":["testable criterion","testable criterion","testable criterion","testable criterion"],"fingerprint":"domain|component|problem"}}],"merged_summaries":[{{"number":123,"title":"title","summary":"one sentence"}}]}}. Only HIGH/MEDIUM. No style-only items. No speculative patterns. For quant code explicitly check look-ahead/data leakage, train/test contamination, walk-forward boundaries, reproducibility, unsafe optimization/evaluation coupling and numerical errors. Every finding must be one complete atomic PR. Omit findings without concrete evidence and deduplicate against BACKLOG.'''
 p=subprocess.run(['copilot','-p',prompt,'--no-ask-user'],cwd=ROOT,text=True,capture_output=True)
 if p.returncode:
  raise RuntimeError('Copilot CLI failed: '+(p.stderr or p.stdout)[-4000:])
 raw=p.stdout.strip(); raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw,flags=re.I|re.S)
 return json.loads(raw)

def valid(items,old):
 out=[]; seen=set(); low=old.lower()
 for x in items if isinstance(items,list) else []:
  sev=str(x.get('severity','')).upper(); fp=str(x.get('fingerprint','')).strip().lower(); ac=x.get('acceptance_criteria',[])
  if sev not in {'HIGH','MEDIUM'} or not fp or fp in low or fp in seen or len(ac)<4: continue
  if not all(x.get(k) for k in ('title','problem','evidence','proposed_change','scope')): continue
  x['severity']=sev; x['fingerprint']=fp; out.append(x); seen.add(fp)
 return out
def next_id(s):
 n=[int(x) for x in re.findall(r'\bPR[- #]?(\d+)\b',s,re.I)]; return max(n,default=0)+1
def item_md(i,x):
 ev='\n'.join('- '+str(v) for v in x['evidence']); sc='\n'.join('- '+str(v) for v in x['scope']); ac='\n'.join('- [ ] '+str(v) for v in x['acceptance_criteria'])
 return f'''\n## PR-{i} -- {x['title']}\n\n**Severity:** {x['severity']}  \n**Type:** {x.get('type','Maintainability')}  \n**Status:** BACKLOG  \n**Fingerprint:** `{x['fingerprint']}`  \n**Detected:** {TODAY}\n\n### Problem\n\n{x['problem']}\n\n### Evidence\n\n{ev}\n\n### Proposed change\n\n{x['proposed_change']}\n\n### Scope\n\n{sc}\n\n### Acceptance criteria\n\n{ac}\n'''
def update(findings,sums):
 old=BACKLOG.read_text(encoding='utf-8',errors='replace') if BACKLOG.exists() else '# Backlog\n'; cur=old; i=next_id(cur); ids=[]; add=''
 for x in findings: ids.append(i); add+=item_md(i,x); i+=1
 m=re.search(r'(?mi)^#\s+(?:Completed|Merged)(?:\s*/\s*Merged)?\b',cur)
 if add: cur=(cur[:m.start()].rstrip()+'\n\n'+add.strip()+'\n\n'+cur[m.start():].lstrip()) if m else cur.rstrip()+'\n\n'+add.strip()+'\n'
 rows=[]
 for x in sums if isinstance(sums,list) else []:
  try: num=int(x.get('number'))
  except Exception: continue
  if re.search(rf'Weekly review merged summary[\s\S]*PR #{num}\b',cur,re.I): continue
  if x.get('title') and x.get('summary'): rows.append(f"- PR #{num} -- **{x['title']}**: {x['summary']}")
 if rows:
  if not re.search(r'(?mi)^#\s+(?:Completed|Merged)(?:\s*/\s*Merged)?\b',cur): cur=cur.rstrip()+'\n\n# Completed / Merged\n'
  cur=cur.rstrip()+f'\n\n## Weekly review merged summary -- {TODAY}\n\n'+'\n'.join(rows)+'\n'
 return old,cur,ids

def git_config(): cmd('git','config','user.name','github-actions[bot]'); cmd('git','config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
def tag(target): git_config(); cmd('git','tag','-f','weekly-review-baseline',target); cmd('git','push','--force','origin','refs/tags/weekly-review-baseline')
def pr(target,base,new,ids,nfiles):
 branch=PREFIX+TODAY; git_config(); cmd('git','checkout','-b',branch,target); BACKLOG.write_text(new,encoding='utf-8'); STATE.parent.mkdir(parents=True,exist_ok=True); STATE.write_text(json.dumps({'last_reviewed_target_sha':target,'last_reviewed_at':NOW,'last_review_model':MODEL},indent=2)+'\n'); cmd('git','add','BACKLOG.md','.github/review/state.json'); cmd('git','commit','-m',f'docs(review): weekly repository review {TODAY}'); cmd('git','push','-u','origin',branch)
 body=f"Automated weekly repository review.\n\n- Review baseline: `{base or 'FIRST FULL REVIEW'}`\n- Review target: `{target}`\n- Model: `{MODEL}`\n- New backlog items: {', '.join('PR-'+str(i) for i in ids) or 'none'}\n- Files considered: {nfiles}\n\nImplementation work remains in separate atomic PRs."
 print(gh('pr','create','--base','main','--head',branch,'--title',f'Weekly repository review -- {TODAY}','--body',body))

def main():
 if not REPO: raise RuntimeError('missing GitHub runtime context')
 b,why=blocked()
 if b: print('SKIP:',why); return
 target=cmd('git','rev-parse','HEAD'); st=state(); base=baseline(st); ctx,files=context(base,target); merged=merged_since(st.get('last_reviewed_at'))
 if base and not ctx.strip() and not merged: tag(target); return
 old=BACKLOG.read_text(encoding='utf-8',errors='replace') if BACKLOG.exists() else ''
 res=ask(ctx,backlog_summary(),merged,base,target); findings=valid(res.get('findings'),old); before,after,ids=update(findings,res.get('merged_summaries'))
 if after==before: tag(target); return
 pr(target,base,after,ids,len(files))
if __name__=='__main__':
 try: main()
 except Exception as e: print('ERROR:',e,file=sys.stderr); raise
