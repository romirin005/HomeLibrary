"""Conservative offline language preflight and bundled Tesseract locator."""
import os,sys,re,shutil,subprocess
from pathlib import Path
BASE=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
RESOURCE=Path(getattr(sys,'_MEIPASS',BASE))
def locate():
 candidates=[BASE/'ocr'/'tesseract.exe',BASE/'ocr'/'bin'/'tesseract',RESOURCE/'ocr'/'tesseract.exe',RESOURCE/'ocr'/'bin'/'tesseract']
 if os.name=='nt':candidates.extend([Path(os.environ.get('ProgramFiles',r'C:\Program Files'))/'Tesseract-OCR'/'tesseract.exe',Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')])
 exe=next((str(x) for x in candidates if x.is_file()),None) or shutil.which('tesseract')
 data_candidates=[BASE/'ocr'/'tessdata',RESOURCE/'ocr'/'tessdata']
 if os.environ.get('TESSDATA_PREFIX'):data_candidates.insert(0,Path(os.environ['TESSDATA_PREFIX']))
 if exe:data_candidates.append(Path(exe).parent/'tessdata')
 data=next((x for x in data_candidates if x.is_dir() and any(x.glob('*.traineddata'))),None)
 if exe and not data:
  try:
   proc=subprocess.run([exe,'--list-langs'],capture_output=True,text=True,timeout=10)
   match=re.search(r'List of available languages in "?([^"\r\n]+)',proc.stderr+proc.stdout)
   if match:
    folder=Path(match.group(1).rstrip('" :'))
    if folder.is_dir():data=folder
  except Exception:pass
 return exe,data
def available_languages():
 exe,data=locate();names=sorted(p.stem for p in data.glob('*.traineddata')) if data else []
 return {'installed':bool(exe),'executable':exe or '', 'tessdata':str(data) if data else '', 'languages':names}
def resolve_tesseract(lang):
 exe,data=locate()
 if not exe or not data:raise RuntimeError('Tesseract OCR или каталог tessdata не найден')
 missing=[x for x in lang.split('+') if not (data/(x+'.traineddata')).is_file()]
 if missing:raise RuntimeError('Отсутствуют OCR-модели: '+', '.join(missing))
 return exe,data
def detect_book_language(filename,row):
 path=Path(filename);installed=set(available_languages()['languages']);text=''
 try:
  if path.suffix.lower()=='.pdf':
   import fitz
   with fitz.open(path) as doc:text=' '.join(doc[i].get_text()[:2500] for i in range(min(3,len(doc))))
  elif path.suffix.lower()=='.epub':
   import zipfile
   with zipfile.ZipFile(path) as z:
    for name in z.namelist():
     if name.lower().endswith(('.xhtml','.html','.htm')):
      text+=re.sub('<[^>]+>',' ',z.read(name).decode('utf8','replace'))[:2000]
      if len(text)>4000:break
 except Exception:pass
 text=text or str(row.get('title_ko',''))
 counts={'rus':0,'kor':0,'jpn':0,'chi_sim':0,'ara':0,'hin':0,'eng':0}
 for c in text:
  n=ord(c)
  if 0x0400<=n<=0x052f:counts['rus']+=1
  elif 0xac00<=n<=0xd7af:counts['kor']+=1
  elif 0x3040<=n<=0x30ff:counts['jpn']+=1
  elif 0x4e00<=n<=0x9fff:counts['chi_sim']+=1
  elif 0x0600<=n<=0x06ff:counts['ara']+=1
  elif 0x0900<=n<=0x097f:counts['hin']+=1
  elif c.isalpha() and n<0x0250:counts['eng']+=1
 key=max(counts,key=counts.get);total=sum(counts.values());score=counts[key]/total if total else 0
 if key=='rus' and sum(text.lower().count(c) for c in 'іїєґ')>=2:key='ukr'
 if key=='eng':score=min(score,0.4) # Latin alphabet cannot distinguish 13 languages
 if key=='chi_sim':score=min(score,0.7) # simplified/traditional/Japanese ambiguity
 if total<30:score=min(score,0.35)
 if key not in installed:score=0
 return {'language':key if key in installed else '', 'confidence':round(score,2),'needs_choice':score<0.85,'reason':'Низкая уверенность' if score<0.85 else 'Определено по письменности'}
