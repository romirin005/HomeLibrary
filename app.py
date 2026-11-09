import os,sys,re,json,csv,io,time,hashlib,threading,urllib.request,urllib.parse,urllib.robotparser,zipfile,html,fnmatch,unicodedata,difflib,mimetypes,subprocess,shutil,tempfile,xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from datetime import datetime
from xml.sax.saxutils import escape
from pathlib import Path
# AppImage is mounted read-only. Store mutable files under XDG_DATA_HOME.
BUNDLE_ROOT=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
RESOURCES=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))
def user_data_root():
 explicit=os.environ.get('HOME_LIBRARY_DATA_DIR','').strip()
 if explicit:return Path(explicit).expanduser().resolve()
 if sys.platform.startswith('linux') and os.environ.get('APPIMAGE'):
  return Path(os.environ.get('XDG_DATA_HOME') or (Path.home()/'.local'/'share'))/'HomeLibrary'
 return BUNDLE_ROOT
ROOT=user_data_root()
ROOT.mkdir(parents=True,exist_ok=True)
DATA=ROOT/'data'; DOWNLOADS=ROOT/'downloads'; DATA.mkdir(parents=True,exist_ok=True); DOWNLOADS.mkdir(parents=True,exist_ok=True)
DB=DATA/'catalog.json'; LOG=DATA/'events.jsonl'
REVIEWS=DATA/'reader_notes.json'
SCAN_HISTORY=DATA/'folder_scan_history.json'
BACKUP_VERSION=1
def scan_history():
 try:return json.loads(SCAN_HISTORY.read_text(encoding='utf8'))
 except:return {}
def write_scan_history(v):SCAN_HISTORY.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf8')
def make_backup():
 buffer=io.BytesIO()
 with LOCK:
  with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED,compresslevel=5) as z:
   manifest={'format':'home-library-backup','version':BACKUP_VERSION,'created':datetime.now().isoformat(),'includes':['data','covers','previews'],'excludes':['downloads','attachments','external_book_files']}
   z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
   for folder in ('data','covers','previews'):
    root=ROOT/folder
    if not root.exists():continue
    for f in root.rglob('*'):
     if f.is_file() and not f.is_symlink():
      rel=f.relative_to(ROOT).as_posix()
      if f.stat().st_size>30*1024*1024:continue
      z.write(f,rel)
 return buffer.getvalue()
def restore_backup(raw):
 """Restore v1.x/v2.x backups transactionally, keeping pre-restore rollback copy."""
 if isinstance(raw, (str, Path)):
  if Path(raw).stat().st_size>4*1024**3:raise ValueError('Архив больше 4 ГиБ')
  source=raw
 else:
  if len(raw)>4*1024**3:raise ValueError('Архив больше 4 ГиБ')
  source=io.BytesIO(raw)
 with zipfile.ZipFile(source) as z:
  entries=z.infolist()
  if len(entries)>30000:raise ValueError('Слишком много файлов')
  if 'manifest.json' in z.namelist():
   manifest=json.loads(z.read('manifest.json'))
   if manifest.get('format')!='home-library-backup' or not isinstance(manifest.get('version'),int) or manifest['version']<1 or manifest['version']>2:
    raise ValueError('Неподдерживаемый формат резервной копии')
  # Older backups without manifest are allowed only if they have known library paths.
  allowed={'data','covers','previews'}
  valid=[];total=0
  for entry in entries:
   if entry.filename=='manifest.json' or entry.is_dir():continue
   name=entry.filename.replace('\\','/')
   rel=Path(name)
   if name.startswith('/') or re.match(r'^[A-Za-z]:',name) or rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] not in allowed:
    raise ValueError('Недопустимый путь в архиве')
   if entry.file_size>2*1024**3 or (entry.compress_size and entry.file_size/max(1,entry.compress_size)>1000):
    raise ValueError('Недопустимый размер или степень сжатия')
   total+=entry.file_size
   if total>8*1024**3:raise ValueError('Превышен лимит распаковки (8 ГиБ)')
   valid.append((entry,rel))
  if not valid:raise ValueError('В архиве нет данных библиотеки')
  with tempfile.TemporaryDirectory(prefix='library-restore-') as tmp:
   stage=Path(tmp)/'stage';stage.mkdir()
   for entry,rel in valid:
    target=stage/rel;target.parent.mkdir(parents=True,exist_ok=True)
    with z.open(entry) as inp,target.open('wb') as out:shutil.copyfileobj(inp,out)
   for file in (stage/'data').glob('*.json'):
    json.loads(file.read_text(encoding='utf8'))
   # Snapshot existing files before changing anything, for manual rollback.
   rollback=DATA/('pre_restore_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.zip')
   with LOCK:
    with zipfile.ZipFile(rollback,'w',zipfile.ZIP_DEFLATED) as back:
     for _,rel in valid:
      existing=ROOT/rel
      if existing.is_file():back.write(existing,rel.as_posix())
    for _,rel in valid:
     dest=ROOT/rel;dest.parent.mkdir(parents=True,exist_ok=True)
     temp=dest.with_name(dest.name+'.restore_tmp')
     shutil.copy2(stage/rel,temp);temp.replace(dest)
 return {'ok':True,'restored':len(valid),'rollback':str(rollback),'note':'Восстановление выполнено. Перезапустите приложение; прежние файлы сохранены в rollback ZIP.'}

def read_reviews():
 try:
  data=json.loads(REVIEWS.read_text(encoding='utf8'))
  return data if isinstance(data,dict) else {}
 except (OSError,ValueError):return {}
def write_reviews(data):
 tmp=REVIEWS.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8');tmp.replace(REVIEWS)

BASE='https://www.nongsaro.go.kr/portal/ps/psb/psbx/cropEbookLst.ps?menuId=PS65290&stdPrdlstCode=IN&sStdPrdlstCode=IN0242W4&totalSearchYn=Y'
LOCK=threading.RLock(); STATE={'running':False,'message':'Готово','processed':0,'mode':'idle'}
MAX_PDF_BYTES=2*1024*1024*1024 # 2 GiB safety limit, streamed to disk
CHUNK=1024*1024
KEYWORDS=['양봉','꿀벌','벌꿀','양봉산물','밀원','여왕벌','뒤영벌','화분매개','벌통','꿀벌 질병','프로폴리스','로열젤리','봉독','beekeeping','honeybee','apiculture']
TRANSLATIONS={'양봉':'Пчеловодство','꿀벌':'Медоносная пчела','벌꿀':'Мёд','양봉산물':'Продукты пчеловодства','밀원식물':'Медоносные растения','밀원':'Медоносы','여왕벌':'Пчелиная матка','뒤영벌':'Шмели','화분매개':'Опыление','꿀벌 질병':'Болезни пчёл','프로폴리스':'Прополис','로열젤리':'Маточное молочко','봉독':'Пчелиный яд','벌통':'Улей'}
AGENT='BeeLibraryCollector/0.4 (personal research; respectful crawler)'
ALLOWED_HOSTS={'nongsaro.go.kr','www.nongsaro.go.kr','yes24.com','www.yes24.com'}
SOURCES=DATA/'sources.json'
DEFAULT_SOURCES=[
 {'name':'Nongsaro · Южная Корея','url':BASE,'enabled':True,'kind':'government','country':'KR'},
 {'name':'YES24 · Южная Корея','url':'https://www.yes24.com/','enabled':True,'kind':'store','country':'KR'},
]
def get_sources():
 try:return json.loads(SOURCES.read_text(encoding='utf8'))
 except:return DEFAULT_SOURCES.copy()
PRESET_SOURCES=[
 ('США','Barnes & Noble','https://www.barnesandnoble.com/'),('США','Books-A-Million','https://www.booksamillion.com/'),('США','Bookshop.org','https://bookshop.org/'),
 ('Китай','JD Books','https://www.jd.com/'),('Китай','Dangdang','https://www.dangdang.com/'),
 ('Южная Корея','Kyobo','https://www.kyobobook.co.kr/'),('Южная Корея','Aladin','https://www.aladin.co.kr/'),
 ('Россия','Читай-город','https://www.chitai-gorod.ru/'),('Россия','Лабиринт','https://www.labirint.ru/'),('Россия','Литрес','https://www.litres.ru/'),
 ('Германия','Thalia','https://www.thalia.de/'),('Германия','Hugendubel','https://www.hugendubel.de/'),
 ('Польша','Empik','https://www.empik.com/'),('Польша','TaniaKsiazka','https://www.taniaksiazka.pl/'),
 ('Словакия','Martinus','https://www.martinus.sk/'),('Словакия','Panta Rhei','https://www.pantarhei.sk/'),
 ('Дания','Saxo','https://www.saxo.com/'),('Дания','Bog & idé','https://www.bog-ide.dk/'),
 ('Франция','Fnac','https://www.fnac.com/'),('Франция','Decitre','https://www.decitre.fr/'),
 ('Италия','IBS','https://www.ibs.it/'),('Испания','Casa del Libro','https://www.casadellibro.com/'),
 ('Великобритания','Waterstones','https://www.waterstones.com/'),('Чехия','Kosmas','https://www.kosmas.cz/'),
 ('Швеция','Adlibris','https://www.adlibris.com/'),('Нидерланды','bol','https://www.bol.com/'),
 ('Международный','Open Library','https://openlibrary.org/'),('Международный','Google Books','https://books.google.com/')]
def save_sources(items):SOURCES.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf8')
def source_hosts():
 hosts=set(ALLOWED_HOSTS)
 for src in get_sources():
  if src.get('enabled'):
   h=urllib.parse.urlsplit(src.get('url','')).hostname
   if h:hosts.add(h)
 return hosts
LANGUAGE_TERMS={
 'ru':['пчела','пчёлы','пчелы','пчеловодство','пчеловод','пасека','улей','медонос','матководство','апитерапия','прополис'],
 'en':['bee','bees','honeybee','honey bee','beekeeping','beekeeper','apiculture','apiary','hive','queen rearing','propolis'],
 'ko':['양봉','꿀벌','벌꿀','양봉산물','밀원식물','밀원','여왕벌','뒤영벌','화분매개','벌통','봉독','프로폴리스'],
 'zh':['蜜蜂','养蜂','養蜂','蜂蜜','蜂箱','蜂王','蜂群','蜂胶','蜂膠'],
 'ja':['養蜂','ミツバチ','蜜蜂','蜂蜜','養蜂家','巣箱','女王蜂'],
 'de':['biene','bienen','imkerei','imker','honigbiene','bienenstock','bienenzucht'],
 'fr':['abeille','abeilles','apiculture','apiculteur','ruche','miel','propolis'],
 'es':['abeja','abejas','apicultura','apicultor','colmena','miel','propóleo'],
 'pt':['abelha','abelhas','apicultura','apicultor','colmeia','mel','própolis'],
 'it':['ape','api','apicoltura','apicoltore','alveare','miele','propoli'],
 'tr':['arıcılık','arıcı','bal arısı','arı kovanı','propolis'],
 'pl':['pszczoła','pszczoły','pszczelarstwo','pszczelarz','pasieka','ul'],
 'uk':['бджола','бджоли','бджільництво','пасіка','вулик'],
 'ar':['نحل','تربية النحل','النحل','خلية النحل','عسل'],
 'hi':['मधुमक्खी','मधुमक्खी पालन','मधुमक्खियां','छत्ता'],
 'nl':['bijen','bijenteelt','imker','bijenkorf','honingbij'],
 'sv':['bi','bin','biodling','biodlare','bikupa'],
 'id':['lebah','perlebahan','peternakan lebah','sarang lebah'],
 'vi':['ong mật','nuôi ong','nghề nuôi ong','tổ ong']
}
import unicodedata,ipaddress,socket
TERMS=DATA/'keywords.json'
def get_terms():
 try:return json.loads(TERMS.read_text(encoding='utf8'))
 except:return LANGUAGE_TERMS
def classify(title):
 txt=unicodedata.normalize('NFKC',title or '').casefold();matches=[]
 for lang,words in get_terms().items():
  for word in words:
   w=unicodedata.normalize('NFKC',word).casefold()
   if len(w)<=3 and w.isascii() and w.isalpha():
    yes=bool(re.search(r'(?<![a-z])'+re.escape(w)+r'(?![a-z])',txt))
   else:yes=w in txt
   if yes:matches.append((lang,word))
 return {'relevant':bool(matches),'language':matches[0][0] if matches else '—','matched_terms':[x[1] for x in matches[:12]]}
def public_hostname(host):
 try:
  for res in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM):
   ip=ipaddress.ip_address(res[4][0])
   if not ip.is_global:return False
  return True
 except:return False

def now(): return datetime.now().isoformat(timespec='seconds')
def log(level,msg):
 with LOCK:
  with LOG.open('a',encoding='utf8') as f:f.write(json.dumps({'time':now(),'level':level,'message':str(msg)},ensure_ascii=False)+'\n')
def read_db():
 try:return json.loads(DB.read_text(encoding='utf8'))
 except:return []
def save_db(rows):DB.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
def allowed_url(url):
 try:
  p=urllib.parse.urlsplit(url)
  return p.scheme=='https' and p.hostname in source_hosts() and not p.username and not p.password and p.port in (None,443)
 except:return False
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if not allowed_url(newurl) or not public_hostname(urllib.parse.urlsplit(newurl).hostname):raise ValueError('Небезопасная переадресация заблокирована')
  return super().redirect_request(req,fp,code,msg,headers,newurl)
