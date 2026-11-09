"""Photo-folder -> readable PDF, OCR Markdown, RAG JSON. No modification of originals."""
import json, re, shutil, subprocess, tempfile, hashlib
from pathlib import Path
from datetime import datetime, timezone
from PIL import Image, ImageOps
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from ocr_language import resolve_tesseract

EXT={'.jpg','.jpeg','.png','.webp','.tif','.tiff','.bmp'}
def natural_key(p):return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)',p.name)]
def photo_files(folder):
    base=Path(folder).expanduser().resolve()
    if not base.is_dir():raise ValueError('Папка с фотографиями не существует')
    paths=sorted((p for p in base.iterdir() if p.is_file() and p.suffix.lower() in EXT),key=natural_key)
    if not paths:raise ValueError('В папке не найдены фотографии JPG/PNG/WEBP/TIFF/BMP')
    if len(paths)>2000:raise ValueError('Максимум 2000 страниц за один запуск')
    return base,paths

def convert_photo_folder(folder,output_root,lang='rus+eng',progress=lambda n,t:None,save_images=True):
    base,photos=photo_files(folder)
    exe,tessdata=resolve_tesseract(lang)
    job_id=hashlib.sha256(str(base).encode()).hexdigest()[:16]
    dest=Path(output_root)/('photos_'+job_id)
    dest.mkdir(parents=True,exist_ok=True)
    image_dir=dest/'images';image_dir.mkdir(exist_ok=True)
    pdf=dest/'book.pdf';md=dest/'book.md'
    # A temporary PDF is atomically promoted only after successful conversion.
    tmp_pdf=dest/'book.partial.pdf'
    doc=canvas.Canvas(str(tmp_pdf),pageCompression=1)
    blocks=[];chunks=[];reviews=[]
    for i,p in enumerate(photos,1):
        with Image.open(p) as original:
            img=ImageOps.exif_transpose(original).convert('RGB')
            w,h=img.size
            if w*h>45_000_000:img.thumbnail((6500,6500),Image.Resampling.LANCZOS);w,h=img.size
            # Preserve aspect ratio; page size is capped at A3 equivalent in points.
            scale=min(1,1190/w,1684/h)
            pw,ph=max(1,w*scale),max(1,h*scale)
            doc.setPageSize((pw,ph));doc.drawImage(ImageReader(img),0,0,width=pw,height=ph);doc.showPage()
            # OCR at original readable resolution, not the reduced PDF page.
            with tempfile.TemporaryDirectory() as td:
                ocr_input=Path(td)/'page.png';img.save(ocr_input,'PNG')
                proc=subprocess.run([str(exe),str(ocr_input),'stdout','-l',lang,'--tessdata-dir',str(tessdata)],capture_output=True,timeout=240)
                if proc.returncode:raise RuntimeError('OCR страница '+str(i)+': '+proc.stderr.decode('utf8','replace')[-400:])
                text=proc.stdout.decode('utf8','replace').strip()
            if save_images:
                name=f'page_{i:04d}.jpg';img.save(image_dir/name,'JPEG',quality=88,optimize=True)
            else:name=None
            blocks.append(f'\n\n<!-- source-page: {i} -->\n\n## Страница {i}\n\n'+(text or '*Текст не распознан*')+'\n')
            chunks.append({'id':f'{job_id}-{i:04d}','source_page':i,'chapter':1,'text':text,'image':f'images/{name}' if name else None,'source_file':p.name})
            if len(text.split())<12:reviews.append({'page':i,'reason':'Мало распознанного текста; проверьте страницу','image':name})
        progress(i,len(photos))
    doc.save();tmp_pdf.replace(pdf)
    title=base.name.replace('"','')
    md.write_text('---\ntitle: '+json.dumps(title,ensure_ascii=False)+'\nsource_format: photographs\npages: '+str(len(photos))+'\n---\n\n# '+title+'\n'+''.join(blocks),encoding='utf8')
    (dest/'rag_chunks.json').write_text(json.dumps(chunks,ensure_ascii=False,indent=2),encoding='utf8')
    (dest/'review_pages.json').write_text(json.dumps(reviews,ensure_ascii=False,indent=2),encoding='utf8')
    (dest/'metadata.json').write_text(json.dumps({'title':title,'format':'photos','pages':len(photos),'created_at':datetime.now(timezone.utc).isoformat(),'source_folder':str(base),'ocr_language':lang},ensure_ascii=False,indent=2),encoding='utf8')
    return {'id':dest.name,'title':title,'pages':len(photos),'review':len(reviews)}
