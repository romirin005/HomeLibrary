"""Non-destructive grouping of library records, with candidate review.
No book files or source catalog records are changed.
"""
import hashlib, json, re, unicodedata
from pathlib import Path
from difflib import SequenceMatcher

NOISE={'pdf','djvu','djv','epub','fb2','medovoeleto','medovoleto','ru','www','com','скан','scan','копия','copy','книга','book'}

def norm(v):
    v=unicodedata.normalize('NFKC',str(v or '')).casefold().replace('ё','е')
    v=re.sub(r'(?i)\b(?:https?\S+|www\S+)\b',' ',v)
    v=re.sub(r'\([^)]*(?:medovoeleto|\.ru|\.com)[^)]*\)',' ',v)
    v=re.sub(r'\((?:copy|копия|\d+)\)\s*$',' ',v)
    return ' '.join(re.findall(r'[^\W_]+',v,flags=re.UNICODE))

def words(v):return [w for w in norm(v).split() if w not in NOISE and not (len(w)==1 and w.isalpha())]

def isbn_key(v):
    for s in re.findall(r'[0-9Xx][0-9Xx\- ]{8,22}[0-9Xx]',str(v or '')):
        x=re.sub('[^0-9X]','',s.upper())
        if len(x) in (10,13):return x
    return ''

def candidates(r):
    vals=[r.get('title_ko'),r.get('title_ru')]
    path=r.get('file_path') or r.get('file')
    if path: vals.append(Path(str(path)).stem)
    return [words(v) for v in vals if len(words(v))>=2]

def edition(r):
    vals=' '.join(str(r.get(k) or '') for k in ('title_ko','year','volume','issue','edition'))
    # Explicit year and volume/issue are meaningful, not filename suffixes (2)/(3).
    years=set(re.findall(r'(?<!\d)(?:18|19|20)\d{2}(?!\d)',vals))
    vols=set(re.findall(r'(?i)(?:том|т\.|vol\.?|volume|часть|ч\.|выпуск|issue|№)\s*([0-9]+)',vals))
    return years,vols

def compatible(a,b):
    ay,av=edition(a);by,bv=edition(b)
    if ay and by and ay.isdisjoint(by):return False
    if av and bv and av.isdisjoint(bv):return False
    aa=words(a.get('author'));bb=words(b.get('author'))
    if aa and bb and aa[0]!=bb[0] and not set(aa).intersection(bb):return False
    return True

def similarity(a,b):
    if not compatible(a,b):return 0.0,'different-edition'
    sa=str(a.get('sha256') or '').lower();sb=str(b.get('sha256') or '').lower()
    if re.fullmatch('[0-9a-f]{64}',sa) and sa==sb:return 1.0,'sha256'
    ia=isbn_key(a.get('isbn'));ib=isbn_key(b.get('isbn'))
    if ia and ib and ia==ib:return .99,'isbn'
    if ia and ib and ia!=ib:return 0.0,'different-isbn'
    best=0.0
    for ta in candidates(a):
      for tb in candidates(b):
        if ta==tb:score=.98
        else:
          # Shorter title may omit author's initials, publisher, scan source, or year.
          s1,s2=set(ta),set(tb);overlap=len(s1&s2)/max(1,min(len(s1),len(s2)))
          order=SequenceMatcher(None,' '.join(ta),' '.join(tb)).ratio()
          score=max(order*.92, overlap*.96 if min(len(s1),len(s2))>=3 else 0)
          if min(len(s1),len(s2))>=3 and overlap>=.90 and order>=.55:score=max(score,.95)
        best=max(best,score)
    # Avoid merging different titles with common author alone.
    return best,'title'

def group_catalog(rows, review_path=None):
    rows=[r for r in rows if r.get('id') is not None]
    parent={str(r['id']):str(r['id']) for r in rows}
    def root(x):
        while parent[x]!=x:
            parent[x]=parent[parent[x]];x=parent[x]
        return x
    def union(a,b):
        a,b=root(a),root(b)
        if a!=b:parent[b]=a
    # Index candidates by rare-ish tokens to avoid quadratic full-library comparisons.
    index={};proposals=[];seen=set()
    for i,a in enumerate(rows):
        toks=set(w for c in candidates(a) for w in c if len(w)>3)
        for w in toks:
            for j in index.get(w,[]):
                if j in seen:continue
                seen.add(j)
                b=rows[j];score,reason=similarity(a,b)
                if score>=.965:union(str(a['id']),str(b['id']))
                elif score>=.76:proposals.append({'a':str(a['id']),'b':str(b['id']),'title_a':a.get('title_ko') or a.get('title_ru') or '', 'title_b':b.get('title_ko') or b.get('title_ru') or '', 'score':round(score,3),'reason':reason})
            index.setdefault(w,[]).append(i)
        seen.clear()
    # SHA / ISBN may match even if titles share no tokens.
    keys={}
    for r in rows:
        rid=str(r['id'])
        for k in [('sha',str(r.get('sha256') or '').lower()),('isbn',isbn_key(r.get('isbn')))]:
            if not k[1] or (k[0]=='sha' and not re.fullmatch('[0-9a-f]{64}',k[1])):continue
            if k in keys and compatible(r,keys[k]):union(rid,str(keys[k]['id']))
            else:keys[k]=r
    groups={}
    for r in rows:groups.setdefault(root(str(r['id'])),[]).append(r)
    result=[];mapping={}
    for members in groups.values():
        primary=next((x for x in members if x.get('file_path')),members[0]);ids=[str(x['id']) for x in members]
        for rid in ids:mapping[rid]=str(primary['id'])
        if len(members)>1:
            result.append({'primary_id':str(primary['id']),'ids':ids,'files':[{'id':str(x['id']),'name':Path(str(x.get('file_path') or x.get('file') or x.get('title_ko') or '')).name,'format':x.get('format'),'path':x.get('file_path','')} for x in members]})
    proposals=[p for p in proposals if mapping[p['a']]!=mapping[p['b']]]
    proposals.sort(key=lambda p:-p['score'])
    return {'groups':result,'primary_by_id':mapping,'total_records':len(rows),'total_books':len(groups),'review_candidates':proposals[:200]}