OPENER=urllib.request.build_opener(NoRedirect)
ROBOTS={}
def robots_ok(url):
 origin='https://'+urllib.parse.urlsplit(url).hostname
 if origin not in ROBOTS:
  rp=urllib.robotparser.RobotFileParser();rp.set_url(origin+'/robots.txt')
  try:rp.read();ROBOTS[origin]=rp
  except Exception as e:
   log('WARN','robots.txt недоступен: '+str(e));return False
 return ROBOTS[origin].can_fetch(AGENT,url)
def fetch(url,max_bytes=8*1024*1024,method='GET'):
 if not allowed_url(url) or not public_hostname(urllib.parse.urlsplit(url).hostname):raise ValueError('Недопустимый или локальный адрес сайта')
 if not robots_ok(url):raise PermissionError('robots.txt не разрешает запрос или недоступен')
 req=urllib.request.Request(url,headers={'User-Agent':AGENT,'Accept':'text/html,application/pdf,*/*'},method=method)
 with OPENER.open(req,timeout=25) as resp:
  if not allowed_url(resp.url):raise ValueError('Небезопасная переадресация')
  typ=resp.headers.get('Content-Type',''); size=resp.headers.get('Content-Length')
  if size and int(size)>max_bytes:raise ValueError(f'Размер ответа превышает лимит {max_bytes//(1024*1024)} МБ')
  data=resp.read(max_bytes+1) if method!='HEAD' else b''
  if len(data)>max_bytes:raise ValueError('Размер превышает установленный лимит')
  return data,resp.url,typ,resp.status
def normalize_url(url):
 return urllib.parse.urldefrag(url)[0]
def discover_ebook_downloads(page,base):
 """Extract native Nongsaro download URLs from viewer/list JS parameters.
 Never enumerate arbitrary numeric ebook codes.
 """
 found=set()
 # Some listings use ebookCode in javascript handlers, not href links.
 codes=set(re.findall(r'(?i)ebookCode\s*[:=]\s*[\"\']?(\d{1,6})',page))
 codes.update(re.findall(r'(?i)(?:cropEbookFileViewPop|cropsEbookWonGoFileDownload)\.(?:ps|do)\?[^\"\'<>\s]*?ebookCode=(\d{1,6})',html.unescape(page)))
 # A frequent onclick signature uses an ebook code as a positional argument.
 for match in re.finditer(r'(?i)(?:ebook|wonGo|fileView)[A-Za-z_]*\s*\(\s*[\"\']?(\d{1,6})[\"\']?\s*[,)]',page):
  codes.add(match.group(1))
 for code in codes:
  found.add('https://www.nongsaro.go.kr/portal/ps/psb/psbx/cropsEbookWonGoFileDownload.do?cropsEbookFileNo=00001&ebookCode='+code)
 for raw in re.findall(r'(?i)(?:cropsEbookWonGoFileDownload\.do|cropEbookFileViewPop\.ps)\?[^\s\"\'<>\\]+',page):
  url=urllib.parse.urljoin(base,html.unescape(raw).replace('\\/','/'))
  if 'cropEbookFileViewPop.ps' in url:
   query=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
   if 'ebookCode' in query:
    found.add('https://www.nongsaro.go.kr/portal/ps/psb/psbx/cropsEbookWonGoFileDownload.do?cropsEbookFileNo=00001&ebookCode='+query['ebookCode'][0])
  else:found.add(url)
 return {u for u in found if allowed_url(u)}
def pdf_candidate(url):
 return bool(re.search(r'\.pdf(?:[?#]|$)|cropsEbookWonGoFileDownload\.do|cropsEbookWonFileView\.ps',url,re.I))
def pdf_stream(url, destination=None, probe=False):
 if urllib.parse.urlsplit(url).hostname in ('yes24.com','www.yes24.com') and not urllib.parse.urlsplit(url).path.lower().endswith('.pdf'):
  raise PermissionError('YES24: карточка товара не является свободным PDF')
 """Bounded stream; probe uses a byte range, never reads a whole PDF into RAM."""
 if not allowed_url(url) or not public_hostname(urllib.parse.urlsplit(url).hostname):raise ValueError('Недопустимый URL')
 if not robots_ok(url):raise PermissionError('robots.txt не разрешает запрос')
 headers={'User-Agent':AGENT,'Accept':'application/pdf,*/*'}
 if probe:headers['Range']='bytes=0-4095'
 req=urllib.request.Request(url,headers=headers)
 with OPENER.open(req,timeout=60) as resp:
  if not allowed_url(resp.url):raise ValueError('Небезопасная переадресация')
  code=resp.status;ctype=resp.headers.get('Content-Type','')
  size=int(resp.headers.get('Content-Length') or 0)
  if not probe and size>MAX_PDF_BYTES:raise ValueError('PDF превышает защитный предел 2 ГиБ')
  first=resp.read(4096 if probe else 5)
  if not first.startswith(b'%PDF-'):raise ValueError('Ответ не PDF (возможно HTML-страница или отказ сервера)')
  if probe:return {'http':code,'size':size if code==200 else int(resp.headers.get('Content-Range','').split('/')[-1]) if resp.headers.get('Content-Range','').split('/')[-1].isdigit() else 0,'type':ctype}
  digest=hashlib.sha256();total=0
  try:
   with open(destination,'wb') as out:
    out.write(first);digest.update(first);total=len(first)
    while True:
     block=resp.read(CHUNK)
     if not block:break
     total+=len(block)
     if total>MAX_PDF_BYTES:raise ValueError('PDF превышает защитный предел 2 ГиБ')
     out.write(block);digest.update(block)
   return {'http':code,'size':total,'sha256':digest.hexdigest()}
  except:
   try:Path(destination).unlink(missing_ok=True)
   except:pass
   raise

class Parser(HTMLParser):
 def __init__(self):super().__init__();self.links=[];self.text=[];self.title='';self._title=False
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='title':self._title=True
  if tag in ('a','iframe','embed','object'):
   link=a.get('href') or a.get('src') or a.get('data')
   if link:self.links.append((link,a.get('title') or a.get('download') or ''))
  if a.get('onclick'):
   for s in re.findall(r"['\"]([^'\"]+(?:\.pdf|cropEbook[^'\"]*|download[^'\"]*))['\"]",a['onclick'],re.I):self.links.append((s,''))
 def handle_endtag(self,tag):
  if tag=='title':self._title=False
 def handle_data(self,data):
  if self._title:self.title+=data
  self.text.append(data)
def title_ru(korean):
 for key in sorted(TRANSLATIONS,key=len,reverse=True):
  if key in korean:return TRANSLATIONS[key]+' — '+korean if korean!=key else TRANSLATIONS[key]
 return 'Требуется перевод'
def relevant(s):return any(k.lower() in s.lower() for k in KEYWORDS)
def upsert(rows,url,title='',source='',status='Найдено',kind='PDF'):
 for row in rows:
  if row['url']==url:
   if title and (not row.get('title_ko') or row['title_ko']=='Без названия'):row['title_ko']=title;row['title_ru']=title_ru(title);row.update(classify(title))
   return row
 row={'id':hashlib.sha256(url.encode()).hexdigest()[:12],'title_ko':title or 'Без названия','title_ru':title_ru(title or ''),'url':url,'source':source,'type':kind,'status':status,'http':'','size':'','sha256':'','file':'','updated':now(),'rights':'Не проверены'}
 row.update(classify(title));row['category']='Книга PDF' if kind=='PDF' else 'Карточка';rows.append(row);return row
def scan(start,max_pages):
 with LOCK:
  if STATE['running']:return
  STATE.update(running=True,message='Поиск страниц',processed=0,mode='scan')
 try:
  rows=read_db();queue=[start];seen=set();found=0
  while queue and len(seen)<max_pages:
   url=queue.pop(0)
   if url in seen:continue
   seen.add(url)
   try:
    if pdf_candidate(url):
     upsert(rows,url,source=start);found+=1;STATE["processed"]=len(seen);continue
    data,final,typ,code=fetch(url)
    if data[:5]==b'%PDF-' or 'application/pdf' in typ.lower():
     upsert(rows,final,source=url);found+=1;log('INFO','PDF: '+final);continue
    if 'html' not in typ.lower() and b'<html' not in data[:1000].lower():continue
    enc='utf-8';match=re.search(br'charset=["\']?([\w-]+)',data[:5000],re.I)
    if match:enc=match.group(1).decode('ascii','ignore')
    try:page=data.decode(enc,errors='replace')
    except:page=data.decode('utf-8',errors='replace')
    p=Parser();p.feed(page)
    for candidate in discover_ebook_downloads(page,final):
     upsert(rows,candidate,p.title,final);found+=1
    for raw,label in p.links:
     absolute=urllib.parse.urljoin(final,html.unescape(raw).strip());absolute=urllib.parse.urldefrag(absolute)[0]
     if not allowed_url(absolute):continue
     if pdf_candidate(absolute):
      upsert(rows,absolute,label or p.title,final);found+=1
     elif ('cropEbook' in absolute or 'ebook' in absolute.lower()) and absolute not in seen and absolute not in queue and len(queue)<150:
      queue.append(absolute)
    # PDF references embedded in script and JSON
    for raw in re.findall(r'''["']([^"']+?\.pdf(?:\?[^"']*)?)["']''',page,re.I):
     absolute=urllib.parse.urljoin(final,html.unescape(raw).replace('\\/','/'))
     if allowed_url(absolute):upsert(rows,absolute,p.title,final);found+=1
    for m in re.finditer(r'cropsEbookWonGoFileDownload\.do\?[^\s"\'<>]+',page,re.I):
     candidate=urllib.parse.urljoin(final,html.unescape(m.group(0)).replace('\\/','/'))
     if allowed_url(candidate):upsert(rows,candidate,p.title,final);found+=1
    STATE['processed']=len(seen);STATE['message']=f'Обработано страниц: {len(seen)}, PDF-кандидатов: {found}'
    log('INFO',f'Просмотрена страница: {final}, HTML-ссылок: {len(p.links)}, PDF-кандидатов всего: {len(rows)}')
   except Exception as e:log('ERROR',f'{url}: {e}')
   time.sleep(1.2)
  with LOCK:save_db(rows)
  STATE['message']=f'Завершено. Страниц: {len(seen)}; найденных ссылок PDF: {len([r for r in rows if r["type"]=="PDF"])}'
 except Exception as e:log('ERROR','Сбой поиска: '+str(e));STATE['message']='Ошибка поиска'
 finally:STATE['running']=False;STATE['mode']='idle'
def scan_mask(start,max_pages,mask):
 """Domain-scoped breadth-first crawl, collecting linked PDFs and native Nongsaro downloads.
 YES24 product pages are bibliographic records; commercial ebook/preview assets are never inferred as free PDFs.
 """
 with LOCK:
  if STATE['running']:return
  STATE.update(running=True,mode='mask',message='Обход раздела',processed=0)
 try:
  rows=read_db();queue=[start];seen=set();new=0
  host=urllib.parse.urlsplit(start).hostname
  prefix=urllib.parse.urlsplit(start).path.rsplit('/',1)[0]+'/'
  is_yes24=host in ('yes24.com','www.yes24.com')
  while queue and len(seen)<max_pages:
   url=queue.pop(0)
   if url in seen:continue
   seen.add(url)
   try:
    data,final,typ,code=fetch(url,max_bytes=12*1024*1024)
    if data.startswith(b'%PDF-'):
     if fnmatch.fnmatch(urllib.parse.unquote(urllib.parse.urlsplit(final).path.rsplit('/',1)[-1]).lower(),mask.lower()):
      before=len(rows);upsert(rows,final,source=url);new+=len(rows)-before
     continue
    if not ('html' in typ.lower() or b'<html' in data[:1000].lower()):continue
    enc='utf-8';m=re.search(br'charset=["\']?([\w-]+)',data[:5000],re.I)
    if m:enc=m.group(1).decode('ascii','ignore')
    page=data.decode(enc,'replace');parser=Parser();parser.feed(page)
    # YES24: index title and link, not paid ebook contents or preview pages.
    if is_yes24 and re.search(r'/product/goods/\d+',urllib.parse.urlsplit(final).path,re.I):
     before=len(rows);r=upsert(rows,final,parser.title or 'YES24',final,status='Карточка книги — не PDF',kind='Каталог')
     r['rights']='Не разрешено';new+=len(rows)-before
    # Other stores: keep bibliographic records, never treat preview content as free books.
    if not is_yes24 and host not in ('www.nongsaro.go.kr','nongsaro.go.kr') and not pdf_candidate(final):
     before=len(rows);upsert(rows,final,parser.title or final,final,status='Карточка / страница — не PDF',kind='Каталог');new+=len(rows)-before
    candidates=set()
    for raw,label in parser.links:
     u=normalize_url(urllib.parse.urljoin(final,html.unescape(raw).strip()))
     if allowed_url(u):candidates.add(u)
    for raw in re.findall(r"[^\s]+?\.pdf(?:\?[^\s]+)?",page,re.I):
     u=normalize_url(urllib.parse.urljoin(final,html.unescape(raw).replace('\\/','/')))
     if allowed_url(u):candidates.add(u)
    if host in ('nongsaro.go.kr','www.nongsaro.go.kr'):candidates.update(discover_ebook_downloads(page,final))
    for u in candidates:
     parsed=urllib.parse.urlsplit(u)
     if parsed.hostname!=host:continue
     filename=urllib.parse.unquote(parsed.path.rsplit('/',1)[-1])
     native=not is_yes24 and pdf_candidate(u) and not filename.lower().endswith('.pdf')
     if (fnmatch.fnmatch(filename.lower(),mask.lower()) or (mask.lower()=='*.pdf' and native)) and pdf_candidate(u):
      before=len(rows);upsert(rows,u,parser.title,final);new+=len(rows)-before
     elif not is_yes24 and parsed.path.startswith(prefix) and (parsed.path.lower().endswith(('.ps','.html','.htm','.php','.asp','.aspx')) or 'ebook' in parsed.path.lower()) and u not in seen and u not in queue and len(queue)<1000:
      queue.append(u)
    STATE['processed']=len(seen);STATE['message']=f'Страниц: {len(seen)}; новых записей: {new}; очередь: {len(queue)}'
    log('INFO',f'Обход {final}; ссылок {len(candidates)}; новых записей {new}')
   except Exception as e:log('ERROR',f'{url}: {e}')
   time.sleep(1.2)
  with LOCK:save_db(rows)
  STATE['message']=f'Готово: страниц {len(seen)}, новых записей {new}. Маска {mask}'
 except Exception as e:log('ERROR',str(e));STATE['message']='Ошибка поиска по маске'
 finally:STATE['running']=False;STATE['mode']='idle'
