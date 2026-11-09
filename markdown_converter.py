"""Offline book -> Markdown + page-grounded RAG JSON. Optional external OCR: tesseract."""
import io, json, re, shutil, subprocess, tempfile, zipfile, html
from pathlib import Path
from ocr_language import resolve_tesseract
from html.parser import HTMLParser
from datetime import datetime, timezone

class TextHTML(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]
    def handle_starttag(self, tag, attrs):
        if tag in ('p','div','h1','h2','h3','li','br','tr'):self.parts.append('\n')
    def handle_data(self,data):self.parts.append(data)
    def text(self):return re.sub(r'\n{3,}','\n\n',''.join(self.parts)).strip()

def ocr_image(image, lang='rus+eng'):
    exe, tessdata = resolve_tesseract(lang)
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'page.png';path.write_bytes(image)
        proc=subprocess.run([exe,str(path),'stdout','-l',lang,'--tessdata-dir',str(tessdata)],capture_output=True,timeout=150)
        if proc.returncode:raise RuntimeError(proc.stderr.decode('utf-8','replace')[-400:])
        return proc.stdout.decode('utf-8','replace').strip()

def safe_name(s):return re.sub(r'[^\w.\-]+','_',s,flags=re.UNICODE)[:80] or 'book'

def _pdf(path, assets, ocr_lang, progress, save_page_images=True):
    import fitz
    doc=fitz.open(path)
    pages=[]
    for n,page in enumerate(doc):
        # OCR every page, even if the PDF already contains selectable text.
        pix=page.get_pixmap(matrix=fitz.Matrix(1.6,1.6),alpha=False)
        if pix.width*pix.height>18_000_000:pix=page.get_pixmap(matrix=fitz.Matrix(1,1),alpha=False)
        page_png=pix.tobytes('png')
        page_scan=None
        if save_page_images:
            page_scan=f'page_{n+1:04d}.png'
            (assets/page_scan).write_bytes(page_png)
        text=ocr_image(page_png,ocr_lang)
        native=page.get_text('text',sort=True).strip()
        # OCR is always executed; a reliable existing text layer wins when OCR drops text.
        native_words=len(re.findall(r'\w+',native))
        ocr_words=len(re.findall(r'\w+',text))
        if native_words>=25 and (ocr_words<max(12,native_words*.7) or len(text)<len(native)*.65):
            text=native

        tables=[]
        try:
            for t in page.find_tables().tables:
                matrix=t.extract()
                if matrix:tables.append(matrix)
        except Exception:pass
        images=[]
        for k,info in enumerate(page.get_images(full=True)[:25]):
            try:
                obj=doc.extract_image(info[0]);ext=obj.get('ext','png')
                if ext.lower() not in ('png','jpg','jpeg','webp'):continue
                # Full-page raster scans are OCR sources, not standalone illustrations.
                placements=page.get_image_rects(info[0])
                if placements and max((r.get_area()/max(page.rect.get_area(),1) for r in placements),default=0)>.68:
                    continue
                name=f'p{n+1:04d}_{k+1:02d}.{ext}';(assets/name).write_bytes(obj['image']);images.append(name)
            except Exception:continue
        pages.append({'page':n+1,'text':text,'tables':tables,'images':images,'page_scan':page_scan,'native_words':native_words,'ocr_words':ocr_words})
        progress(n+1,len(doc))
    doc.close();return pages

def _djvu(path, assets, ocr_lang, progress, save_page_images=True):
    if not shutil.which('ddjvu') or not shutil.which('djvused'):
        raise RuntimeError('Для DJVU установите DjVuLibre (ddjvu и djvused)')
    r=subprocess.run(['djvused',str(path),'-e','n'],capture_output=True,text=True,timeout=20,check=True)
    count=int(r.stdout.strip());pages=[]
    for n in range(1,count+1):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'page.png'
            subprocess.run(['ddjvu','-format=png',f'-page={n}','-scale=120',str(path),str(out)],check=True,capture_output=True,timeout=150)
            data=out.read_bytes();text=ocr_image(data,ocr_lang)
        # Retain the original scan in assets for verification, but do not inject
        # it as an illustration into book.md on every page.
        name=f'page_{n:04d}.png' if save_page_images else None
        if name:(assets/name).write_bytes(data)
        pages.append({'page':n,'text':text,'tables':[],'images':[],'page_scan':name});progress(n,count)
    return pages

def _epub(path,assets,ocr_lang,progress):
    import xml.etree.ElementTree as ET
    pages=[]
    with zipfile.ZipFile(path) as z:
        container=ET.fromstring(z.read('META-INF/container.xml'))
        rootfile=next(e.attrib['full-path'] for e in container.iter() if e.tag.endswith('rootfile'))
        base=Path(rootfile).parent
        opf=ET.fromstring(z.read(rootfile))
        manifest={e.attrib['id']:e.attrib['href'] for e in opf.iter() if e.tag.endswith('item')}
        spine=[e.attrib['idref'] for e in opf.iter() if e.tag.endswith('itemref')]
        for idx,ref in enumerate(spine):
            href=manifest.get(ref,'');entry=str(base/href).replace('\\','/')
            try:raw=z.read(entry).decode('utf-8','replace')
            except KeyError:continue
            parser=TextHTML();parser.feed(raw)
            text=parser.text()
            # OCR every raster illustration/page image found in EPUB chapters.
            images=[];ocr_parts=[]
            for src in re.findall(r'<(?:img|image)\b[^>]*(?:src|href|xlink:href)=["\']([^"\']+)',raw,re.I)[:30]:
                from posixpath import normpath,dirname,join
                target=normpath(join(dirname(entry),html.unescape(src).split('#')[0]))
                if target not in z.namelist():continue
                ext=Path(target).suffix.lower()
                if ext not in ('.png','.jpg','.jpeg','.webp','.tif','.tiff'):continue
                data=z.read(target);name=f'chapter_{idx+1:04d}_{len(images)+1:02d}{ext}'
                (assets/name).write_bytes(data);images.append(name)
                ocr_parts.append(ocr_image(data,ocr_lang))
            pages.append({'page':idx+1,'text':text+'\n'+'\n'.join(ocr_parts),'tables':[],'images':images})
            progress(idx+1,len(spine))
    return pages

