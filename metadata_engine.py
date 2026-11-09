"""Offline bibliographic metadata extraction. Optional PyMuPDF and DjVuLibre."""
import re,zipfile,hashlib,subprocess,shutil,xml.etree.ElementTree as ET,unicodedata
from pathlib import Path

ISBN_PATTERN=re.compile(r'(?i)(?:ISBN(?:-1[03])?\s*[:：]?\s*)?((?:97[89][\s-]?)?\d[\d\s-]{8,20}[\dXx])')
def valid_isbn(raw):
 s=re.sub(r'[^0-9Xx]','',raw).upper()
 if len(s)==10:return sum((10-i)*(10 if c=='X' else int(c)) for i,c in enumerate(s))%11==0
 if len(s)==13:return sum(int(c)*(1 if i%2==0 else 3) for i,c in enumerate(s))%10==0
 return False
def find_isbns(text):
 found=[]
 for m in ISBN_PATTERN.finditer(text or ''):
  raw=m.group(1);digits=re.sub(r'[^0-9Xx]','',raw).upper()
  # Some text is concatenated to ISBN; search windows
  for n in (13,10):
   for i in range(max(1,len(digits)-n+1)):
    part=digits[i:i+n]
    if len(part)==n and valid_isbn(part) and part not in found:found.append(part)
 return found[:8]
def strip_xml(s):return re.sub(r'\s+',' ',re.sub('<[^>]+>',' ',s or '')).strip()
def sha256_file(p):
 h=hashlib.sha256()
 with open(p,'rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def epub_meta(p):
 result={};text=''
 with zipfile.ZipFile(p) as z:
  container=ET.fromstring(z.read('META-INF/container.xml'))
  opf=next(x.attrib['full-path'] for x in container.iter() if x.tag.endswith('rootfile'))
  root=ET.fromstring(z.read(opf))
  for tag,key in [('title','title'),('creator','author'),('publisher','publisher'),('description','description'),('date','year'),('language','language'),('identifier','identifiers')]:
   vals=[(e.text or '').strip() for e in root.iter() if e.tag.endswith('}'+tag) or e.tag==tag]
   vals=[v for v in vals if v]
   if vals:result[key]='; '.join(vals[:3])[:1800]
  items=[e for e in root.iter() if e.tag.endswith('item') and e.attrib.get('media-type') in ('application/xhtml+xml','text/html')]
  import posixpath
  base=posixpath.dirname(opf)
  for e in items[:3]:
   path=posixpath.normpath(posixpath.join(base,e.attrib.get('href','')))
   try:text+=' '+strip_xml(z.read(path).decode('utf-8','replace'))[:12000]
   except (KeyError,ValueError):pass
 return result,text
def pdf_meta(p):
 try:import fitz
 except ImportError:return {},'', ['Для чтения метаданных PDF установите pymupdf']
 d=fitz.open(p)
 if d.needs_pass:return {},'', ['PDF защищён паролем']
 md=d.metadata or {};res={}
 for k,out in [('title','title'),('author','author'),('subject','description'),('creationDate','year')]:
  if md.get(k):res[out]=str(md[k]).strip()
 if res.get('year','').startswith('D:'):res['year']=res['year'][2:6]
 text='\n'.join(d[i].get_text() for i in range(min(6,len(d))))[:60000]
 d.close()
 return res,text,[]
def djvu_meta(p):
 exe=shutil.which('djvutxt')
 if not exe:return {},'', ['Для извлечения текста DJVU установите DjVuLibre (djvutxt) и добавьте его в PATH']
 try:
  cp=subprocess.run([exe,str(p)],capture_output=True,timeout=35,check=False)
  if cp.returncode:return {},'', ['DjVuLibre не смог извлечь текст (возможно, это скан без OCR)']
  return {},cp.stdout.decode('utf-8','replace')[:60000],[]
 except (OSError,subprocess.TimeoutExpired) as e:return {},'', [str(e)]
def guess_lines(text,filename):
 lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines()[:120]]
 lines=[x for x in lines if 3<=len(x)<=180]
 out={}
 for line in lines:
  m=re.match(r'(?i)^(?:author|authors|автор(?:ы)?|by|저자|著者)\s*[:：]\s*(.+)',line)
  if m:out.setdefault('author',m.group(1)[:200])
  m=re.match(r'(?i)^(?:title|название|제목)\s*[:：]\s*(.+)',line)
  if m:out.setdefault('title',m.group(1)[:300])
  m=re.match(r'(?i)^(?:publisher|издательство|출판사)\s*[:：]\s*(.+)',line)
  if m:out.setdefault('publisher',m.group(1)[:200])
  m=re.match(r'(?i)^(?:abstract|аннотация|summary|초록)\s*[:：]\s*(.+)',line)
  if m:out.setdefault('description',m.group(1)[:1600])
 if not out.get('title'):out['title']=Path(filename).stem.replace('_',' ')
 return out
def extract(path):
 p=Path(path);warnings=[];text='';meta={}
 try:
  if p.suffix.lower()=='.epub':meta,text=epub_meta(p)
  elif p.suffix.lower()=='.pdf':meta,text,warnings=pdf_meta(p)
  elif p.suffix.lower() in ('.djvu','.djv'):meta,text,warnings=djvu_meta(p)
  else:return {'metadata':{},'warnings':['Автоматический анализ доступен для PDF, EPUB, DJVU']}
 except Exception as e:return {'metadata':{},'warnings':['Ошибка извлечения: '+str(e)[:250]]}
 hints=guess_lines(text,p.name)
 for k,v in hints.items():meta.setdefault(k,v)
 isbn=find_isbns(' '.join([meta.get('identifiers',''),text[:60000]]))
 if isbn:meta['isbn']=isbn[0]
 if not meta.get('description'):
  for m in re.finditer(r'(?i)(?:abstract|аннотация|summary|초록)\s*[:：]?\s*([^\n]{50,900})',text):
   meta['description']=m.group(1).strip();break
 if meta.get('year'):
  y=re.search(r'(?:18|19|20)\d{2}',meta['year'])
  if y:meta['year']=y.group(0)
 return {'metadata':meta,'warnings':warnings,'isbn_candidates':isbn,'text_available':bool(text.strip())}