def bulk_download():
 with LOCK:
  if STATE['running']:return
  STATE.update(running=True,mode='bulk',message='Массовое скачивание разрешённых PDF',processed=0)
 try:
  ids=[r['id'] for r in read_db() if r.get('rights')=='Разрешено' and r.get('status')!='Скачан']
  for i,id in enumerate(ids,1):
   download_one(id);STATE['processed']=i;STATE['message']=f'Скачивание {i}/{len(ids)}';time.sleep(1.5)
  STATE['message']=f'Завершено: обработано {len(ids)} PDF'
 finally:STATE['running']=False;STATE['mode']='idle'
def check_one(id):
 with LOCK:
  rows=read_db();row=next((r for r in rows if r['id']==id),None)
  if not row:raise ValueError('Запись не найдена')
 try:
  result=pdf_stream(row['url'],probe=True)
  row.update(status='PDF подтверждён',http=result['http'],size=result['size'],updated=now());log('INFO','PDF проверен без полной загрузки: '+row['url'])
 except Exception as e:row.update(status='Ошибка проверки',updated=now());log('ERROR',f'Проверка {row["url"]}: {e}')
 with LOCK:save_db(rows)
 return row
def download_one(id):
 with LOCK:
  rows=read_db();row=next((r for r in rows if r['id']==id),None)
  if not row:raise ValueError('Запись не найдена')
 if row['rights']!='Разрешено':raise PermissionError('Сначала подтвердите разрешение на скачивание этой книги')
 name=row['id']+'.pdf';partial=DOWNLOADS/(name+'.part')
 try:
  result=pdf_stream(row['url'],destination=partial)
  partial.replace(DOWNLOADS/name)
  row.update(status='Скачан',http=result['http'],size=result['size'],sha256=result['sha256'],file=name,updated=now())
  log('INFO',f'Скачан {name}, {result["size"]} байт')
 except Exception as e:
  partial.unlink(missing_ok=True)
  row.update(status='Ошибка скачивания',updated=now());log('ERROR',f'Скачивание {row["url"]}: {e}')
 with LOCK:save_db(rows)
 return row