def _table_md(rows):
    if not rows:return ''
    cells=lambda row:'| '+' | '.join(str(x or '').replace('|','\\|').replace('\n',' ') for x in row)+' |'
    return '\n'.join([cells(rows[0]),cells(['---']*len(rows[0]))]+[cells(r) for r in rows[1:]])

def convert_book(file_path, output_root, book_id, title='', author='', isbn='', ocr_lang='rus+eng', progress=lambda n,total:None, mode='auto', save_page_images=True, review_uncertain=True):
    path=Path(file_path).resolve();root=Path(output_root).resolve()
    if not path.is_file():raise FileNotFoundError(path)
    if path.suffix.lower() not in ('.pdf','.djvu','.djv','.epub'):
        raise ValueError('В первой версии конвертера поддерживаются PDF, DJVU и EPUB')
    target=root/safe_name(str(book_id));target.mkdir(parents=True,exist_ok=True)
    assets=target/'images';assets.mkdir(exist_ok=True)
    ext=path.suffix.lower()
    pages=(_pdf(path,assets,ocr_lang,progress,save_page_images) if ext=='.pdf' else
           _djvu(path,assets,ocr_lang,progress,save_page_images) if ext in ('.djvu','.djv') else
           _epub(path,assets,ocr_lang,progress))
    # Lightweight document-type heuristic; review flags are not OCR confidence scores.
    if mode not in ('auto','book','magazine','visual'):raise ValueError('Неверный режим документа')
    if mode=='auto':
        short=sum(len(x.get('text','').strip())<280 for x in pages)
        image_rich=sum(len(x.get('images',[]))>=2 for x in pages)
        mode='magazine' if pages and (image_rich/len(pages)>.25 or short/len(pages)>.50) else 'book'
    review=[]
    if review_uncertain:
        for item in pages:
            txt=item.get('text','').strip()
            words=re.findall(r'\w+',txt)
            bad=(len(words)<12 or (len(words)>10 and sum(len(w)==1 for w in words)/len(words)>.38))
            if bad:review.append({'page':item['page'],'reason':'Мало текста или много одиночных символов','page_scan':item.get('page_scan'),'text_excerpt':txt[:500]})
    (target/'review_pages.json').write_text(json.dumps(review,ensure_ascii=False,indent=2),encoding='utf-8')
    chapters_dir=target/'chapters';chapters_dir.mkdir(exist_ok=True)
    sections=[];chunks=[];chapter=0
    for item in pages:
        text=item['text'];match=re.search(r'(?im)^\s*(?:глава|chapter|раздел|section)\s+[\dIVXLC]+[^\n]*',text)
        if match or chapter==0:chapter+=1
        heading=match.group(0).strip() if match else f'Часть {chapter}'
        content=f'<!-- source-page: {item["page"]} -->\n\n'+text+'\n'
        for t in item['tables']:content+='\n'+_table_md(t)+'\n'
        for img in item['images']:content+=f'\n![Иллюстрация](../images/{img})\n'
        # A separate Markdown file for each chapter/section, with page attribution.
        if not sections or sections[-1]['chapter']!=chapter:sections.append({'chapter':chapter,'heading':heading,'parts':[]})
        sections[-1]['parts'].append(content)
        for offset in range(0,len(text),1400):
            chunk=text[offset:offset+1400].strip()
            if chunk:chunks.append({'id':f'{book_id}:p{item["page"]}:c{offset//1400}', 'book_id':str(book_id),'title':title or path.stem,'author':author,'isbn':isbn,'source_file':path.name,'source_page':item['page'],'chapter':chapter,'text':chunk,'images':item['images'],'page_scan':item.get('page_scan'),'tables':item['tables']})
    mdparts=[f'# {title or path.stem}\n',f'Автор: {author}\n' if author else '',f'ISBN: {isbn}\n' if isbn else '']
    for s in sections:
        body=f'## {s["heading"]}\n\n'+ '\n'.join(s['parts'])
        name=f'{s["chapter"]:03d}_{safe_name(s["heading"])}.md'
        (chapters_dir/name).write_text(body,encoding='utf-8')
        mdparts.append(body.replace('../images/','images/'))
    (target/'book.md').write_text('\n'.join(mdparts),encoding='utf-8')
    (target/'rag_chunks.json').write_text(json.dumps(chunks,ensure_ascii=False,indent=2),encoding='utf-8')
    meta={'book_id':str(book_id),'title':title or path.stem,'author':author,'isbn':isbn,'source_file':str(path),'format':ext,'pages':len(pages),'chunks':len(chunks),'chapters':len(sections),'ocr':'all_pages','ocr_languages':ocr_lang,'document_mode':mode,'review_pages':len(review),'created_at':datetime.now(timezone.utc).isoformat()}
    (target/'metadata.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'ok':True,'book_id':str(book_id),'pages':len(pages),'chapters':len(sections),'chunks':len(chunks),'folder':str(target)}