def export_csv(rows):
 output=io.StringIO();fields=['id','title_ko','title_ru','relevant','language','matched_terms','category','url','source','status','http','size','sha256','file','rights','updated','author','publisher','year','isbn','description','cover_url','store_url','notes','format','file_path'];w=csv.DictWriter(output,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows([{**r,'matched_terms':', '.join(r.get('matched_terms',[]))} for r in rows]);return ('\ufeff'+output.getvalue()).encode('utf8')
def export_xlsx(rows):
 fields=['id','title_ko','title_ru','relevant','language','matched_terms','category','url','source','status','http','size','sha256','file','rights','updated']
 def cell(val,idx):
  v=escape(str(val or ''));return f'<c r="{idx}" t="inlineStr"><is><t>{v}</t></is></c>'
 def col(n):
  s='';n+=1
  while n:s=chr(65+(n-1)%26)+s;n=(n-1)//26
  return s
 lines=[]
 for i,vals in enumerate([fields]+[[', '.join(r.get(k,[])) if isinstance(r.get(k),list) else r.get(k,'') for k in fields] for r in rows],1):lines.append('<row r="%d">%s</row>'%(i,''.join(cell(v,f'{col(j)}{i}') for j,v in enumerate(vals))))
 sheet='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+''.join(lines)+'</sheetData></worksheet>'
 buf=io.BytesIO()
 with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
  z.writestr('[Content_Types].xml','<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
  z.writestr('_rels/.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
  z.writestr('xl/workbook.xml','<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Каталог" sheetId="1" r:id="rId1"/></sheets></workbook>')
  z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
  z.writestr('xl/worksheets/sheet1.xml',sheet)
 return buf.getvalue()

# v0.6: local formats and bibliographic catalogizer
import mimetypes
from html import unescape as html_unescape
SUPPORTED_EXT={'.pdf','.epub','.djvu','.djv','.fb2','.mobi','.azw3','.cbz','.cbr','.txt','.rtf','.docx'}
LOCAL_CONFIG=DATA/'local_folders.json'
ATTACHMENTS=ROOT/'attachments';ATTACHMENTS.mkdir(exist_ok=True)
COVERS=ROOT/'covers';COVERS.mkdir(exist_ok=True)
def folders():
 try:return json.loads(LOCAL_CONFIG.read_text(encoding='utf8'))
 except:return []
def save_folders(items):LOCAL_CONFIG.write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf8')
def local_file_id(path):return hashlib.sha256(('local:'+str(path.resolve())).encode()).hexdigest()[:12]
# v0.9: automatic previews, cover variants and ISBN lookup
import posixpath,queue
PREVIEW_QUEUE=queue.Queue(maxsize=10000)
PREVIEWS=ROOT/'previews';PREVIEWS.mkdir(exist_ok=True)
def preview_image(path):
 ext=path.suffix.lower()
 if ext=='.pdf':
  try:
   import fitz
   if hasattr(fitz,"TOOLS"):fitz.TOOLS.mupdf_display_errors(False)
   with fitz.open(path) as doc:
    if not doc.page_count:return None
    page=doc[0]
    scale=min(1.0, 850/max(page.rect.width,1), 1100/max(page.rect.height,1))
    return page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False,colorspace=fitz.csRGB).tobytes('png')
  except ImportError:return None
 if ext=='.epub':
  try:
   with zipfile.ZipFile(path) as z:
    for name in z.namelist():
     if name.lower().endswith(('.png','.jpg','.jpeg','.webp')) and ('cover' in name.lower() or 'front' in name.lower()):
      data=z.read(name)
      if len(data)<8*1024*1024:return data
  except (OSError,zipfile.BadZipFile):pass
 if ext in ('.cbz','.epub'):
  try:
   with zipfile.ZipFile(path) as z:
    for name in sorted(z.namelist()):
     if name.lower().endswith(('.png','.jpg','.jpeg','.webp')):
      data=z.read(name)
      if len(data)<8*1024*1024:return data
  except (OSError,zipfile.BadZipFile):pass
 if ext in ('.djvu','.djv'):
  exe=find_ddjvu()
  if exe:
   try:
    from PIL import Image
    for fmt in ('png','ppm'):
     proc=subprocess.run([exe,f'-format={fmt}','-page=1','-size=900x1200',str(path)],capture_output=True,timeout=45)
     if proc.returncode==0 and proc.stdout:
      buf=io.BytesIO();Image.open(io.BytesIO(proc.stdout)).convert('RGB').save(buf,'PNG');return buf.getvalue()
   except (ImportError,OSError,subprocess.TimeoutExpired,ValueError) as exc:
    log('WARN','DJVU обложка: '+str(exc)[:180])
 return None
def make_preview(fid,path):
 target=PREVIEWS/(fid+'.png')
 if target.is_file() and target.stat().st_mtime>=path.stat().st_mtime:return target
 try:
  data=preview_image(path)
  if not data:return None
  from PIL import Image
  im=Image.open(io.BytesIO(data))
  if im.width*im.height>45_000_000:raise ValueError('Изображение слишком большое для безопасного превью')
  im.thumbnail((650,950));buf=io.BytesIO();im.convert('RGB').save(buf,'PNG');target.write_bytes(buf.getvalue());return target
 except Exception as exc:log('WARN','Превью '+path.name+': '+str(exc)[:180]);return None

def cover_images(fid):
 files=[]
 for ext in ('.jpg','.jpeg','.png','.webp'):
  p=COVERS/(fid+ext)
  if p.is_file():files.append(('manual',p))
 p=PREVIEWS/(fid+'.png')
 if p.is_file():files.append(('preview',p))
 return files

def isbn_cover(isbn):
 code=re.sub(r'[^0-9X]','',isbn.upper())
 if len(code) not in (10,13):raise ValueError('Нужен ISBN-10 или ISBN-13')
 urls=['https://covers.openlibrary.org/b/isbn/'+code+'-L.jpg?default=false']
 req=urllib.request.Request('https://www.googleapis.com/books/v1/volumes?'+urllib.parse.urlencode({'q':'isbn:'+code,'maxResults':1}),headers={'User-Agent':AGENT})
 try:
  with urllib.request.urlopen(req,timeout=12) as response:
   info=json.load(response)
  links=(info.get('items') or [{}])[0].get('volumeInfo',{}).get('imageLinks',{})
  if links.get('thumbnail'):urls.append(links['thumbnail'].replace('http://','https://'))
 except Exception as exc:log('WARN','Google Books ISBN: '+str(exc)[:130])
 for url in urls:
  try:return fetch_cover_image(url)
  except Exception as exc:log('WARN','ISBN cover: '+str(exc)[:130])
 raise ValueError('Обложка для этого ISBN не найдена')

def isbn_candidates(raw):
 codes=[]
 for v in re.findall(r'(?<![0-9])(?:97[89][0-9-]{10,22}|[0-9][0-9-]{8,18}[0-9Xx])(?![0-9])',str(raw or '')):
  code=re.sub('[^0-9X]','',v.upper())
  if len(code) in (10,13) and code not in codes:codes.append(code)
 if not codes:raise ValueError('Укажите корректный ISBN-10 или ISBN-13')
 results=[];errors=[]
 for code in codes[:4]:
  for provider,url in [('Google Books','https://www.googleapis.com/books/v1/volumes?'+urllib.parse.urlencode({'q':'isbn:'+code,'maxResults':3})),('Open Library','https://openlibrary.org/api/books?'+urllib.parse.urlencode({'bibkeys':'ISBN:'+code,'format':'json','jscmd':'data'}))]:
   try:
    req=urllib.request.Request(url,headers={'User-Agent':AGENT,'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=12) as resp: data=json.load(resp)
    if provider=='Google Books':
     items=[x.get('volumeInfo',{}) for x in data.get('items',[])]
     for x in items:
      results.append({'provider':provider,'isbn':code,'title':x.get('title',''),'author':', '.join(x.get('authors',[])),'publisher':x.get('publisher',''),'year':x.get('publishedDate','')[:4],'description':re.sub('<[^>]*>','',x.get('description',''))[:2500],'cover_url':x.get('imageLinks',{}).get('thumbnail','').replace('http://','https://'),'source_url':x.get('infoLink','')})
    else:
     x=data.get('ISBN:'+code)
     if x:results.append({'provider':provider,'isbn':code,'title':x.get('title',''),'author':', '.join(a.get('name','') for a in x.get('authors',[])),'publisher':', '.join(a.get('name','') for a in x.get('publishers',[])),'year':str(x.get('publish_date','')),'description':str(x.get('notes',''))[:2500],'cover_url':x.get('cover',{}).get('large',''),'source_url':x.get('url','')})
   except Exception as exc:errors.append(provider+': '+str(exc)[:100])
 return {'candidates':results,'errors':errors,'isbn':codes[0]}

TRANSLATE_STATE={'running':False,'done':0,'total':0,'errors':0,'message':'Не запущен'}
def batch_translate_worker(ids=None):
 TRANSLATE_STATE.update(running=True,done=0,total=0,errors=0,message='Подготовка')
 try:
  with LOCK:
   todo=[(r['id'],r.get('title_ko','')) for r in read_db() if r.get('file_path') and (not ids or r['id'] in ids) and not str(r.get('title_ru','')).strip()]
  TRANSLATE_STATE['total']=len(todo)
  for i,(fid,title) in enumerate(todo,1):
   try:
    v=translate_title(title);translation=str(v.get('translation','')).strip()
    if translation and translation.casefold()!=clean_display_title(title).casefold():
     with LOCK:
      rows=read_db();r=next((x for x in rows if x['id']==fid),None)
      if r and not r.get('title_ru'):r['title_ru_suggested']=translation;r['translation_provider']=v.get('provider','');save_db(rows)
   except Exception as exc:TRANSLATE_STATE['errors']+=1;log('WARN','Пакетный перевод: '+str(exc)[:120])
   TRANSLATE_STATE.update(done=i,message=f'Обработано {i} из {len(todo)}')
   time.sleep(.4)
 finally:TRANSLATE_STATE['running']=False

def apply_isbn_candidate(fid,fields):
 allowed=('isbn','author','publisher','year','description','cover_url','title_ru','title_ko')
 with LOCK:
  rows=read_db();r=next((x for x in rows if x['id']==fid),None)
  if not r:raise ValueError('Книга не найдена')
  for key in allowed:
   if key in fields and isinstance(fields[key],str):r[key]=fields[key].strip()[:3000 if key=='description' else 500]
  r['updated']=now();save_db(rows)
 return {'ok':True}

def epub_spine(fid):
 path=book_file(fid)
 if path.suffix.lower()!='.epub':raise ValueError('Это не EPUB')
 with zipfile.ZipFile(path) as z:
  container=ET.fromstring(z.read('META-INF/container.xml'))
  opf=next(x.attrib['full-path'] for x in container.iter() if x.tag.endswith('rootfile'))
  base=posixpath.dirname(opf)
  root=ET.fromstring(z.read(opf))
  items={x.attrib['id']:x.attrib for x in root.iter() if x.tag.endswith('item') and 'id' in x.attrib}
  spine=[x.attrib['idref'] for x in root.iter() if x.tag.endswith('itemref')]
  chapters=[]
  for key in spine:
   item=items.get(key,{})
   if 'html' not in item.get('media-type',''):continue
   name=posixpath.normpath(posixpath.join(base,urllib.parse.unquote(item.get('href',''))))
   if name in z.namelist():chapters.append({'path':name,'title':posixpath.basename(name)})
  return chapters[:500]

def epub_resource(fid,name):
 path=book_file(fid)
 if path.suffix.lower()!='.epub':raise ValueError('Это не EPUB')
 name=urllib.parse.unquote(name)
 if not name or name.startswith('/') or '..' in name.split('/') or '\\' in name:raise ValueError('Недопустимый путь')
 with zipfile.ZipFile(path) as z:
  if name not in z.namelist():raise ValueError('Ресурс отсутствует')
  entry=z.getinfo(name)
  if entry.file_size>12*1024*1024:raise ValueError('Ресурс слишком большой')
  data=z.read(name)
 ext=Path(name).suffix.lower()
 mime={'.xhtml':'application/xhtml+xml','.html':'text/html','.css':'text/css','.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif','.svg':'image/svg+xml','.webp':'image/webp','.woff':'font/woff','.woff2':'font/woff2'}.get(ext)
 if not mime:raise ValueError('Тип ресурса не поддерживается')
 if ext in ('.xhtml','.html'):
  # Strip active content and external fetches; images and CSS remain in the EPUB.
  from html.parser import HTMLParser
  class Cleaner(HTMLParser):
   def __init__(self):super().__init__(convert_charrefs=True);self.out=[];self.skip=0
   def handle_starttag(self,tag,attrs):
    tag=tag.lower()
    if tag in ('script','iframe','object','embed','form','video','audio'):
     self.skip+=1;return
    if self.skip:return
    allowed={'html','head','body','div','section','article','p','span','h1','h2','h3','h4','h5','h6','blockquote','ul','ol','li','img','svg','path','figure','figcaption','br','hr','em','strong','b','i','u','sup','sub','table','thead','tbody','tr','td','th','a','link','style','meta'}
    if tag not in allowed:return
    safe=[]
    for k,v in attrs:
     if not v or k.lower().startswith('on') or k.lower() in ('srcset','style'):continue
     if k in ('src','href'):
      if v.startswith(('http:','https:','//','data:','javascript:','#')):continue
      relative=posixpath.normpath(posixpath.join(posixpath.dirname(name),urllib.parse.unquote(v.split('#')[0])))
      if relative.startswith('../'):continue
      v='/api/epub/resource?'+urllib.parse.urlencode({'id':fid,'path':relative})
     elif k not in ('alt','title','class','id','width','height','rel','type','viewBox','d','xmlns'):continue
     safe.append(' '+k+'="'+html.escape(v,quote=True)+'"')
    self.out.append('<'+tag+''.join(safe)+'>')
   def handle_endtag(self,tag):
    if tag.lower() in ('script','iframe','object','embed','form','video','audio'):
     self.skip=max(0,self.skip-1);return
    if not self.skip:self.out.append('</'+tag+'>')
   def handle_data(self,data):
    if not self.skip:self.out.append(html.escape(data))
   def handle_entityref(self,name):
    if not self.skip:self.out.append('&'+name+';')
  cleaner=Cleaner();cleaner.feed(data.decode('utf-8',errors='replace'))
  data=('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src none; img-src self data:; style-src self unsafe-inline; font-src self;"><style>body{max-width:780px;margin:30px auto;padding:0 20px;font:19px/1.7 Georgia,serif}img,svg{max-width:100%;height:auto}</style>'+''.join(cleaner.out)).encode()
 return data,mime


# v1.0: local bibliographic extraction and duplicate detection
from metadata_engine import extract as extract_metadata, sha256_file, valid_isbn

def analyze_record(row, calculate_hash=True):
 path=Path(row.get('file_path',''))
 if not path.is_file():return {'error':'Файл отсутствует'}
 result=extract_metadata(path)
 md=result.get('metadata',{})
 changed=[]
 mapping={'title':'title_ko','author':'author','publisher':'publisher','year':'year','isbn':'isbn','description':'description'}
 for source,target in mapping.items():
  val=str(md.get(source,'')).strip()
  if not val:continue
  # Keep manually edited metadata; filename titles are provisional.
  if target=='title_ko':
   old=row.get(target,'')
   if not old or old==path.stem.replace('_',' ').strip():row[target]=val[:500];changed.append(target)
  elif not row.get(target):row[target]=val[:6000];changed.append(target)
 if calculate_hash and not row.get('sha256'):
  row['sha256']=sha256_file(path);changed.append('sha256')
 row['metadata_checked']=now();row['metadata_warnings']=result.get('warnings',[])
 row['metadata_suggestions']=md
 row.update(classify(' '.join([row.get('title_ko',''),row.get('description','')[:500]])))
 if not row.get('title_ru') or row.get('title_ru')==title_ru(path.stem):
  # Explicitly a suggestion, never pretend it is a verified translation.
  row['translation_suggested']=True
 return {'id':row['id'],'changed':changed,'warnings':result.get('warnings',[]),'metadata':md}

def duplicate_key(row):
 name=clean_display_title(row.get('title_ko') or '')
 name=re.sub(r'(?i)\b(?:compressed|retail|scan|scanned|ebook|e-book|pdf|djvu|epub|сжатый|сжатая|копия|скан)\b',' ',name)
 name=re.sub(r'(?i)\s*\(\s*\d+\s*\)\s*$',' ',name)
 name=re.sub(r'(?i)\s*[-_]\s*(?:copy|копия|compressed|сжатый)\s*\d*$',' ',name)
 return norm_title(name)

def duplicate_groups(rows):
 # Indexed exact title matches plus fuzzy comparisons in bounded buckets.
 from collections import defaultdict
 groups=[];parent=list(range(len(rows)))
 def find(i):
  while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
  return i
 def union(i,j):parent[find(j)]=find(i)
 by_title=defaultdict(list);by_isbn=defaultdict(list);by_hash=defaultdict(list)
 for i,r in enumerate(rows):
  title=duplicate_key(r)
  if title and len(title)>=5:by_title[title].append(i)
  isbn=re.sub(r'[^0-9X]','',str(r.get('isbn') or '').upper())
  if len(isbn) in (10,13):by_isbn[isbn].append(i)
  if r.get('sha256'):by_hash[r['sha256']].append(i)
 for buckets in (by_title,by_isbn,by_hash):
  for indexes in buckets.values():
   for j in indexes[1:]:union(indexes[0],j)
 # Handle punctuation, author suffixes and spelling variants.
 buckets=defaultdict(list)
 for i,r in enumerate(rows):
  title=duplicate_key(r)
  if len(title)>=9:buckets[title[:10]].append(i)
 for indexes in buckets.values():
  if len(indexes)>250:continue
  for pos,i in enumerate(indexes):
   a=duplicate_key(rows[i])
   for j in indexes[pos+1:]:
    b=duplicate_key(rows[j]);sim=difflib.SequenceMatcher(None,a,b).ratio()
    if sim>=.88 or (min(len(a),len(b))>=12 and (a in b or b in a) and min(len(a),len(b))/max(len(a),len(b))>=.78):union(i,j)
 components=defaultdict(list)
 for i in range(len(rows)):components[find(i)].append(i)
 for indexes in components.values():
  if len(indexes)<2:continue
  first=rows[indexes[0]]
  groups.append({'id':first['id'],'title':first.get('title_ko',''),'matches':[{'id':rows[j]['id'],'title':rows[j].get('title_ko',''),'reason':'Совпадение названия / ISBN / хэша','score':1.0} for j in indexes[1:]]})
 return sorted(groups,key=lambda g:-len(g['matches']))

def analyze_library(ids=None):
 with LOCK:
  rows=read_db();reports=[]
  for r in rows:
   if not r.get('file_path') or (ids is not None and r['id'] not in ids):continue
   try:reports.append(analyze_record(r))
   except Exception as e:reports.append({'id':r['id'],'error':str(e)});log('ERROR',f"Анализ {r['id']}: {e}")
  save_db(rows)
 return {'analyzed':len(reports),'updated':sum(bool(x.get('changed')) for x in reports),'reports':reports[:100], 'duplicates':len(duplicate_groups(rows))}

def _preview_worker():
 while True:
  fid,filename=PREVIEW_QUEUE.get()
  try:
   path=Path(filename)
   if path.is_file() and make_preview(fid,path):
    with LOCK:
     rows=read_db()
     for row in rows:
      if row.get('id')==fid:
       row['cover_preview']='/api/cover/preview?id='+fid
       break
     save_db(rows)
  except Exception as exc:log('WARN','Фоновое превью: '+str(exc)[:180])
  finally:PREVIEW_QUEUE.task_done()
threading.Thread(target=_preview_worker,daemon=True,name='preview-worker').start()

def import_folder(path,recursive=True):
 root=Path(path).expanduser().resolve()
 if not root.is_dir():raise ValueError('Папка не существует на компьютере, где запущен сервер')
 count=0;updated=0;queued=0;new_ids=[]
 with LOCK:
  history=scan_history();is_repeat=root.as_posix() in history
  rows=read_db();by_id={r['id']:r for r in rows}
  for file in (root.rglob('*') if recursive else root.iterdir()):
   if not file.is_file() or file.suffix.lower() not in SUPPORTED_EXT:continue
   try:
    stat=file.stat();fid=local_file_id(file);old=by_id.get(fid)
    if old:
     old.update(file_path=str(file),size=stat.st_size,updated=now(),status='Локальный файл');updated+=1
    else:
     title=file.stem.replace('_',' ').strip()
     old={'id':fid,'title_ko':title,'title_ru':'','url':'','source':'Локальная папка: '+str(root),'type':'Локальная книга','category':'Локальная книга','status':'Локальный файл','http':'','size':stat.st_size,'sha256':'','file':'','file_path':str(file),'format':file.suffix.lower().lstrip('.'),'rights':'Личный архив','updated':now(),'added_at':now(),'author':'','publisher':'','year':'','isbn':'','description':'','cover_url':'','store_url':'','notes':''}
     old.update(classify(title));old['new_since_scan']=bool(is_repeat);rows.append(old);by_id[fid]=old;count+=1
     if is_repeat:new_ids.append(fid)
    preview=PREVIEWS/(fid+'.png')
    if not (preview.is_file() and preview.stat().st_mtime>=stat.st_mtime):
     try:PREVIEW_QUEUE.put_nowait((fid,str(file)));queued+=1
     except queue.Full:log('WARN','Очередь превью заполнена')
   except OSError as e:log('WARN',f'Файл {file}: {e}')
  save_db(rows)
  history[root.as_posix()]={'last_scan':now(),'new_on_last_scan':new_ids,'last_added':count}
  write_scan_history(history)
 log('INFO',f'Быстрый импорт {root}: новых {count}, обновлено {updated}, превью в очереди {queued}. Метаданные анализируются отдельно.')
 return {'added':count,'updated':updated,'total':len(rows),'previews_queued':queued,'new_on_last_scan':len(new_ids),'note':'Быстрый импорт завершён. Превью создаются в фоне; для извлечения ISBN и авторов используйте «Проанализировать все локальные книги».'}

class MetaParser(HTMLParser):
 def __init__(self):super().__init__();self.meta={};self.title='';self.in_title=False;self.jsonld=[];self.in_json=False;self.jsonbuf=''
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='meta':
   key=(a.get('property') or a.get('name') or '').lower();val=a.get('content','')
   if key and val:self.meta[key]=val
  if tag=='title':self.in_title=True
  if tag=='script' and a.get('type','').lower()=='application/ld+json':self.in_json=True;self.jsonbuf=''
 def handle_endtag(self,tag):
  if tag=='title':self.in_title=False
  if tag=='script' and self.in_json:
   self.in_json=False
   try:self.jsonld.append(json.loads(self.jsonbuf))
   except:pass
 def handle_data(self,data):
  if self.in_title:self.title+=data
  if self.in_json:self.jsonbuf+=data

def metadata_from_url(url):
 data,final,typ,status=fetch(url,max_bytes=6*1024*1024)
 if 'html' not in typ.lower() and b'<html' not in data[:1000].lower():raise ValueError('Страница не является HTML')
 m=re.search(br'charset=[\"\']?([\w-]+)',data[:5000],re.I)
 enc=m.group(1).decode('ascii','ignore') if m else 'utf-8'
 page=data.decode(enc,errors='replace');parser=MetaParser();parser.feed(page)
 md=parser.meta
 result={'title_ko':html_unescape(md.get('og:title') or md.get('twitter:title') or parser.title).strip()[:500], 'author':md.get('author',''), 'publisher':'','year':'','isbn':'','description':html_unescape(md.get('og:description') or md.get('description') or '')[:4000], 'cover_url':md.get('og:image') or md.get('twitter:image') or '', 'store_url':final}
 def visit(x):
  if isinstance(x,list):
   for i in x:visit(i)
  if not isinstance(x,dict):return
  if '@graph' in x:visit(x['@graph'])
  types=x.get('@type',[]);types=[types] if isinstance(types,str) else types
  if any(str(t).lower() in ('book','product') for t in types):
   for a,b in [('name','title_ko'),('isbn','isbn'),('description','description'),('datePublished','year'),('image','cover_url')]:
    val=x.get(a)
    if isinstance(val,list):val=val[0] if val else ''
    if isinstance(val,dict):val=val.get('url','')
    if val and (not result.get(b) or b in ('isbn','year')):result[b]=str(val)[:4000]
   for a,b in [('author','author'),('publisher','publisher')]:
    val=x.get(a)
    if isinstance(val,list):val=', '.join(str(v.get('name','') if isinstance(v,dict) else v) for v in val)
    if isinstance(val,dict):val=val.get('name','')
    if val:result[b]=str(val)[:500]
 for item in parser.jsonld:visit(item)
 if result['cover_url']:result['cover_url']=urllib.parse.urljoin(final,result['cover_url'])
 return result

def save_card(d):
 title=str(d.get('title_ko') or d.get('title') or '').strip()[:500]
 if not title:raise ValueError('Укажите название книги')
 url=str(d.get('store_url') or d.get('url') or '').strip()
 if url and not url.startswith(('https://','http://')):raise ValueError('Неверная ссылка магазина')
 key='card:'+url if url else 'card:'+title+'|'+str(d.get('isbn',''))
 fid=hashlib.sha256(key.encode()).hexdigest()[:12]
 with LOCK:
  rows=read_db();r=next((r for r in rows if r['id']==fid),None)
  if r is None:
   r={'id':fid,'title_ko':title,'title_ru':'','url':url,'source':url or 'Ручная карточка','type':'Каталог','category':'Карточка книги','status':'Карточка создана','http':'','size':'','sha256':'','file':'','file_path':'','format':'','rights':'Не проверены','updated':now()};rows.append(r)
  for key in ('title_ko','title_ru','author','publisher','year','isbn','description','cover_url','store_url','notes'):
   if key in d:r[key]=str(d[key]).strip()[:6000]
  r['url']=url or r.get('url','');r['updated']=now();r.update(classify(r['title_ko']))
  if not r.get('title_ru'):r['title_ru']=title_ru(title)
  save_db(rows)
 log('INFO','Карточка сохранена: '+title)
 return r

def attach_path(fid,path):
 file=Path(path).expanduser().resolve()
 if not file.is_file() or file.suffix.lower() not in SUPPORTED_EXT:raise ValueError('Файл не существует или формат не поддерживается')
 with LOCK:
  rows=read_db();r=next((x for x in rows if x['id']==fid),None)
  if not r:raise ValueError('Карточка не найдена')
  r.update(file_path=str(file),format=file.suffix.lower().lstrip('.'),size=file.stat().st_size,status='Файл прикреплён',updated=now());analyze_record(r);save_db(rows)
 return r

def clean_display_title(title):
 title=str(title or '').strip()
 title=re.sub(r'^\s*(?:\((?:e-?book|pdf|epub|kindle|электронная книга|ru|en|de|fr|es|ko|zh|pl|it|pt|ja)\)\s*)+', '',title,flags=re.I)
 title=re.sub(r'^\s*(?:(?:YES24|Amazon|Google Books|Nongsaro|Book Depository|교보문고|알라딘)\s*[:|—–-]\s*)+', '',title,flags=re.I)
 title=re.sub(r'\s*[-|]\s*(?:YES24|Amazon|Google Books|Nongsaro|Book Depository)\s*$', '',title,flags=re.I)
 # Preserve actual title: do not strip the first word 'Beekeeping' without explicit confirmation.
 return title.strip() or str(title or '')

def norm_title(value):
 value=clean_display_title(value).casefold()
 value=unicodedata.normalize('NFKD',value)
 return ' '.join(re.findall(r'[^\W_]+',value,flags=re.UNICODE))

def match_catalog():
 with LOCK:
  rows=read_db(); locals_=[r for r in rows if r.get('file_path') and Path(r['file_path']).is_file()]
  results=[]
  for card in rows:
   if card.get('file_path') or card.get('type')=='Локальная книга':continue
   candidates=[]
   for file in locals_:
    if file['id']==card['id']:continue
    ci=re.sub(r'[^0-9X]','',str(card.get('isbn','')).upper());fi=re.sub(r'[^0-9X]','',str(file.get('isbn','')).upper())
    if ci and fi and len(ci) in (10,13) and ci==fi:score=1.;why='ISBN'
    else:
     a=norm_title(card.get('title_ko',''));b=norm_title(file.get('title_ko',''))
     if not a or not b:continue
     similarity=difflib.SequenceMatcher(None,a,b).ratio()
     ca=norm_title(card.get('author',''));fa=norm_title(file.get('author',''))
     score=similarity*.88+(0.12 if ca and fa and (ca==fa or ca in fa or fa in ca) else 0)
     why='Название + автор' if ca and fa else 'Название'
    if score>=.67:candidates.append({'file_id':file['id'],'file_name':Path(file['file_path']).name,'path':file['file_path'],'score':round(score,3),'reason':why})
   if candidates:results.append({'card_id':card['id'],'card_title':card.get('title_ko',''),'matches':sorted(candidates,key=lambda x:-x['score'])[:5]})
  return results

def translate_title(text):
 text=clean_display_title(text)
 if not text:return {'translation':'','provider':'none'}
 if re.search('[А-Яа-яЁё]',text):return {'translation':text,'provider':'Исходное название на русском'}
 # Public third-party service; never send book files or private metadata.
 params=urllib.parse.urlencode({'q':text[:450],'langpair':'en|ru' if re.search(r'[A-Za-z]',text) else 'autodetect|ru'})
 req=urllib.request.Request('https://api.mymemory.translated.net/get?'+params,headers={'User-Agent':'BeeLibraryCollector/0.7'})
 try:
  with urllib.request.urlopen(req,timeout=12) as resp:
   data=json.load(resp)
  val=html.unescape(str(data.get('responseData',{}).get('translatedText',''))).strip()
  if val and val.lower()!=text.lower() and not val.startswith('MYMEMORY WARNING'):
   return {'translation':val,'provider':'MyMemory (внешний сервис; результат требует проверки)'}
 except Exception as exc:log('WARN','Перевод недоступен: '+str(exc)[:160])
 return {'translation':title_ru(text) if title_ru(text)!=text else '', 'provider':'Локальный словарь / сервис недоступен'}

def cover_for(fid):
 for ext in ('.jpg','.jpeg','.png','.webp'):
  p=COVERS/(fid+ext)
  if p.is_file():return p
 return None

def save_cover_bytes(fid,data,ext):
 if not re.fullmatch('[a-f0-9]{12}',fid):raise ValueError('Неверный идентификатор')
 if len(data)>12*1024*1024 or len(data)<32:raise ValueError('Размер обложки должен быть до 12 МБ')
 if data.startswith(b'\x89PNG\r\n\x1a\n'):ext='.png'
 elif data.startswith(b'\xff\xd8\xff'):ext='.jpg'
 elif data[:4]==b'RIFF' and data[8:12]==b'WEBP':ext='.webp'
 else:raise ValueError('Допустимы только PNG, JPEG, WEBP')
 with LOCK:
  rows=read_db();r=next((x for x in rows if x['id']==fid),None)
  if not r:raise ValueError('Карточка не найдена')
  for old in COVERS.glob(fid+'.*'):old.unlink(missing_ok=True)
  target=COVERS/(fid+ext);target.write_bytes(data)
  r['cover_local']='/api/cover?id='+fid;r['updated']=now();save_db(rows)
 return {'ok':True,'url':'/api/cover?id='+fid}

# Cover URLs are image resources, not crawl seeds. Only allow known public book-image hosts.
COVER_HOSTS={'covers.openlibrary.org','books.google.com','books.google.com.cu','books.google.ru','books.google.co.uk','books.google.co.kr','books.google.de','books.google.fr','books.google.es','books.google.co.jp','books.googleusercontent.com','books.googleusercontent.com','books.google.com.br','books.google.it','books.google.ca','books.google.com.au','books.google.co.in','books.google.pl','books.google.com.mx','books.google.nl','books.google.com.tr','books.google.com.hk','books.google.com.tw','books.google.co.id','books.google.com.ua','books.google.com.ar','books.google.com.co','books.google.com.pe','books.google.com.vn','books.google.com.sg','books.google.com.my','books.google.com.ph','books.google.co.za','books.google.com.eg','books.google.ae','books.google.com.sa','books.google.com.pk','books.google.co.th','books.google.com.bd','books.google.com.ng','books.google.com.gh','books.google.co.ke','books.google.com.uy','books.google.com.py','books.google.com.bo','books.google.com.ec','books.google.com.gt','books.google.com.do','books.google.com.pa','books.google.com.cr','books.google.com.ni','books.google.com.hn','books.google.com.sv','books.google.com.pr','books.google.com.jm','books.google.com.tt','books.google.com.bz','books.google.com.bs','books.google.com.bb','books.google.com.gy','books.google.com.sr','books.google.com.fj','books.google.com.pg','books.google.com.sb','books.google.com.ws','books.google.com.to','books.google.com.vu','books.google.com.ck','books.google.com.nu','books.google.com.ki','books.google.com.nr','books.google.com.tv','books.google.com.fm','books.google.com.mh','books.google.com.pw','books.google.com.gu','books.google.com.mp','books.google.com.as','books.google.com.vi','books.google.com.um','books.google.com.cu'}
def cover_url_allowed(url):
 try:
  u=urllib.parse.urlsplit(url)
  return u.scheme=='https' and u.hostname in (COVER_HOSTS|source_hosts()) and not u.username and not u.password and u.port in (None,443) and public_hostname(u.hostname)
 except:return False
class CoverRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  if not cover_url_allowed(newurl):raise ValueError('Переадресация обложки на неизвестный сайт запрещена')
  return super().redirect_request(req,fp,code,msg,headers,newurl)
COVER_OPENER=urllib.request.build_opener(CoverRedirect)
def fetch_cover_image(url):
 if not cover_url_allowed(url):raise ValueError('Сайт обложки не разрешён. Используйте Google Books или добавьте сайт в источники.')
 candidates=[url]
 u=urllib.parse.urlsplit(url)
 if u.hostname and u.hostname.startswith('books.google.') and '/books/content' in u.path:
  volume=urllib.parse.parse_qs(u.query).get('id',[''])[0]
  if re.fullmatch(r'[a-zA-Z0-9_-]{5,32}',volume):
   candidates.append('https://books.google.com/books/content?'+urllib.parse.urlencode({'id':volume,'printsec':'frontcover','img':'1','zoom':'1','source':'gbs_api'}))
   try:
    api='https://www.googleapis.com/books/v1/volumes/'+urllib.parse.quote(volume)
    with urllib.request.urlopen(urllib.request.Request(api,headers={'User-Agent':AGENT}),timeout=12) as r:
     if r.status==200:
      info=json.load(r).get('volumeInfo',{}).get('imageLinks',{})
      for key in ('large','medium','small','thumbnail','smallThumbnail'):
       link=info.get(key,'').replace('http://','https://',1)
       if link and cover_url_allowed(link):candidates.append(link)
   except Exception as exc:log('WARN','Google Books API: '+str(exc)[:160])
 errors=[]
 for candidate in dict.fromkeys(candidates):
  try:
   req=urllib.request.Request(candidate,headers={'User-Agent':'Mozilla/5.0 BeeLibraryCollector/0.8','Accept':'image/avif,image/webp,image/png,image/jpeg,*/*','Referer':'https://books.google.com/'})
   with COVER_OPENER.open(req,timeout=22) as r:
    if not cover_url_allowed(r.url):raise ValueError('Недопустимый адрес после перенаправления')
    if int(r.headers.get('Content-Length','0'))>12*1024*1024:raise ValueError('Изображение больше 12 МБ')
    data=r.read(12*1024*1024+1)
    if len(data)>12*1024*1024:raise ValueError('Изображение больше 12 МБ')
    if data.startswith((b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff')) or (data[:4]==b'RIFF' and data[8:12]==b'WEBP'):return data
    raise ValueError('Ответ не является PNG/JPEG/WEBP (возможно, HTML или защита сайта)')
  except Exception as exc:errors.append(str(exc)[:140])
 raise ValueError('Не удалось получить обложку: '+'; '.join(errors[-3:]))
def book_file(fid):
 row=next((x for x in read_db() if x.get('id')==fid),None)
 if not row:raise ValueError('Карточка не найдена')
 path=Path(row['file_path']) if row.get('file_path') else DOWNLOADS/str(row.get('file',''))
 if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXT:raise ValueError('Файл книги недоступен')
 return path
def choose_book_folder():
 # Prefer native desktop dialogs, with tkinter as a cross-platform fallback.
 if sys.platform.startswith('linux'):
  dialogs=[('zenity',['--file-selection','--directory','--title=Выберите папку с книгами']),
           ('kdialog',['--getexistingdirectory',str(Path.home()),'Выберите папку с книгами'])]
  for name,args in dialogs:
   exe=shutil.which(name)
   if not exe:continue
   proc=subprocess.run([exe,*args],capture_output=True,text=True,timeout=180)
   if proc.returncode==0:return proc.stdout.strip()
   if proc.returncode==1:return ''
 try:
  import tkinter as tk
  from tkinter import filedialog
  window=tk.Tk();window.withdraw();window.attributes('-topmost',True)
  try:return filedialog.askdirectory(parent=window,title='Выберите папку с книгами',mustexist=True)
  finally:window.destroy()
 except Exception as exc:
  raise ValueError('Не удалось открыть окно выбора папки. Установите zenity или kdialog либо укажите путь вручную: '+str(exc))

def find_ddjvu():
 for folder in (BUNDLE_ROOT/'bin',RESOURCES/'bin'):
  for name in ('ddjvu','ddjvu.exe'):
   candidate=folder/name
   if candidate.is_file() and os.access(candidate,os.X_OK):return str(candidate)
 exe=shutil.which('ddjvu') or shutil.which('ddjvu.exe')
 if exe:return exe
 if os.name=='nt':
  roots=[os.environ.get('ProgramFiles',''),os.environ.get('ProgramFiles(x86)',''),r'C:\Program Files',r'C:\Program Files (x86)']
  for root in filter(None,roots):
   for rel in ('DjVuLibre/ddjvu.exe','DjVuLibre/bin/ddjvu.exe','DjVuLibre/DjVuLibre/ddjvu.exe','DjVuLibre/djvulibre/ddjvu.exe'):
    candidate=Path(root)/rel
    if candidate.is_file():return str(candidate)
 return None

def find_djvu_viewer():
 if os.name!='nt':return None
 candidates=[]
 for root in (os.environ.get('ProgramFiles',''),os.environ.get('ProgramFiles(x86)',''),r'C:\Program Files',r'C:\Program Files (x86)'):
  if root:
   for rel in ('WinDjView/WinDjView.exe','DjVuLibre/DjView.exe','DjVuLibre/djview.exe','DjVuLibre/bin/djview.exe','SumatraPDF/SumatraPDF.exe'):
    candidates.append(Path(root)/rel)
 return next((str(p) for p in candidates if p.is_file()),None)

def ppm_to_png(data):
 import zlib,struct
 m=re.match(rb'P6\s+(\d+)\s+(\d+)\s+255\s',data)
 if not m:raise ValueError('Неподдерживаемый формат PPM')
 w,h=map(int,m.groups());raw=data[m.end():]
 if w*h>5000000 or len(raw)!=w*h*3:raise ValueError('Некорректное изображение DJVU')
 def chunk(tag,b):return struct.pack('>I',len(b))+tag+b+struct.pack('>I',zlib.crc32(tag+b)&0xffffffff)
 scan=b''.join(b'\0'+raw[y*w*3:(y+1)*w*3] for y in range(h))
 return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(scan,6))+chunk(b'IEND',b'')
def epub_chapters(fid):
 path=book_file(fid)
 if path.suffix.lower()!='.epub':raise ValueError('Это не EPUB')
 chapters=[]
 with zipfile.ZipFile(path) as z:
  for entry in z.infolist():
   if entry.filename.lower().endswith(('.xhtml','.html','.htm')) and not entry.filename.startswith('/') and entry.file_size<3*1024*1024:
    if len(chapters)>=300:break
    raw=z.read(entry)
    if len(raw)>3*1024*1024:continue
    try:
     tree=ET.fromstring(raw)
     for elem in tree.iter():
      if elem.tag.rsplit('}',1)[-1] in ('script','style','svg'):elem.clear()
     lines=[]
     for el in tree.iter():
      tag=el.tag.rsplit('}',1)[-1]
      if tag in ('h1','h2','h3','p','li','blockquote','div'):
       txt=' '.join(''.join(el.itertext()).split())
       if txt and (tag!='div' or len(txt)<160):lines.append(txt)
     # Avoid duplicate parent/child blocks; plain-text reading is intentional.
     dedup=[]
     for line in lines:
      if not dedup or line!=dedup[-1]:dedup.append(line)
     if dedup:chapters.append({'name':entry.filename,'text':'\n\n'.join(dedup)[:250000]})
    except ET.ParseError:continue
 return chapters
def send_preview(handler,fid):
 row=next((x for x in read_db() if x.get('id')==fid),None)
 if not row:handler.respond({'error':'Карточка не найдена'},404);return
 file=Path(row['file_path']) if row.get('file_path') else DOWNLOADS/str(row.get('file',''))
 if not file.is_file() or file.suffix.lower() not in SUPPORTED_EXT:handler.respond({'error':'Локальный файл недоступен'},404);return
 ext=file.suffix.lower()
 if ext not in ('.pdf','.epub','.txt','.rtf','.jpg','.jpeg','.png'):
  handler.respond({'error':'Встроенный просмотр для этого формата недоступен. Используйте внешнюю программу чтения.'},415);return
 typ={'.pdf':'application/pdf','.epub':'application/epub+zip','.txt':'text/plain; charset=utf-8','.rtf':'application/rtf','.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png'}[ext]
 handler.send_response(200);handler.send_header('Content-Type',typ);handler.send_header('Content-Length',str(file.stat().st_size));handler.send_header('Content-Disposition',"inline; filename*=UTF-8''"+urllib.parse.quote(file.name));handler.send_header('X-Content-Type-Options','nosniff');handler.end_headers()
 with file.open('rb') as f:
  while chunk:=f.read(CHUNK):handler.wfile.write(chunk)
# Markdown/RAG conversion: one worker, isolated output, no modification of source books.
from markdown_converter import convert_book
MD_ROOT=DATA/'markdown_exports'
MD_ROOT.mkdir(parents=True,exist_ok=True)
MD_LOCK=threading.Lock()
MD_STATUS={'running':False,'message':'Ожидание','completed':0,'total':0,'current':'','error':'','percent':0,'page':0,'pages':0,'success':0,'failed':0}
from ocr_language import detect_book_language, available_languages, resolve_tesseract

def md_detect(ids):
 catalog={str(r.get('id')):r for r in read_db()}
 result=[]
 for fid in ids[:100]:
  row=catalog.get(str(fid))
  if not row or not row.get('file_path'):continue
  try:
   result.append({'id':str(fid),**detect_book_language(row['file_path'],row)})
  except Exception as exc:result.append({'id':str(fid),'language':'','confidence':0,'needs_choice':True,'reason':str(exc)})
 return {'books':result,'ocr':available_languages()}

def start_md_job(ids,language,mode="auto",save_page_images=True,review_uncertain=True):
 if not isinstance(ids,list) or not ids or len(ids)>10000:raise ValueError('Выберите книги для конвертации')
 if not re.fullmatch(r'[a-z_]{3,8}(?:\+[a-z_]{3,8}){0,5}',language):raise ValueError('Неверные языки OCR')
 with MD_LOCK:
  if MD_STATUS['running']:raise ValueError('Конвертация уже выполняется')
  MD_STATUS.update(running=True,ids=[str(x) for x in ids],message='Подготовка',completed=0,total=len(ids),current='',error='',percent=0,page=0,pages=0,success=0,failed=0)
 def worker():
  try:
   catalog={str(r.get('id')):r for r in read_db()}
   for idx,fid in enumerate(ids):
    row=catalog.get(str(fid))
    if not row or not row.get('file_path'):
     log('ERROR',f'Markdown: нет локального файла для {fid}')
    else:
     with MD_LOCK:MD_STATUS.update(current=row.get('title_ru') or row.get('title_ko') or str(fid),message=f'Книга {idx+1}/{len(ids)}')
     try:
      def update_md_page(n,total):
       with MD_LOCK:
        MD_STATUS.update(page=n,pages=total,percent=min(99,round(100*(idx+n/max(total,1))/len(ids),1)),message=f'Книга {idx+1}/{len(ids)} · страница {n}/{total}')
      result=convert_book(row['file_path'],MD_ROOT,str(fid),row.get('title_ru') or row.get('title_ko') or '',row.get('author',''),row.get('isbn',''),language,update_md_page,mode=mode,save_page_images=save_page_images,review_uncertain=review_uncertain)
      with MD_LOCK:MD_STATUS['success']+=1
      log('INFO',f'Markdown: {result["folder"]}, фрагментов {result["chunks"]}')
     except Exception as exc:
      log('ERROR',f'Markdown: {fid}: {exc}')
      with MD_LOCK:MD_STATUS.update(error=str(exc),failed=MD_STATUS['failed']+1)
    with MD_LOCK:MD_STATUS.update(completed=idx+1,percent=round(100*(idx+1)/len(ids),1),page=0,pages=0)
  finally:
   with MD_LOCK:MD_STATUS.update(running=False,message='Обработка завершена',current='',percent=100)
 threading.Thread(target=worker,daemon=True,name='markdown-converter').start()
 return {'ok':True,'total':len(ids)}

# v2.4: non-destructive grouping. Every source row remains in catalog.json.
from catalog_groups import group_catalog

def catalog_groups_payload():
 return group_catalog(read_db(), DATA/'group_review.json')

# v2.5 photo-folder converter: separate queue from mass book conversion.
PHOTO_LOCK=threading.Lock()
PHOTO_STATUS={'running':False,'percent':0,'page':0,'pages':0,'message':'Ожидание','error':'','result':None}
def start_photo_job(folder,language,save_images=True):
    if not isinstance(folder,str) or not folder.strip():raise ValueError('Укажите папку')
    if not re.fullmatch(r'[a-z_]{3,8}(?:\+[a-z_]{3,8}){0,5}',language):raise ValueError('Неверный язык OCR')
    from photo_book_converter import photo_files
    base,paths=photo_files(folder)
    # Validate OCR before accepting the background job.
    from ocr_language import resolve_tesseract
    resolve_tesseract(language)
    with PHOTO_LOCK:
        if PHOTO_STATUS['running']:raise ValueError('Фото-книга уже обрабатывается')
        PHOTO_STATUS.update(running=True,percent=0,page=0,pages=len(paths),message='Начало обработки',error='',result=None)
    def worker():
        try:
            def progress(n,total):
                with PHOTO_LOCK:PHOTO_STATUS.update(page=n,pages=total,percent=round(n/total*100,1),message=f'Страница {n}/{total}')
            from photo_book_converter import convert_photo_folder
            result=convert_photo_folder(str(base),MD_ROOT,language,progress,save_images)
            with PHOTO_LOCK:PHOTO_STATUS.update(result=result,message='PDF и Markdown готовы',percent=100)
        except Exception as exc:
            log('ERROR','Фото-книга: '+str(exc))
            with PHOTO_LOCK:PHOTO_STATUS.update(error=str(exc),message='Ошибка конвертации')
        finally:
            with PHOTO_LOCK:PHOTO_STATUS['running']=False
    threading.Thread(target=worker,daemon=True,name='photo-book-converter').start()
    return {'ok':True,'pages':len(paths)}

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def respond(self,obj,status=200):self.send_bytes(json.dumps(obj,ensure_ascii=False).encode(),status,'application/json; charset=utf-8')
 def send_bytes(self,b,status=200,typ='application/octet-stream',name=None):
  self.send_response(status);self.send_header('Content-Type',typ);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
  if name:self.send_header('Content-Disposition','attachment; filename="'+name+'"')
  self.end_headers();self.wfile.write(b)
 def do_GET(self):
  p=urllib.parse.urlsplit(self.path);q=urllib.parse.parse_qs(p.query)
  if p.path=='/':self.send_bytes((RESOURCES/'index.html').read_bytes(),typ='text/html; charset=utf-8')
  elif p.path=='/api/state':self.respond(STATE)
  elif p.path=='/api/catalog':self.respond(read_db())
  elif p.path=='/api/catalog/groups':self.respond(catalog_groups_payload())
  elif p.path=='/api/markdown/status':self.respond(MD_STATUS.copy())
  elif p.path=='/api/photo-book/status':self.respond(PHOTO_STATUS.copy())
  elif p.path=='/api/markdown/exports':
   catalog={str(r.get('id')):r for r in read_db()}
   exports=[]
   for x in MD_ROOT.iterdir():
    if not x.is_dir() or not (x/'book.md').is_file():continue
    row=catalog.get(x.name,{})
    try:meta=json.loads((x/'metadata.json').read_text(encoding='utf8'))
    except Exception:meta={}
    exports.append({'id':x.name,'ready':True,'title':meta.get('title') or row.get('title_ru') or row.get('title_ko') or x.name,'author':meta.get('author') or row.get('author',''),'pages':meta.get('pages',0),'created_at':meta.get('created_at',''),'format':meta.get('format','')})
   self.respond(sorted(exports,key=lambda x:x['created_at'],reverse=True))
  elif p.path=='/api/markdown/view':
   fid=q.get('id',[''])[0]
   if not re.fullmatch(r'[a-zA-Z0-9_\-]{1,80}',fid):self.respond({'error':'Недопустимый ID'},400);return
   f=MD_ROOT/fid/'book.md'
   if not f.is_file():self.respond({'error':'Markdown ещё не создан'},404);return
   self.send_bytes(f.read_bytes(),typ='text/plain; charset=utf-8')
  elif p.path=='/api/photo-book/pdf':
   fid=q.get('id',[''])[0]
   if not re.fullmatch(r'photos_[a-f0-9]{16}',fid):self.respond({'error':'Недопустимый ID'},400);return
   file=MD_ROOT/fid/'book.pdf'
   if not file.is_file():self.respond({'error':'PDF не найден'},404);return
   self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Length',str(file.stat().st_size));self.send_header('Content-Disposition','inline; filename="book.pdf"');self.end_headers()
   with file.open('rb') as f:
    while chunk:=f.read(1024*1024):self.wfile.write(chunk)
  elif p.path=='/api/markdown/ready':
   fid=q.get('id',[''])[0]
   if not re.fullmatch(r'[a-zA-Z0-9_\-]{1,80}',fid):self.respond({'error':'Недопустимый ID'},400);return
   self.respond({'ready':(MD_ROOT/fid/'book.md').is_file()})
  elif p.path=='/api/markdown/download':
   fid=q.get('id',[''])[0]
   if not re.fullmatch(r'[a-zA-Z0-9_\-]{1,80}',fid):self.respond({'error':'Недопустимый ID'},400);return
   folder=MD_ROOT/fid
   if not (folder/'book.md').is_file():self.respond({'error':'Результат не найден'},404);return
   buffer=io.BytesIO()
   with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
    for file in folder.rglob('*'):
     if file.is_file():archive.write(file,file.relative_to(folder))
   self.send_bytes(buffer.getvalue(),typ='application/zip',name='markdown_'+fid+'.zip')
  elif p.path=='/api/reader/notes':self.respond(read_reviews())
  elif p.path=='/api/ocr/status':
   self.respond(available_languages())
  elif p.path=='/api/backup/download':
   data=make_backup();self.send_bytes(data,typ='application/zip',name='Home_Library_backup_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.zip')
  elif p.path=='/api/reader/search':
   term=q.get('q',[''])[0].strip()[:150]
   if len(term)<2:self.respond({'books':[]})
   else:
    try:
     url='https://openlibrary.org/search.json?'+urllib.parse.urlencode({'q':term,'limit':8,'fields':'key,title,author_name,first_publish_year,isbn,cover_i'})
     req=urllib.request.Request(url,headers={'User-Agent':'HomeLibrary/1.5 (personal catalog)'})
     with urllib.request.urlopen(req,timeout=12) as response:payload=json.load(response)
     self.respond({'books':[{'title':x.get('title',''),'author':', '.join(x.get('author_name',[])[:2]),'year':x.get('first_publish_year'),'isbn':(x.get('isbn') or [''])[0],'url':'https://openlibrary.org'+x.get('key',''),'cover':('https://covers.openlibrary.org/b/id/'+str(x['cover_i'])+'-M.jpg') if x.get('cover_i') else ''} for x in payload.get('docs',[])[:8]]})
    except Exception as exc:self.respond({'books':[],'error':'Поиск временно недоступен: '+str(exc)})
  elif p.path=='/api/sources':self.respond(get_sources())
  elif p.path=='/api/sources/presets':self.respond([{'country':c,'name':n,'url':u} for c,n,u in PRESET_SOURCES])
  elif p.path=='/api/translation/batch/state':self.respond(TRANSLATE_STATE.copy())
  elif p.path=='/api/keywords':self.respond(get_terms())
  elif p.path=='/api/folders':self.respond(folders())
  elif p.path=='/api/folders/browse':
   try:
    chosen=choose_book_folder()
    self.respond({'path':chosen or '', 'cancelled':not bool(chosen)})
   except Exception as exc:self.respond({'error':'Не удалось открыть окно выбора папки: '+str(exc)},500)
  elif p.path=='/assets/logo_bee.mp4':
   file=RESOURCES/'assets'/'logo_bee.mp4'
   size=file.stat().st_size
   range_header=self.headers.get('Range','')
   m=re.fullmatch(r'bytes=(\d*)-(\d*)',range_header)
   if m and (m.group(1) or m.group(2)):
    if m.group(1):start=int(m.group(1));end=int(m.group(2)) if m.group(2) else size-1
    else:start=max(0,size-int(m.group(2)));end=size-1
    if start>=size or start>end:
     self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
    end=min(end,size-1);self.send_response(206);self.send_header('Content-Range',f'bytes {start}-{end}/{size}');length=end-start+1
   else:start=0;length=size;self.send_response(200)
   self.send_header('Content-Type','video/mp4');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(length));self.send_header('Cache-Control','private, max-age=3600');self.end_headers()
   with file.open('rb') as f:
    f.seek(start)
    while length>0:
     chunk=f.read(min(CHUNK,length))
     if not chunk:break
     try:self.wfile.write(chunk)
     except (BrokenPipeError,ConnectionResetError):break
     length-=len(chunk)
  elif p.path=='/assets/beekeeper.png':self.send_bytes((RESOURCES/'assets'/'beekeeper.png').read_bytes(),typ='image/png')
  elif p.path=='/api/logs':
   try:lines=LOG.read_text(encoding='utf8').splitlines()[-150:];self.respond([json.loads(x) for x in reversed(lines)])
   except:self.respond([])
  elif p.path=='/api/export':
   rows=read_db();fmt=q.get('format',['csv'])[0]
   if fmt=='xlsx':self.send_bytes(export_xlsx(rows),typ='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',name='nongsaro_catalog.xlsx')
   else:self.send_bytes(export_csv(rows),typ='text/csv; charset=utf-8',name='nongsaro_catalog.csv')
  elif p.path=='/api/matches':self.respond(match_catalog())
  elif p.path=='/api/duplicates':self.respond(duplicate_groups(read_db()))
  elif p.path=='/api/cover/preview':
   fid=q.get('id',[''])[0];file=PREVIEWS/(fid+'.png')
   if file.is_file():self.send_bytes(file.read_bytes(),typ='image/png')
   else:self.respond({'error':'Превью отсутствует'},404)
  elif p.path=='/api/cover/gallery':
   fid=q.get('id',[''])[0];self.respond([{'type':t,'url':'/api/cover'+('' if t=='manual' else '/preview')+'?id='+fid} for t,pth in cover_images(fid)])
  elif p.path=='/api/epub/spine':
   try:self.respond(epub_spine(q.get('id',[''])[0]))
   except Exception as exc:self.respond({'error':str(exc)},400)
  elif p.path=='/api/epub/resource':
   try:
    data,typ=epub_resource(q.get('id',[''])[0],q.get('path',[''])[0]);self.send_bytes(data,typ=typ)
   except Exception as exc:self.respond({'error':str(exc)},400)
  elif p.path=='/api/cover':
   fid=q.get('id',[''])[0];file=cover_for(fid)
   if not file:self.respond({'error':'Обложка отсутствует'},404)
   else:self.send_bytes(file.read_bytes(),typ=mimetypes.guess_type(file.name)[0] or 'image/jpeg')
  elif p.path=='/api/preview':send_preview(self,q.get('id',[''])[0])
  elif p.path=='/api/epub/chapters':
   try:self.respond(epub_chapters(q.get('id',[''])[0]))
   except Exception as exc:self.respond({'error':str(exc)},400)
  elif p.path=='/api/djvu/page':
   try:
    file=book_file(q.get('id',[''])[0]);page=int(q.get('page',['1'])[0])
    if file.suffix.lower() not in ('.djvu','.djv') or not 1<=page<=10000:raise ValueError('Неверная страница DJVU')
    exe=find_ddjvu()
    if not exe:raise ValueError('Не найден ddjvu.exe. Установите DjVuLibre для встроенного просмотра либо откройте книгу во внешней программе.')
    # Render to a temporary file rather than buffering enormous decompressed PPM in memory.
    # ddjvu supports -format=png in common DjVuLibre builds; fallback to PPM for older builds.
    with tempfile.TemporaryDirectory(prefix='home_library_djvu_') as td:
     output=Path(td)/'page.png'
     cmd=[exe,'-format=png','-page='+str(page),'-size=900x1200',str(file),str(output)]
     result=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=50,creationflags=(subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
     if result.returncode or not output.is_file():
      output=Path(td)/'page.ppm'
      cmd=[exe,'-format=ppm','-page='+str(page),'-size=900x1200',str(file),str(output)]
      result=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=50,creationflags=(subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0))
      if result.returncode or not output.is_file():raise ValueError('Ошибка DjVuLibre: '+result.stderr.decode(errors='replace')[:350])
      if output.stat().st_size>22*1024*1024:raise ValueError('Страница слишком большая для предпросмотра')
      data=ppm_to_png(output.read_bytes())
     else:
      if output.stat().st_size>15*1024*1024:raise ValueError('Страница слишком большая для предпросмотра')
      data=output.read_bytes()
    self.send_bytes(data,typ='image/png')
   except Exception as exc:self.respond({'error':str(exc)},400)
  elif p.path=='/api/local_file':
   fid=q.get('id',[''])[0]
   r=next((x for x in read_db() if x.get('id')==fid),None)
   file=Path(r.get('file_path','')) if r and r.get('file_path') else None
   if not file or not file.is_file() or file.suffix.lower() not in SUPPORTED_EXT:self.respond({'error':'Файл недоступен'},404);return
   self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(file.stat().st_size));self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urllib.parse.quote(file.name));self.send_header('X-Content-Type-Options','nosniff');self.end_headers()
   with file.open('rb') as stream:
    while chunk:=stream.read(CHUNK):self.wfile.write(chunk)
  elif p.path=='/api/file':
   name=q.get('name',[''])[0]
   if not re.fullmatch(r'[a-f0-9]{12}\.pdf',name) or not (DOWNLOADS/name).is_file():self.respond({'error':'Файл не найден'},404)
   else:
    file=DOWNLOADS/name
    self.send_response(200);self.send_header('Content-Type','application/pdf');self.send_header('Content-Length',str(file.stat().st_size));self.send_header('Content-Disposition','attachment; filename="'+name+'"');self.end_headers()
    with file.open('rb') as f:
     while chunk:=f.read(CHUNK):self.wfile.write(chunk)
  else:self.respond({'error':'Не найдено'},404)
 def do_POST(self):
  try:
   if urllib.parse.urlsplit(self.path).path=='/api/backup/restore-file':
    size=int(self.headers.get('Content-Length','0'))
    if size<1 or size>4*1024**3:raise ValueError('Размер Backup должен быть от 1 байта до 4 ГиБ')
    with tempfile.TemporaryDirectory(prefix='homelibrary-upload-') as tempdir:
     archive=Path(tempdir)/'backup.zip'
     remaining=size
     with archive.open('wb') as output:
      while remaining:
       chunk=self.rfile.read(min(1024*1024,remaining))
       if not chunk:raise ValueError('Загрузка архива прервана')
       output.write(chunk);remaining-=len(chunk)
     self.respond(restore_backup(archive))
    return
   if urllib.parse.urlsplit(self.path).path=='/api/djvu/open':
    size=int(self.headers.get('Content-Length','0'))
    if size<1 or size>4096:raise ValueError('Неверный запрос')
    payload=json.loads(self.rfile.read(size))
    file=book_file(str(payload.get('id','')))
    if file.suffix.lower() not in ('.djvu','.djv'):raise ValueError('Это не DJVU')
    if os.name=='nt':
     viewer=find_djvu_viewer()
     if viewer:
      subprocess.Popen([viewer,str(file)],creationflags=subprocess.CREATE_NO_WINDOW)
      self.respond({'ok':True,'message':'Книга открывается в '+Path(viewer).name});return
     try:os.startfile(str(file))
     except OSError as exc:raise ValueError('Windows не знает программу для .djvu. Установите WinDjView, SumatraPDF или DjVuLibre: '+str(exc))
    elif sys.platform.startswith('linux'):
     opener=shutil.which('xdg-open') or shutil.which('gio')
     if not opener:raise ValueError('Для внешнего открытия установите xdg-utils или GIO и программу просмотра DJVU')
     cmd=[opener,'open',str(file)] if Path(opener).name=='gio' else [opener,str(file)]
     subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    elif sys.platform=='darwin':
     subprocess.Popen(['open',str(file)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    else:raise ValueError('Неподдерживаемая платформа')
    self.respond({'ok':True,'message':'Книга передана внешнему приложению'});return
   if urllib.parse.urlsplit(self.path).path=='/api/cover/upload':
    query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
    size=int(self.headers.get('Content-Length','0'))
    if size<32 or size>12*1024*1024:raise ValueError('Обложка до 12 МБ')
    self.respond(save_cover_bytes(query.get('id',[''])[0],self.rfile.read(size),''));return
   if urllib.parse.urlsplit(self.path).path=='/api/card/upload':
    query=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
    fid=query.get('id',[''])[0];filename=Path(query.get('filename',[''])[0]).name
    ext=Path(filename).suffix.lower();size=int(self.headers.get('Content-Length','0'))
    if ext not in SUPPORTED_EXT or size<1 or size>2*1024**3:raise ValueError('Недопустимый формат или размер (до 2 ГиБ)')
    with LOCK:
     rows=read_db()
     if not any(x['id']==fid for x in rows):raise ValueError('Карточка не найдена')
    target=ATTACHMENTS/(fid+ext);part=ATTACHMENTS/(fid+ext+'.part')
    remaining=size;sha=hashlib.sha256()
    try:
     with part.open('wb') as out:
      while remaining:
       chunk=self.rfile.read(min(CHUNK,remaining))
       if not chunk:raise ValueError('Передача прервана')
       out.write(chunk);sha.update(chunk);remaining-=len(chunk)
     part.replace(target)
    except:
     part.unlink(missing_ok=True);raise
    with LOCK:
     rows=read_db();r=next(x for x in rows if x['id']==fid)
     r.update(file_path=str(target),format=ext[1:],size=size,sha256=sha.hexdigest(),status='Файл прикреплён',updated=now());
     if make_preview(fid,target):r['cover_preview']='/api/cover/preview?id='+fid
     analyze_record(r,calculate_hash=False)
     save_db(rows)
    log('INFO',f'Файл {filename} прикреплён к карточке {fid}')
    self.respond({'ok':True});return
   n=int(self.headers.get('Content-Length','0'))
   if n>500000:self.respond({'error':'Слишком большой запрос'},413);return
   d=json.loads(self.rfile.read(n) or b'{}');path=self.path
   if path=='/api/markdown/detect':
    self.respond(md_detect(d.get('ids',[])))
   elif path=='/api/photo-book/start':
    self.respond(start_photo_job(str(d.get('folder','')),str(d.get('language','rus+eng')),bool(d.get('save_images',True))))
   elif path=='/api/markdown/start':
    self.respond(start_md_job(d.get('ids',[]),str(d.get('language','rus+eng')),str(d.get('mode','auto')),bool(d.get('save_page_images',True)),bool(d.get('review_uncertain',True))))
   elif path=='/api/backup/restore':
    encoded=d.get('archive_base64','')
    import base64
    if not isinstance(encoded,str) or len(encoded)>400*1024*1024:raise ValueError('Недопустимый архив')
    self.respond(restore_backup(base64.b64decode(encoded,validate=True)))
   elif path=='/api/reader/notes/save':
    fid=str(d.get('id',''))
    if not any(x.get('id')==fid for x in read_db()):raise ValueError('Книга не найдена')
    status=str(d.get('status',''))
    if status not in ('','reading','later'):raise ValueError('Недопустимый статус')
    rating=int(d.get('rating',0))
    if not 0<=rating<=5:raise ValueError('Оценка должна быть от 0 до 5')
    comment=str(d.get('comment',''))[:10000]
    with LOCK:
     notes=read_reviews();notes[fid]={'status':status,'rating':rating,'comment':comment,'updated':now()};write_reviews(notes)
    self.respond({'ok':True})
   elif path=='/api/metadata/analyze':
    ids=d.get('ids');self.respond(analyze_library(set(map(str,ids)) if isinstance(ids,list) else None))
   elif path=='/api/matches/apply':
    fid=str(d.get('card_id',''));file_id=str(d.get('file_id',''))
    with LOCK:
     rows=read_db();card=next((x for x in rows if x['id']==fid),None);file=next((x for x in rows if x['id']==file_id),None)
     if not card or not file or not file.get('file_path'):raise ValueError('Не найдена карточка или локальный файл')
     if not Path(file['file_path']).is_file():raise ValueError('Локальный файл больше не существует')
     card.update(file_path=file['file_path'],format=file.get('format',''),size=file.get('size',0),status='Файл сопоставлен',updated=now());
     if make_preview(fid,Path(file['file_path'])):card['cover_preview']='/api/cover/preview?id='+fid
     save_db(rows)
    self.respond({'ok':True})
   elif path=='/api/cover/isbn':
    fid=str(d.get('id',''));isbn=str(d.get('isbn','')).strip()
    self.respond(save_cover_bytes(fid,isbn_cover(isbn),''))
   elif path=='/api/cover/fetch':
    fid=str(d.get('id',''));url=str(d.get('url','')).strip()
    data=fetch_cover_image(url)
    self.respond(save_cover_bytes(fid,data,''))
   elif path=='/api/title/translate':self.respond(translate_title(str(d.get('text',''))))
   elif path=='/api/isbn/lookup':self.respond(isbn_candidates(str(d.get('isbn',''))))
   elif path=='/api/isbn/apply':self.respond(apply_isbn_candidate(str(d.get('id','')),d.get('fields',{})))
   elif path=='/api/translation/suggest/apply':
    with LOCK:
     rows=read_db();r=next((x for x in rows if x['id']==d.get('id')),None)
     if not r:raise ValueError('Книга не найдена')
     if not r.get('title_ru_suggested'):raise ValueError('Нет предложенного перевода')
     r['title_ru']=r.pop('title_ru_suggested');save_db(rows)
    self.respond({'ok':True})
   elif path=='/api/translation/batch/start':
    if TRANSLATE_STATE['running']:raise ValueError('Перевод уже выполняется')
    ids=d.get('ids') if isinstance(d.get('ids'),list) else None
    threading.Thread(target=batch_translate_worker,args=(ids,),daemon=True).start()
    self.respond({'ok':True})
   elif path=='/api/folders/add':
    folder=str(d.get('path','')).strip();recursive=bool(d.get('recursive',True))
    if not Path(folder).expanduser().is_dir():raise ValueError('Папка не существует')
    with LOCK:
     items=folders()
     if not any(x['path']==str(Path(folder).expanduser().resolve()) for x in items):items.append({'path':str(Path(folder).expanduser().resolve()),'recursive':recursive});save_folders(items)
    result=import_folder(folder,recursive)
    if d.get('translate') and not TRANSLATE_STATE['running']:threading.Thread(target=batch_translate_worker,daemon=True).start()
    self.respond(result)
   elif path=='/api/folders/scan':
    folder=str(d.get('path',''));item=next((x for x in folders() if x['path']==folder),None)
    if not item:raise ValueError('Папка не зарегистрирована')
    result=import_folder(folder,item['recursive'])
    if d.get('translate') and not TRANSLATE_STATE['running']:threading.Thread(target=batch_translate_worker,daemon=True).start()
    self.respond(result)
   elif path=='/api/folders/remove':
    folder=str(d.get('path',''))
    with LOCK:save_folders([x for x in folders() if x['path']!=folder])
    self.respond({'ok':True})
   elif path=='/api/card/fetch':
    url=str(d.get('url','')).strip()
    if not allowed_url(url):raise ValueError('Сначала добавьте HTTPS-магазин в источники')
    self.respond(metadata_from_url(url))
   elif path=='/api/card/save':self.respond(save_card(d))
   elif path=='/api/card/attach':self.respond(attach_path(str(d.get('id','')),str(d.get('path',''))))
   elif path=='/api/sources/presets/add':
    urls=d.get('urls',[])
    if not isinstance(urls,list) or len(urls)>100:raise ValueError('Некорректный выбор источников')
    with LOCK:
     items=get_sources();existing={x.get('url') for x in items}
     for country,name,url in PRESET_SOURCES:
      if url in urls and url not in existing:
       items.append({'name':name+' · '+country,'url':url,'enabled':False,'kind':'store','country':country});existing.add(url)
     save_sources(items)
    self.respond({'ok':True,'count':len(items)})
   elif path=='/api/sources/add':
    url=str(d.get('url','')).strip();name=str(d.get('name','')).strip()[:100]
    if not name or not url.startswith('https://'):raise ValueError('Нужно название и HTTPS-адрес')
    h=urllib.parse.urlsplit(url).hostname
    if not h or not public_hostname(h) or urllib.parse.urlsplit(url).username:raise ValueError('Адрес должен быть публичным HTTPS-сайтом')
    with LOCK:
     sources=get_sources()
     if any(x['url']==url for x in sources):raise ValueError('Источник уже добавлен')
     sources.append({'name':name,'url':url,'enabled':True,'kind':'custom'});save_sources(sources)
    self.respond({'ok':True})
   elif path=='/api/sources/toggle':
    with LOCK:
     sources=get_sources();idx=int(d.get('index',-1))
     if idx<0 or idx>=len(sources):raise ValueError('Источник не найден')
     sources[idx]['enabled']=bool(d.get('enabled'));save_sources(sources)
    self.respond({'ok':True})
   elif path=='/api/keywords/save':
    lang=str(d.get('language','')).lower()
    words=d.get('words',[])
    if not re.fullmatch('[a-z]{2}',lang) or not isinstance(words,list) or len(words)>150 or any(not isinstance(x,str) or len(x)>80 for x in words):raise ValueError('Неверный список терминов')
    with LOCK:
     terms=get_terms();terms[lang]=[w.strip() for w in words if w.strip()];TERMS.write_text(json.dumps(terms,ensure_ascii=False,indent=2),encoding='utf8')
    self.respond({'ok':True})
   elif path=='/api/reclassify':
    with LOCK:
     rows=read_db()
     for r in rows:r.update(classify(r.get('title_ko','')))
     save_db(rows)
    self.respond({'ok':True})
   elif path=='/api/scan':
    if STATE['running']:self.respond({'error':'Сбор уже выполняется'},409);return
    start=d.get('url',BASE)
    if not allowed_url(start):raise ValueError('Добавьте и включите источник перед сканированием')
    pages=max(1,min(60,int(d.get('pages',12))))
    threading.Thread(target=scan_mask,args=(start,pages,'*.pdf'),daemon=True).start();self.respond({'ok':True})
   elif path=='/api/scan_mask':
    if STATE['running']:self.respond({'error':'Уже выполняется операция'},409);return
    start=d.get('url',BASE);mask=str(d.get('mask','*.pdf')).strip()
    if not allowed_url(start):raise ValueError('Добавьте и включите источник перед сканированием')
    if not re.fullmatch(r'[a-zA-Z0-9*?._-]{1,80}',mask):raise ValueError('Некорректная маска файла')
    pages=max(1,min(300,int(d.get('pages',30))))
    threading.Thread(target=scan_mask,args=(start,pages,mask),daemon=True).start();self.respond({'ok':True})
   elif path=='/api/bulk':
    if STATE['running']:self.respond({'error':'Уже выполняется операция'},409);return
    threading.Thread(target=bulk_download,daemon=True).start();self.respond({'ok':True})
   elif path=='/api/rights_bulk':
    raise PermissionError('Массовое подтверждение прав отключено. Проверяйте каждую книгу отдельно')
    with LOCK:
     rows=read_db()
     for row in rows:row['rights']='Разрешено'
     save_db(rows)
    self.respond({'ok':True})
   elif path=='/api/add':
    url=d.get('url','').strip()
    if not allowed_url(url):raise ValueError('Разрешены только HTTPS-ссылки Nongsaro')
    if not pdf_candidate(url):raise ValueError('Требуется ссылка PDF или штатный адрес скачивания Nongsaro')
    with LOCK:
     rows=read_db();upsert(rows,url,d.get('title','').strip(),'Вручную');save_db(rows)
    self.respond({'ok':True})
   elif path=='/api/rights':
    with LOCK:
     rows=read_db();row=next((r for r in rows if r['id']==d.get('id')),None)
     if not row:raise ValueError('Запись не найдена')
     row['rights']='Разрешено' if d.get('allowed') is True else 'Не проверены';save_db(rows)
    self.respond({'ok':True})
   elif path in ('/api/check','/api/download'):
    row=check_one(d['id']) if path.endswith('check') else download_one(d['id']);self.respond(row)
   elif path=='/api/translate':
    with LOCK:
     rows=read_db();row=next((r for r in rows if r['id']==d.get('id')),None)
     if not row:raise ValueError('Запись не найдена')
     row['title_ru']=str(d.get('title_ru','')).strip()[:300];save_db(rows)
    self.respond({'ok':True})
   else:self.respond({'error':'Не найдено'},404)
  except Exception as e:self.respond({'error':str(e)},400)
if __name__=='__main__':
 import webbrowser
 HOST='127.0.0.1';PORT=int(os.environ.get('PORT','8765'))
 print(f'Домашняя библиотека v2.5: http://{HOST}:{PORT}')
 threading.Timer(0.8,lambda:webbrowser.open(f'http://{HOST}:{PORT}')).start()
 ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
